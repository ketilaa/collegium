# Hypothesis quality: is the organization actually converging on anything?

Accumulating hypotheses is not the same as accumulating knowledge. This
file tracks, over time, whether the pipeline (Scout → Researcher →
Skeptic → Historian) is actually converging on good, non-duplicate,
falsifiable findings, or just recording anything it can find. It is a
standing concern, not a one-off audit: add a new dated entry whenever
the question comes up again (an owner review, a session's own check
before trusting the board's numbers, or after a fix meant to improve
this), newest last, the same convention as `docs/decisions.md`. Update
the "Known mechanisms and their limits" section when a fix changes one
of them.

## Known mechanisms and their limits

- **Exact-match dedup** (`memory.find_live_hypothesis`): if the model
  re-proposes a hypothesis with the *same* statement (case, spacing and a
  final period ignored) as a live one, the new evidence is attached to
  the existing hypothesis instead of creating a duplicate. This only
  catches identical wording, never a paraphrase.
- **`refines`** (`ProposedHypothesis.refines`, `roles/researcher.py`): the
  Researcher can say a new hypothesis refines an existing one (shown to
  it as E1, E2, ...); if that new one is later accepted, the Historian
  (`historian.py:120-127`) automatically supersedes the old one. This is
  the real merging mechanism, and it works correctly whenever `refines`
  is set — the gap found on 2026-10-03 (below) was that the model rarely
  sets it for a plain restatement, only for a clear "improvement" or
  "correction," which `researcher.md` was then worded to cover
  explicitly. Whether that wording change actually reduces duplicates is
  for the next dated entry below to say, not this one.
- **Visibility is capped**: the Researcher sees at most 15 live
  hypotheses in the domain (`memory.live_hypotheses`'s default `limit`),
  the Strategist at most 20. A domain with more live hypotheses than that
  has ones a new proposal simply cannot be checked against.
- Nothing merges hypotheses after the fact, automatically or by the
  owner's command — the only path to one superseding another is the
  Researcher flagging it at proposal time via `refines`.
- **Giving up** (`historian.py`, `MAX_LIFETIME_CRITIQUES`): a hypothesis
  whose open critiques survive `resolver.MAX_ROUNDS` *and* whose critiques
  (across its whole life, not just this streak) have passed
  `MAX_LIFETIME_CRITIQUES` is retired by the Historian rather than left
  `under_review` to be found as the same "unresolved critiques" gap
  forever. Added 2026-10-03; see that entry below.

## 2026-10-03 · First audit, and the researcher.md fix

Prompted by the owner asking whether the organization had found
anything useful, given it "seems to just trigger on more or less any
statement it can find."

**The numbers** (across all domains, at the time): 80 hypotheses — 10
accepted, 4 rejected, 1 superseded, 49 `under_review`, 16 still
`proposed`. 78 observations.

**What "accepted" actually contained**, reading all 10 statements:

- About 4-5 were genuinely specific, falsifiable, evidence-backed claims
  with real research value (e.g. the AI-adoption/junior-developer-hiring
  link, which the `developers-ai` mission exists to track).
- **2 were the same finding, never merged**: both "AI lets 37signals
  write less code by hand" and "37signals shifted its practices because
  of AI-assisted workflows" were accepted separately, at the same 0.85
  confidence, instead of one refining the other. Root cause found in
  code: `researcher.md` only told the model to set `refines` for a
  "sharper or corrected version" of an existing hypothesis, not for an
  independently-found restatement of the same claim — exactly this case,
  since neither corrects the other, they just both happen to be true.
  Fixed by widening that instruction (see the mechanism note above); not
  yet re-measured.
- **2 were a company's own stated motivation, accepted as fact**: "OpenAI
  is aiming to accelerate scientific discovery by providing free
  access..." and "...responding to a need for democratizing access..." —
  both are OpenAI's own framing of its own announcement, picked up from
  coverage that repeats the press release. Two articles repeating the
  same press release satisfies "two independent sites" without the claim
  ever being independently verified. Not yet fixed; a candidate fix would
  teach the "Be calibrated" rule in `organization.md` that a company's
  stated motive for its own action is the claimant's own word, the same
  caution already applied to a press release's factual claims
  (`reliability.py`'s press-release ceiling), rather than something a
  model should score as near-certain just because it was widely reported.
- **1 wasn't a hypothesis at all**: "Real-SWE is designed to evaluate AI
  models on private, real-world codebases" is a benchmark's own
  documented purpose, not a claim anyone could falsify. `researcher.md`
  already says restating the observation is not a hypothesis; this
  slipped through anyway. Not yet fixed.

**The bigger issue was the middle, not the ends.** The `rejected` bucket
(4) showed the gate does sometimes say no, including to claims above the
0.6 confidence line but blocked by open critiques. But `under_review` had
hypotheses sitting at confidence 0.93, 0.85, 0.82 and 0.80, blocked only
by open critiques that never close — one pair of OpenAI-safety hypotheses
had been critiqued **13 and 14 times respectively** across many separate
Scout/Research passes without ever reaching a verdict either way. The
critique-resolution loop is capped at 2 rounds per `resolve` invocation
(`resolver.MAX_ROUNDS`), but nothing stops a long-lived hypothesis from
accumulating fresh critiques indefinitely across many separate rounds of
new research over weeks. Not investigated further this entry; a
candidate next step is to look at how many of the 49 are stale for that
reason specifically, versus genuinely still being actively worked.

**Net read:** proposing is permissive (almost any descriptive sentence
in a source can become a "hypothesis"); accepting is reasonably strict
(confidence + two independent sources + no blocking critique); but
*resolving* the ambiguous middle, which is most of what gets found, is
the weak step. Confirmed real, specific findings exist and are useful,
but they are a minority of what reaches "accepted," diluted by
duplicates and by accepting a source's own self-interested framing as
verified fact.

## 2026-10-03 · Self-interested sources, and giving up on a stuck hypothesis

Same day, following straight on from the audit above: the owner asked
to fix the two remaining findings.

For the OpenAI-self-description case: `organization.md`'s calibration
rule and `skeptic.md`'s critique guidance now both say explicitly that a
party's own account of its own motives or plans for its own action is
that party's word about itself, not independently verified just because
several outlets repeated it — the same caution `reliability.py` already
gives a press release, now stated as a rule the model applies itself
rather than only a code-level ceiling on known wire-service hosts. Not
yet re-measured; a future entry should check whether this kind of claim
still gets accepted readily.

For the stuck-hypothesis case (13 and 14 critiques, never resolving):
added `historian.MAX_LIFETIME_CRITIQUES` (8). The Historian already sent
a hypothesis back for another round while `round_ < resolver.MAX_ROUNDS`,
and already left a note ("critiques still open after 2 rounds") once
that was exhausted — but left it `under_review` regardless, to be
re-sent by the Strategist the next time it saw the same "unresolved
critiques" gap, which is how two hypotheses reached double-digit
critique counts without ever being decided either way. Now, once a
hypothesis's total critique count (not just this streak) passes the
threshold at the point the current round is also exhausted, the
Historian retires it instead. No new column was added for *why*: the
Historian's own run note already records it, in the same `runs.notes`
trail every other decision leaves.

Not done: the "Real-SWE isn't a falsifiable hypothesis" case, and
re-checking the `refines` wording's actual effect on new duplicates —
both still need a future audit entry to say whether they held up.
