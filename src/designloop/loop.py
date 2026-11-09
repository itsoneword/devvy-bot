"""The design loop (phase 3): variants, mechanical gate, critic
rounds, one collage to the owner.

Every step is injected, so ``pump2 design run`` wires the real ones
(:mod:`src.designloop.steps`) and tests wire fakes:

- ``generate(brief, feedback) -> branches``: the variants
  (``design/<card>/vN``). ``feedback`` maps a branch to what must change;
  empty on the first call. A branch with no feedback is left as it is.
- ``render(branch) -> renders``; ``gate(branch, renders) -> failures``.
  A gate failure goes back to the generator and is not a round; a render that
  failed the gate never reaches the critic.
- ``critic(brief, renders) -> Critique`` (:mod:`.critic`), per variant.
- ``collage(variants) -> Path``; ``deliver(text, images)``.

Stop: every variant ``ready``, :data:`MAX_ROUNDS` rounds, or the same fixes
as last round. The ``ready`` variants reach the owner as a choice; a loop
that stops on rounds / repeated without one still offers every critiqued
variant (dv-2d00), with its open remarks. Either way the offer is sorted by
critic score, best first (= recommended). Only a loop where no variant got
past the gate sends a short "design failed: reasons" note, no images and no
pick.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from .brief import Brief
from .critic import Critique, repeated

MAX_ROUNDS = 3
# Generator re-runs per round for gate failures; past it the variant is
# dropped with its failures as notes.
GATE_RETRIES = 3

Feedback = Mapping[str, Sequence[str]]
Generate = Callable[[Brief, Feedback], Awaitable[Sequence[str]]]
Render = Callable[[str], Awaitable[Sequence[Path]]]
Gate = Callable[[str, Sequence[Path]], Awaitable[Sequence[str]]]
Critic = Callable[[Brief, Sequence[Path]], Awaitable[Critique]]
Collage = Callable[[Sequence["Variant"]], Awaitable[Path]]
Deliver = Callable[[str, Sequence[Path]], Awaitable[None]]
Describe = Callable[[str], Awaitable[str]]


@dataclass
class Variant:
    """One variant as the loop last saw it."""

    branch: str
    renders: Tuple[Path, ...] = ()
    gate_failures: Tuple[str, ...] = ()
    critique: Optional[Critique] = None
    # The critic's unresolved fixes (or the gate's, for a dropped variant):
    # they go with the variant to the owner and the build.
    notes: Tuple[str, ...] = ()
    # One trade-off line for the collage, from the generator's notes.
    tradeoff: str = ""

    @property
    def ready(self) -> bool:
        return self.critique is not None and self.critique.verdict == "ready"

    @property
    def name(self) -> str:
        return self.branch.rsplit("/", 1)[-1]


@dataclass
class Outcome:
    """``stop``: ready | rounds | repeated | gate. ``offered`` are the
    variants the owner picks from (1..n, in order); empty = design failed."""

    stop: str
    rounds: int
    variants: List[Variant]
    offered: List[Variant] = field(default_factory=list)
    collage: Optional[Path] = None
    message: str = ""


def notes_of(critique: Critique) -> Tuple[str, ...]:
    return tuple(f"{name}: {fix}" for name, fix in critique.fixes.items())


async def run_loop(
    brief: Brief,
    *,
    generate: Generate,
    render: Render,
    gate: Gate,
    critic: Critic,
    collage: Collage,
    deliver: Deliver,
    describe: Optional[Describe] = None,
    title: str = "",
    max_rounds: int = MAX_ROUNDS,
) -> Outcome:
    """Run the loop to a stop; deliver the collage or the failure note."""
    title = title or brief.screen_id
    feedback: Dict[str, List[str]] = {}
    last: Dict[str, Critique] = {}
    variants: List[Variant] = []
    stop, rounds = "rounds", 0
    for n in range(1, max_rounds + 1):
        passed, dropped = await _gated(brief, feedback, generate, render, gate)
        if not passed:
            variants, stop = dropped, "gate"
            break
        rounds = n
        for variant in passed:
            variant.critique = await critic(brief, variant.renders)
            variant.notes = notes_of(variant.critique)
        variants = passed + dropped
        revising = [v for v in passed if not v.ready]
        if not revising:
            stop = "ready"
            break
        if last and all(
            v.branch in last and repeated(last[v.branch], v.critique)  # type: ignore[arg-type]
            for v in revising
        ):
            stop = "repeated"
            break
        last = {v.branch: v.critique for v in passed if v.critique}
        feedback = {v.branch: list(v.notes) for v in revising}

    offered = [v for v in variants if v.ready]
    if not offered and stop in ("rounds", "repeated"):
        offered = [v for v in variants if v.critique is not None]
    offered.sort(key=lambda v: v.critique.score if v.critique else 0, reverse=True)
    outcome = Outcome(stop=stop, rounds=rounds, variants=variants, offered=offered)
    if offered:
        if describe:
            for variant in offered:
                variant.tradeoff = (await describe(variant.branch)).strip()
        outcome.collage = await collage(offered)
        outcome.message = offer_text(title, outcome)
        await deliver(outcome.message, [outcome.collage])
    else:
        outcome.message = failed_text(title, outcome)
        await deliver(outcome.message, [])
    return outcome


async def _gated(
    brief: Brief,
    feedback: Feedback,
    generate: Generate,
    render: Render,
    gate: Gate,
) -> Tuple[List[Variant], List[Variant]]:
    """Generate, render and gate until every variant passes or the retries
    run out. Returns (passed, dropped)."""
    asked: Feedback = feedback
    variants: List[Variant] = []
    for attempt in range(GATE_RETRIES + 1):
        variants = []
        for branch in await generate(brief, asked):
            renders = tuple(Path(p) for p in await render(branch))
            failures = tuple(await gate(branch, renders))
            variants.append(Variant(branch, renders, gate_failures=failures))
        failed = [v for v in variants if v.gate_failures]
        if not failed or attempt == GATE_RETRIES:
            break
        # Only the gate's findings: a variant that passed keeps what it has,
        # and the critic's fixes it already got are not applied twice.
        asked = {v.branch: [f"gate: {f}" for f in v.gate_failures] for v in failed}
    passed = [v for v in variants if not v.gate_failures]
    dropped = [v for v in variants if v.gate_failures]
    for variant in dropped:
        variant.notes = tuple(f"gate: {f}" for f in variant.gate_failures)
    return passed, dropped


STOP_WHY = {
    "rounds": f"no variant ready after {MAX_ROUNDS} critic rounds",
    "repeated": "the critic's notes repeated: the generator is not moving",
    "gate": "no variant passed the mechanical gate",
}


# The Telegram message is cut past its limit: keep the remarks short so the
# pick line survives. The full notes stay in the card body.
REMARKS_SHOWN = 2
REMARK_CHARS = 160


def offer_text(title: str, outcome: Outcome) -> str:
    """The owner's choice: one line per offered variant, reply a number."""
    n = len(outcome.offered)
    plural = "s" if n > 1 else ""
    ready = all(v.ready for v in outcome.offered)
    if ready:
        lines = [f"🎨 {title}: {n} design variant{plural}, critic: ready"]
    else:
        lines = [
            f"🎨 {title}: {n} design variant{plural}; the critic called none ready "
            f"after {outcome.rounds} critic round{'s' if outcome.rounds != 1 else ''}"
            " — best score first, 1 is recommended"
        ]
    for i, variant in enumerate(outcome.offered, 1):
        score = variant.critique.score if variant.critique else 0
        line = f"{i}. {variant.tradeoff or variant.branch} ({score}/10)"
        if not ready and i == 1:
            line += " — recommended"
        lines.append(line)
        if not variant.ready:
            for note in variant.notes[:REMARKS_SHOWN]:
                if len(note) > REMARK_CHARS:
                    note = note[: REMARK_CHARS - 1] + "…"
                lines.append(f"   · {note}")
            if len(variant.notes) > REMARKS_SHOWN:
                lines.append(f"   · +{len(variant.notes) - REMARKS_SHOWN} more")
    choices = ", ".join(str(i) for i in range(1, n + 1))
    lines.append(f"↩ reply with the number of the one to build: {choices}")
    return "\n".join(lines)


def failed_text(title: str, outcome: Outcome) -> str:
    """The owner's rule: a short note with the reasons, no choice."""
    lines = [f"🎨 design failed: {title} — {STOP_WHY.get(outcome.stop, outcome.stop)}"]
    for variant in outcome.variants:
        if variant.notes:
            lines.append(f"{variant.name}: " + "; ".join(variant.notes))
    return "\n".join(lines)
