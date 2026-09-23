"""Mapping related entities: which organisations, people, products and
technologies an observation and its evidence mention.

Runs after research as the Researcher. Each mention must be found in the
text it is attributed to. Entities are matched to known ones by name or
alias, so the organization builds one picture of each company or product
across observations and domains.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from collegium import memory
from collegium.db import Connection
from collegium.grounding import mentions
from collegium.jobs import Job
from collegium.roles.base import Context, Persist, Role
from collegium.untrusted import fence

EntityType = Literal[
    "organization", "company", "person", "product", "model", "technology", "place", "other"
]


class MentionedEntity(BaseModel):
    name: str = Field(description="The name exactly as written in a text")
    entity_type: EntityType
    mentioned_in: list[int] = Field(min_length=1, description="Numbers of the texts, e.g. [1, 3]")


class EntityMap(BaseModel):
    entities: list[MentionedEntity] = Field(max_length=12)


class Mapper(Role):
    name = "researcher"
    job_kind = "map"
    prompt_file = "mapper.md"

    def prepare(self, ctx: Context, job: Job) -> Persist:
        observation_id = UUID(job.payload["observation_id"])
        evidence_ids = [UUID(i) for i in job.payload.get("evidence_ids", [])]
        with ctx.db.reading() as conn:
            obs = memory.observation(conn, observation_id)
            if obs is None:
                raise LookupError(f"observation {observation_id} not found")
            domain_ids = memory.node_domain_ids(conn, observation_id)
            evidence = memory.observation_evidence(conn, observation_id)
            evidence += [
                e
                for e in memory.evidence_texts(conn, evidence_ids)
                if e["id"] not in {x["id"] for x in evidence}
            ]
            known = memory.top_entities(conn, domain_ids, limit=30)

        # T1 is the observation; the rest are the evidence excerpts.
        texts: list[tuple[UUID, str]] = [(observation_id, obs["statement"])]
        texts += [(e["id"], e["excerpt"]) for e in evidence if e["excerpt"]]
        listing = "\n\n".join(fence(f"T{i}", text) for i, (_, text) in enumerate(texts, 1))
        known_names = ", ".join(k["name"] for k in known) or "none yet"
        entity_map = ctx.llm.generate(
            self.system_prompt(),
            f"Entities the organization already knows: {known_names}\n\n{listing}\n\n"
            "Which entities do these texts mention?",
            EntityMap,
        )

        def persist(conn: Connection) -> str:
            linked = created = ungrounded = 0
            for entity in entity_map.entities:
                name = " ".join(entity.name.split())
                places = [
                    texts[n - 1][0]
                    for n in dict.fromkeys(entity.mentioned_in)
                    if 1 <= n <= len(texts) and mentions(texts[n - 1][1], name)
                ]
                if not places:
                    ungrounded += 1
                    continue
                entity_id = memory.find_entity(conn, name)
                if entity_id is None:
                    entity_id = memory.add_entity(conn, name=name, entity_type=entity.entity_type)
                    created += 1
                memory.tag_domains(conn, entity_id, domain_ids)
                for node_id in places:
                    if not memory.has_relationship(conn, node_id, "mentions", entity_id):
                        memory.add_relationship(
                            conn, subject_id=node_id, predicate="mentions", object_id=entity_id
                        )
                        linked += 1
            return (
                f"{len(entity_map.entities)} entities proposed: {created} new, "
                f"{linked} mentions linked, {ungrounded} not found in their texts"
            )

        return persist
