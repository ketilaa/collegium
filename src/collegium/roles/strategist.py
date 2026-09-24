"""The Strategist: decides where the organization's attention goes.

Runs daily per domain (queued by the scheduler) or on the owner's request.
It starts from deterministic knowledge-gap checks (`strategy.find_gaps`),
asks the model for goals, actions and at most one program proposal, and
then applies the rules that are not the model's to decide:

- goals are created active (the owner's decision), without duplicates,
  and closed once everything they investigate has been decided;
- goals the model says no longer serve the missions are abandoned, with
  its reason kept as the goal's outcome, if they are active goals of this
  domain and the same plan does not also continue them;
- actions are queued only within the remaining budget, most important
  first, and one scout per day is always kept;
- a program is only ever proposed, as a decision for the owner.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from collegium import jobs, memory, strategy
from collegium.db import Connection
from collegium.jobs import Job
from collegium.roles.base import Context, Persist, Role

# Estimated paid calls per action, including the review it leads to.
ACTION_COST = {"scout": 8, "corroborate": 8, "resolve": 8}
# Paid calls the Strategist leaves for work already queued.
RESERVE = 5


class Action(BaseModel):
    kind: Literal["scout", "corroborate", "resolve"]
    target: str | None = Field(None, description="H label for corroborate and resolve")
    focus: str | None = Field(None, description="For scout: what to look into, in plain words")


class PlannedGoal(BaseModel):
    existing: str | None = Field(None, description="G label, to continue an existing goal")
    statement: str = Field(description="What the organization wants to find out")
    success_criteria: str
    priority: int = Field(ge=1, le=5, description="1 is most important")
    about: list[str] = Field(default_factory=list, description="H and N labels")
    actions: list[Action] = Field(default_factory=list, max_length=3)


class GoalAbandonment(BaseModel):
    goal: str = Field(description="G label of an active goal")
    reason: str = Field(description="Why it no longer deserves attention")


class ProgramProposal(BaseModel):
    name: str
    charter: str = Field(description="Why this deserves sustained attention, and its scope")
    rationale: str


class StrategyPlan(BaseModel):
    assessment: str
    goals: list[PlannedGoal] = Field(max_length=3)
    abandon: list[GoalAbandonment] = Field(
        default_factory=list,
        max_length=3,
        description="Active goals to stop pursuing: off-mission, stale or superseded",
    )
    program: ProgramProposal | None = None


class Strategist(Role):
    name = "strategist"
    job_kind = "strategize"
    prompt_file = "strategist.md"
    searches = False  # it queues work; the work itself searches

    def prepare(self, ctx: Context, job: Job) -> Persist:
        domain_id = UUID(job.payload["domain_id"])
        with ctx.db.reading() as conn:
            domain = memory.domain(conn, domain_id)
            if domain is None:
                raise LookupError(f"domain {domain_id} not found")
            hypotheses = memory.live_hypotheses(conn, [domain_id], limit=20)
            entities = memory.top_entities(conn, [domain_id], limit=15)
            goals = memory.active_goals(conn, [domain_id])
            programs = memory.programs(conn, [domain_id])
            abandoned = memory.closed_goals(conn, [domain_id], "abandoned")
            gaps = strategy.find_gaps(conn, domain_id)
            budget = _remaining_budget(ctx, conn)
            missions = (memory.mission(conn, None), memory.mission(conn, domain_id))
            open_critiques = {
                h["id"]: len(memory.open_critiques(conn, h["id"])) for h in hypotheses
            }

        labels: dict[str, UUID] = {}
        labels |= {f"H{i}": h["id"] for i, h in enumerate(hypotheses, 1)}
        labels |= {f"N{i}": e["id"] for i, e in enumerate(entities, 1)}
        labels |= {f"G{i}": g["id"] for i, g in enumerate(goals, 1)}
        label_of = {v: k for k, v in labels.items()}

        brief = _brief(
            domain,
            missions,
            hypotheses,
            open_critiques,
            entities,
            goals,
            abandoned,
            programs,
            gaps,
            label_of,
            budget,
        )
        plan = ctx.llm.generate(
            self.system_prompt(), brief + "\n\nWhat is your plan?", StrategyPlan
        )
        hypothesis_ids = {h["id"] for h in hypotheses}

        def persist(conn: Connection) -> str:
            notes = [f"assessment: {plan.assessment}"]
            achieved = _close_finished_goals(conn, goals)
            if achieved:
                notes.append(f"{len(achieved)} goals achieved")
            abandoned_now = _abandon_goals(conn, plan, labels, goals, achieved)
            notes += [f"abandoned goal: {s} ({reason})" for s, reason in abandoned_now.values()]
            closed = achieved | set(abandoned_now)

            allowance = budget - RESERVE
            queued: list[str] = []
            skipped = 0
            scouted = False
            existing_goals = {memory.statement_key(g["statement"]): g["id"] for g in goals}
            for planned in sorted(plan.goals, key=lambda g: g.priority):
                goal_id = labels.get((planned.existing or "").strip().upper())
                if goal_id is None:
                    goal_id = existing_goals.get(memory.statement_key(planned.statement))
                if goal_id in closed:
                    continue  # closed in this run; nothing more to do for it
                if goal_id is None:
                    goal_id = memory.add_goal(
                        conn,
                        statement=planned.statement,
                        success_criteria=planned.success_criteria,
                        priority=planned.priority,
                    )
                    memory.tag_domains(conn, goal_id, [domain_id])
                    existing_goals[memory.statement_key(planned.statement)] = goal_id
                    notes.append(f"new goal: {planned.statement}")
                for label in planned.about:
                    target = labels.get(label.strip().upper())
                    if (
                        target
                        and target != goal_id
                        and not memory.has_relationship(conn, goal_id, "investigates", target)
                    ):
                        memory.add_relationship(
                            conn, subject_id=goal_id, predicate="investigates", object_id=target
                        )
                for action in planned.actions:
                    payload = _payload(action, domain_id, labels, hypothesis_ids)
                    if payload is None:
                        skipped += 1
                        continue
                    cost = ACTION_COST[action.kind]
                    if cost > allowance:
                        skipped += 1
                        continue
                    allowance -= cost
                    jobs.enqueue(
                        conn, action.kind, {**payload, "goal_id": goal_id}, parent_job_id=job.id
                    )
                    queued.append(action.kind)
                    scouted |= action.kind == "scout"

            # One scout a day keeps the organization's view current, whatever
            # the plan says, if the budget allows.
            if not scouted and any(g.kind == "not scouted" for g in gaps):
                if ACTION_COST["scout"] <= allowance:
                    jobs.enqueue(conn, "scout", {"domain_id": domain_id}, parent_job_id=job.id)
                    queued.append("scout")
                else:
                    skipped += 1

            if plan.program and not any(
                memory.statement_key(p["name"]) == memory.statement_key(plan.program.name)
                for p in programs
            ):
                program_id = memory.add_program(
                    conn, name=plan.program.name, charter=plan.program.charter
                )
                memory.tag_domains(conn, program_id, [domain_id])
                decision_id = memory.add_decision(
                    conn,
                    statement=f"Open research program: {plan.program.name}",
                    rationale=plan.program.rationale,
                )
                memory.tag_domains(conn, decision_id, [domain_id])
                memory.add_relationship(
                    conn, subject_id=decision_id, predicate="concerns", object_id=program_id
                )
                notes.append(f"proposed program for the owner: {plan.program.name}")

            notes.append(
                f"{len(gaps)} gaps; queued {', '.join(queued) or 'nothing'}"
                + (f"; {skipped} actions skipped (budget or invalid)" if skipped else "")
                + f"; budget left before queueing {budget}"
            )
            return "; ".join(notes)

        return persist


def _payload(action: Action, domain_id, labels, hypothesis_ids) -> dict | None:
    if action.kind == "scout":
        return {"domain_id": domain_id, "focus": action.focus}
    target = labels.get((action.target or "").strip().upper())
    if target not in hypothesis_ids:
        return None
    if action.kind == "resolve":
        return {"hypothesis_id": target, "round": 1}
    return {"hypothesis_id": target}


def _close_finished_goals(conn: Connection, goals: list[dict]) -> set[UUID]:
    """Goals whose hypotheses have all been decided are achieved."""
    closed = set()
    for g in goals:
        targets = [memory.hypothesis(conn, t) for t in g["about"]]
        hypotheses = [h for h in targets if h is not None]
        if hypotheses and all(h["status"] not in ("proposed", "under_review") for h in hypotheses):
            memory.set_goal_status(
                conn, g["id"], "achieved", "Every hypothesis it investigated has been decided."
            )
            closed.add(g["id"])
    return closed


def _abandon_goals(
    conn: Connection, plan: StrategyPlan, labels: dict, goals: list[dict], achieved: set[UUID]
) -> dict[UUID, tuple[str, str]]:
    """Abandon the goals the plan drops, keeping the reason. Only active
    goals of this domain can be abandoned, never one the same plan also
    continues: when the model contradicts itself, the goal stays."""
    active = {g["id"]: g for g in goals if g["id"] not in achieved}
    continued = {labels.get((p.existing or "").strip().upper()) for p in plan.goals}
    abandoned: dict[UUID, tuple[str, str]] = {}
    for a in plan.abandon:
        goal_id = labels.get(a.goal.strip().upper())
        reason = a.reason.strip()
        if goal_id not in active or goal_id in continued or goal_id in abandoned or not reason:
            continue
        memory.set_goal_status(conn, goal_id, "abandoned", reason)
        abandoned[goal_id] = (active[goal_id]["statement"], reason)
    return abandoned


def _remaining_budget(ctx: Context, conn: Connection) -> int:
    if ctx.acquisition is None:
        return 0
    used, _ = memory.paid_calls_in_window(conn, ctx.acquisition.metered_providers)
    return ctx.settings.daily_call_budget - used


def _brief(
    domain,
    missions,
    hypotheses,
    open_critiques,
    entities,
    goals,
    abandoned,
    programs,
    gaps,
    label_of,
    budget,
) -> str:
    lines = [f"Domain: {domain['name']}"]
    if domain.get("description"):
        lines.append(domain["description"])
    # Missions are the owner's own words, not outside text.
    organization, domain_mission = missions
    if organization:
        lines.append(f"\nThe organization's mission, set by the owner: {organization['statement']}")
    if domain_mission:
        lines.append(f"This domain's mission, set by the owner: {domain_mission['statement']}")
    lines.append("\nHypotheses:")
    for h in hypotheses:
        conf = f"{h['confidence']:.2f}" if h["confidence"] is not None else "unassessed"
        lines.append(
            f"[{label_of[h['id']]}] ({h['status']}, confidence {conf}, "
            f"{open_critiques[h['id']]} open critiques) {h['statement']}"
        )
    if not hypotheses:
        lines.append("- none yet")
    lines.append("\nEntities mentioned most:")
    lines += [
        f"[{label_of[e['id']]}] {e['name']} ({e['entity_type']}, {e['mentions']} mentions)"
        for e in entities
    ] or ["- none yet"]
    lines.append("\nActive goals:")
    for g in goals:
        about = ", ".join(label_of.get(t, "?") for t in g["about"]) or "nothing linked"
        lines.append(
            f"[{label_of[g['id']]}] (priority {g['priority']}) {g['statement']} -- about {about}"
        )
    if not goals:
        lines.append("- none")
    if abandoned:
        lines.append("\nRecently abandoned goals (do not set them again unless something changed):")
        lines += [f"- {g['statement']} Reason: {g['outcome']}" for g in abandoned]
    lines.append("\nResearch programs:")
    lines += [f"- ({p['status']}) {p['name']}: {p['charter']}" for p in programs] or ["- none"]
    lines.append("\nKnowledge gaps found:")
    for gap in gaps:
        about = f"[{label_of[gap.about]}] " if gap.about in label_of else ""
        lines.append(f"- {gap.kind}: {about}{gap.description}")
    if not gaps:
        lines.append("- none")
    lines.append(
        f"\nPaid searches left in the next 24 hours: {budget}. Each action costs about "
        f"{ACTION_COST['scout']}."
    )
    return "\n".join(lines)
