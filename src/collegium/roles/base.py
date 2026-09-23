"""What every role shares.

A role's `prepare` does the slow part (reading memory, searching, asking the
model) outside any transaction, and returns a `Persist` function. The worker
calls that function inside one transaction acting as the role, together
with marking the job done, so a job's knowledge and its follow-up jobs are
committed all at once or not at all.
"""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from collegium import memory
from collegium.acquisition import Acquisition, Document
from collegium.config import Settings
from collegium.db import Connection, Database
from collegium.grounding import locate_excerpt
from collegium.jobs import Job
from collegium.llm import LLM

Persist = Callable[[Connection], str]


class NothingToWorkWith(RuntimeError):
    """Raised when a role cannot do its job yet, e.g. search found nothing.
    The job is retried later."""


@dataclass
class Context:
    db: Database
    llm: LLM
    acquisition: Acquisition | None
    settings: Settings


class Role:
    name: ClassVar[str]  # actor name in the database
    job_kind: ClassVar[str]
    prompt_file: ClassVar[str | None] = None
    uses_llm: ClassVar[bool] = True

    def system_prompt(self) -> str:
        prompts = resources.files("collegium.roles") / "prompts"
        shared = (prompts / "organization.md").read_text()
        return shared + "\n\n" + (prompts / self.prompt_file).read_text()

    def version(self) -> str:
        """Identifies the role definition that produced a run. Changes
        whenever the prompt changes."""
        return hashlib.sha256(self.system_prompt().encode()).hexdigest()[:12]

    def prepare(self, ctx: Context, job: Job) -> Persist:
        raise NotImplementedError

    def gather(self, ctx: Context, queries: list[str]) -> list[Document]:
        if ctx.acquisition is None:
            raise NothingToWorkWith("no acquisition provider configured")
        s = ctx.settings
        return ctx.acquisition.gather(
            queries,
            max_results=s.max_search_results,
            max_documents=s.max_documents,
            max_chars=s.max_document_chars,
        )


# ---------------------------------------------------------------------------
# Shared model output
# ---------------------------------------------------------------------------


_QUERY_LABEL = re.compile(r"^\s*(web\s+)?search(\s+query)?\s*:\s*", re.IGNORECASE)
_QUERY_EXCLUSION = re.compile(r"\s-\s*(['\"]).*?\1|\s-\S+")


class SearchPlan(BaseModel):
    queries: list[str] = Field(
        min_length=1, max_length=3, description="Plain web search queries, no operators"
    )

    @field_validator("queries")
    @classmethod
    def _plain(cls, queries: list[str]) -> list[str]:
        """Small models decorate queries with labels, quotes and exclusions."""
        cleaned = []
        for q in queries:
            q = _QUERY_EXCLUSION.sub("", _QUERY_LABEL.sub("", q))
            q = q.strip().strip("'\"").strip()
            if q:
                cleaned.append(q)
        if not cleaned:
            raise ValueError("no usable queries")
        return cleaned


class Stance(BaseModel):
    hypothesis: str = Field(description="Label of the hypothesis, e.g. H1 or E2")
    stance: Literal["supports", "contradicts", "context"]
    rationale: str = Field(description="Why the excerpt supports, contradicts or informs it")


class EvidenceItem(BaseModel):
    document: int = Field(description="Number of the document quoted, e.g. 2 for [D2]")
    excerpt: str = Field(
        description="Words copied exactly from that document: one to three sentences, no headings"
    )
    summary: str = Field(description="What the excerpt shows, in one sentence")
    reliability: float = Field(ge=0, le=1, description="How far this source can be trusted")
    bears_on: list[Stance] = Field(min_length=1)


# ---------------------------------------------------------------------------
# Rendering and storing
# ---------------------------------------------------------------------------


def render_documents(documents: list[Document]) -> str:
    if not documents:
        return "No documents were found."
    parts = []
    for i, d in enumerate(documents, 1):
        header = f"[D{i}] {d.title}\n{d.url}"
        if d.published_at:
            header += f"\nPublished: {d.published_at}"
        parts.append(f"{header}\n---\n{d.content}")
    return "\n\n".join(parts)


def _label(label: str) -> str:
    return label.strip().upper()


@dataclass(frozen=True)
class GroundedEvidence:
    item: EvidenceItem
    document: Document
    excerpt: str  # the source's own words


@dataclass
class Grounding:
    grounded: list[GroundedEvidence]
    dropped: list[str]  # excerpts the model gave that are not in the cited document

    def supported(self) -> set[str]:
        """Labels of hypotheses that at least one grounded excerpt supports."""
        return {
            _label(s.hypothesis)
            for g in self.grounded
            for s in g.item.bears_on
            if s.stance == "supports"
        }

    def describe_dropped(self, limit: int = 3) -> str:
        if not self.dropped:
            return ""
        shown = "; ".join(repr(d[:100]) for d in self.dropped[:limit])
        return f" Dropped: {shown}"


def ground_evidence(items: list[EvidenceItem], documents: list[Document]) -> Grounding:
    """Keep the evidence whose excerpt is found in a retrieved document.

    The cited document is tried first. A small model sometimes cites the
    wrong number, so the other documents are tried next; the evidence is then
    attributed to the document that actually contains the excerpt.
    """
    grounding = Grounding(grounded=[], dropped=[])
    for item in items:
        cited = item.document - 1
        order = sorted(range(len(documents)), key=lambda i: i != cited)
        for i in order:
            excerpt = locate_excerpt(item.excerpt, documents[i].content)
            if excerpt is not None:
                grounding.grounded.append(GroundedEvidence(item, documents[i], excerpt))
                break
        else:
            grounding.dropped.append(item.excerpt)
    return grounding


@dataclass
class EvidenceOutcome:
    stored: int = 0
    unlinked: int = 0
    touched: set[UUID] = field(default_factory=set)


def store_evidence(
    conn: Connection,
    grounded: list[GroundedEvidence],
    targets: dict[str, UUID],
    domain_ids: list[UUID],
) -> EvidenceOutcome:
    """Store grounded evidence, linked to the hypotheses it bears on.
    `targets` maps prompt labels to hypothesis ids; stances on other labels
    are ignored, and evidence bearing on none of them is not stored."""
    outcome = EvidenceOutcome()
    for g in grounded:
        stances = [s for s in g.item.bears_on if _label(s.hypothesis) in targets]
        if not stances:
            outcome.unlinked += 1
            continue
        source_id = memory.record_source(
            conn,
            uri=g.document.url,
            title=g.document.title,
            published_at=g.document.published_at,
            content=g.document.content,
            metadata={"provider": g.document.provider, **g.document.metadata},
            acquisition_id=g.document.acquisition_id,
        )
        evidence_id = memory.add_evidence(
            conn,
            summary=g.item.summary,
            excerpt=g.excerpt,
            source_id=source_id,
            reliability=g.item.reliability,
        )
        memory.tag_domains(conn, evidence_id, domain_ids)
        linked: set[UUID] = set()
        for s in stances:
            target = targets[_label(s.hypothesis)]
            if target in linked:
                continue
            memory.link_evidence(
                conn,
                evidence_id=evidence_id,
                target_id=target,
                target_kind="hypothesis",
                stance=s.stance,
                rationale=s.rationale,
            )
            linked.add(target)
        outcome.stored += 1
        outcome.touched |= linked
    return outcome
