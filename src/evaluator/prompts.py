"""Evaluator prompt templates for memory extraction (op-based)."""

from typing import List, Optional

MEMORY_OP_INSTRUCTIONS = """\
You maintain an agent's long-term memory. You are shown the CURRENT facts and a
recent conversation. Emit ONLY new or changed facts as a JSON array of ops.

Each fact is ONE atomic statement, kept verbatim-close to what was said.

Ops:
  {"op": "add", "kind": "user_fact"|"conversation_result",
   "tier": "pinned"|"general", "content": "<one atomic statement>",
   "label": "<optional>", "source_ref": "<optional workspace file path>",
   "supersedes_id": <optional id of an existing fact you believe this updates>}
  {"op": "invalidate", "target_id": <id>}
      # fact is no longer true and nothing replaces it

You emit CANDIDATES only. A separate reconciliation step compares each
candidate against the existing facts and decides whether it is new, already
known, or replaces an old fact — do not make that call yourself. When you
believe a candidate updates a specific existing fact, attach its id as
"supersedes_id" (a hint, not a decision).

Kind rules:
- "user_fact": something the user stated or that is true of the user.
- "conversation_result": something YOU (the agent) produced — research, a
  diagnosis, a decision reached. If a workspace file holds the full text, put
  its path in "source_ref" and keep "content" to a one-line summary.

Durability rules:
- A fact must still be true and useful weeks from now. Dated one-off events —
  a meal (with or without kcal/running totals), one night's sleep, one
  session's actual sets/reps, one weigh-in, one day's summary — are log
  entries, never facts. Emit nothing for them.
- Only durable statements qualify: preferences, allergies/injuries/medical
  conditions, goals and targets, standing protocols/programs, biographical
  facts.
- A repeated pattern stated AS a pattern ("usually skips breakfast") is
  durable; the instance ("skipped breakfast today") is not.
- Open work is not a fact: a todo, a plan in progress, an open question
  ("need to check X"), or a ticket's current status goes stale silently
  because a fact has no done state. Todos live in items, tickets on the
  board. Emit nothing for them. A DECISION reached is a fact; the work it
  leads to is not.

Lifecycle rules:
- One statement per op. NEVER combine an old fact and new information into one
  op — when a fact changed, emit an "add" whose content is ONLY the new
  statement (plus the "supersedes_id" hint); the old text is preserved
  automatically.
- Scope difference is NOT contradiction ("no nuts in desserts" vs "allergic to
  nuts" are both true). When unsure whether two facts conflict, keep both: emit
  nothing, or "add" the new one without a hint.

Tier rules:
- "pinned" = a standing constraint that must never be violated, in ANY domain.
  Not only food. It covers:
    * safety/health limits — allergies, medical restrictions, "no oven"
    * privacy and disclosure rules — what must never be published, shown or
      written into public output (real names, addresses, credentials, ids)
    * standing "never do X" / "always do X" instructions the user has given
  If violating the fact would harm the user or break a promise to them, it is
  pinned, however it was phrased in conversation.
- "core" = a hard identity fact the agent must always know about the user:
  age/body metrics, goals, training availability, the current agreed
  plan/program, standing defaults ("drinks 3.2% milk unless stated").
  Core facts are permanent context — they change only when the user's life
  changes, never day to day. Not for status, events, or observations.
- "general" = soft preferences, history, status, project notes.
- Emit nothing ([]) if there is nothing new or changed.
- Never restate facts already present unchanged."""

# Appended ONLY when the speaking user belongs to a family. Conservative by
# design: conversations are private, private is the default scope, and only
# explicitly family-framed facts may cross into the shared scope.
FAMILY_SCOPE_INSTRUCTIONS = """\

Scope (this user belongs to a family):
- Each op may add "scope": "private" (default) or "family".
- Use "family" ONLY when the user explicitly frames the fact as shared with
  or for the whole family ("we", "our family", "remember this for the
  family"). Facts about the household that everyone should see (shared
  dietary rules, family schedule, home context) qualify ONLY with such
  explicit framing.
- Ambiguous or personal framing -> "private". When unsure, ALWAYS choose
  "private". Conversations are private: never promote something a user said
  about themselves into family scope.
- For "family" facts, state who reported it inside the content (e.g.
  "Anna: the family dinner is at 19:00") so other members see attribution.
- Facts listed under "Current Family Facts" are family-scoped; supersede
  them only with ops carrying "scope": "family"."""


EVALUATOR_PROMPT = """You maintain an agent's long-term memory.

## Current Facts (id | kind | tier | date | content)
{current_facts}
{family_section}
## Instructions
{instructions}

## Output Format
Return ONLY a JSON array of ops. No commentary, no markdown fences.
If nothing is new or changed, return: []

## Conversation
{conversation_text}
"""

FAMILY_FACTS_SECTION = """
## Current Family Facts (id | kind | tier | date | content) — shared, scope "family"
{family_facts}
"""


def format_current_facts(facts: List) -> str:
    """Render existing facts as 'id | kind | tier | date | content' so the
    model can reason about kinds and dates when deciding ops."""
    if not facts:
        return "(no existing facts)"
    lines = []
    for f in facts:
        valid_from = getattr(f, "valid_from", None)
        date = valid_from.strftime("%d.%m.%y") if valid_from else "?"
        kind = getattr(f, "kind", "user_fact")
        lines.append(f"{f.id} | {kind} | {f.tier} | {date} | {f.content}")
    return "\n".join(lines)


# Reconciler (dv-c5d9 step 3, second haiku call, owner decision D4). The
# reconciler — not the extraction prompt — decides contradiction (review R5);
# its verdicts are mapped to ops by deterministic code in
# src/evaluator/reconciler.py. Guardrails are the fixed owner decisions,
# verbatim: scope difference is not contradiction; when unsure keep both.
RECONCILE_INSTRUCTIONS = """\
You reconcile CANDIDATE facts against an agent's EXISTING facts. For each
candidate decide, by contradiction — never by ranking or "which is better":

- "new": no existing fact says this or conflicts with it.
- "duplicate": an existing fact already states the same information. Give its
  id as "existing_id". Restatements and translations count as duplicates.
- "contradicts": the candidate and an existing fact cannot both be true (the
  fact CHANGED — the candidate states the new state of the same thing). Give
  the existing fact's id as "existing_id". Set "safety": true when either
  side is safety-relevant (allergy, medication, medical limits, health
  constraints).
- "unsure": might overlap or conflict, but you are not certain.

Revocation:
- When a candidate states that a restriction imposed by an existing fact is
  LIFTED / RELAXED / EXPIRED / CHANGED (any language — "снято", "прошло",
  "no longer applies"), verdict is "contradicts" with the imposing fact's id
  as "existing_id" AND "revokes": true. A constraint and its own revocation
  are never "both true", never "unsure", and never a safety conflict to keep
  side by side.

Guardrails:
- Scope difference is NOT contradiction ("no nuts in desserts" vs "allergic
  to nuts" are both true — verdict "new" or "unsure", never "contradicts").
- When unsure, keep both: verdict "unsure".
- Only compare a candidate against facts in the SAME section (private vs
  family). A cross-section clash is "unsure".

Return ONLY a JSON array, one verdict per candidate, no commentary:
  {"index": <candidate index>, "verdict": "new"|"duplicate"|"contradicts"|
   "unsure", "existing_id": <id or null>, "safety": true|false,
   "revokes": true|false}
"""

RECONCILE_PROMPT = """You reconcile candidate memory facts against existing ones.

## Existing Facts (id | kind | tier | date | content)
{current_facts}
{family_section}
## Candidates (index | tier | content)
{candidates}

## Instructions
{instructions}
"""

RECONCILE_FAMILY_SECTION = """
## Existing Family Facts (id | kind | tier | date | content) — shared scope
{family_facts}
"""


def build_reconcile_prompt(
    candidates: List,
    current_facts: str,
    family_facts: Optional[str] = None,
) -> str:
    """Build the reconciler prompt. ``candidates`` are validated op dicts."""
    lines = []
    for i, cand in enumerate(candidates):
        lines.append(f"{i} | {cand.get('tier')} | {cand.get('content')}")
    family_section = ""
    if family_facts is not None:
        family_section = RECONCILE_FAMILY_SECTION.format(family_facts=family_facts)
    return RECONCILE_PROMPT.format(
        current_facts=current_facts,
        family_section=family_section,
        candidates="\n".join(lines) or "(none)",
        instructions=RECONCILE_INSTRUCTIONS,
    )


def build_evaluator_prompt(
    conversation_text: str,
    current_facts: str,
    family_facts: Optional[str] = None,
) -> str:
    """Build the full evaluator prompt from components.

    ``family_facts`` is None for users without a family — the prompt then
    never mentions family scope, so the evaluator cannot emit it.
    """
    instructions = MEMORY_OP_INSTRUCTIONS
    family_section = ""
    if family_facts is not None:
        instructions += FAMILY_SCOPE_INSTRUCTIONS
        family_section = FAMILY_FACTS_SECTION.format(family_facts=family_facts)
    return EVALUATOR_PROMPT.format(
        current_facts=current_facts,
        family_section=family_section,
        instructions=instructions,
        conversation_text=conversation_text,
    )
