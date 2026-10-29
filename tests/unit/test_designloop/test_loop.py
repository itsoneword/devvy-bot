"""The design loop runner (dv-9133 phase 3): variants, gate, critic rounds,
collage.

Contract pinned here (docs/research/dv-9133/analysis.md §3 and phase 3):
``src.designloop.loop.run_loop(brief, *, generate, render, gate, critic,
collage, deliver)`` is a coroutine. Every step is injected and awaited, so the
taskpump subcommand wires the real ones (agentjob on the ``design_generate``
route, the board's render / gate argv, ``critic.critique`` through the router,
the board python's collage script, the pump's notify) and these tests wire
fakes. No real model, browser or Telegram.

- ``generate(brief, feedback) -> Sequence[str]``: the variant branches
  (``design/<card>/vN``). ``feedback`` maps a branch to what must change:
  gate failures or the critic's fixes; empty on the first call.
- ``render(branch) -> Sequence[Path]``: that variant's renders.
- ``gate(branch, renders) -> Sequence[str]``: mechanical failures, empty =
  pass. A failure goes back to the generator and is NOT a round; a render
  that failed the gate never reaches the critic.
- ``critic(brief, renders) -> Critique`` (phase 2's class), per variant.
- ``collage(variants) -> Path``: one image of the variants.
- ``deliver(text, images)``: the message to the owner's (gymbro) chat.

Stop: every variant ``ready``, 3 rounds, or the same fixes as last round.
The outcome has ``.stop`` (``ready`` | ``rounds`` | ``repeated``), ``.rounds``
and ``.variants`` (each with ``.branch`` and ``.notes``: the critic's
unresolved fixes). Owner 26.10 22:31: only ``ready`` reaches the owner as a
choice; a loop that stops without it sends a short "design failed: reasons"
note with no images and no pick.
"""

from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

import pytest

from src.designloop.brief import build_brief
from src.designloop.critic import CRITERIA, Critique

CARD = "gb-d001"
BRANCHES = tuple(f"design/{CARD}/v{n}" for n in (1, 2, 3))


def _criteria(fix: str = "") -> Dict[str, str]:
    out = {name: "ok" for name in CRITERIA}
    if fix:
        out["hierarchy"] = f"fix: {fix}"
    return out


def ready() -> Critique:
    return Critique(verdict="ready", criteria=_criteria(), score=9)


def revise(fix: str) -> Critique:
    return Critique(verdict="revise", criteria=_criteria(fix), score=5)


def branch_of(renders: Sequence[Path]) -> str:
    """The variant a render set belongs to: <tmp>/<vN>/a<attempt>/<theme>.png."""
    name = Path(renders[0]).parent.parent.name
    return f"design/{CARD}/{name}"


class Steps:
    """Fake steps. ``gate_fail`` = {(branch, render attempt): failures};
    ``verdicts(round, branch) -> Critique``."""

    def __init__(self, tmp: Path, verdicts: Any, gate_fail: Mapping = None):
        self.tmp = tmp
        self.verdicts = verdicts
        self.gate_fail = dict(gate_fail or {})
        self.feedback: List[Mapping[str, Sequence[str]]] = []
        self.renders: Counter = Counter()
        self.gated: List[Tuple[str, int, Tuple[str, ...]]] = []
        self.critiqued: List[Tuple[str, Tuple[Path, ...]]] = []
        self.collages: List[Any] = []
        self.delivered: List[Tuple[str, List[Path]]] = []
        self.critic_round = 0

    @property
    def generations(self) -> int:
        return len(self.feedback)

    async def generate(self, brief, feedback):
        self.feedback.append(dict(feedback or {}))
        return list(BRANCHES)

    async def render(self, branch):
        self.renders[branch] += 1
        attempt = self.renders[branch]
        folder = self.tmp / branch.rsplit("/", 1)[-1] / f"a{attempt}"
        folder.mkdir(parents=True, exist_ok=True)
        out = []
        for theme in ("dark", "light"):
            path = folder / f"{theme}.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + str(path).encode())
            out.append(path)
        return out

    async def gate(self, branch, renders):
        attempt = int(Path(renders[0]).parent.name[1:])
        failures = tuple(self.gate_fail.get((branch, attempt), ()))
        self.gated.append((branch, attempt, failures))
        return list(failures)

    async def critic(self, brief, renders):
        renders = tuple(Path(p) for p in renders)
        branch = branch_of(renders)
        self.critiqued.append((branch, renders))
        # One critic round = one critique per variant.
        n = (len(self.critiqued) - 1) // len(BRANCHES) + 1
        return self.verdicts(n, branch)

    async def collage(self, variants):
        self.collages.append(list(variants))
        path = self.tmp / "collage.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\ncollage")
        return path

    async def deliver(self, text, images=()):
        self.delivered.append((str(text), [Path(p) for p in images or ()]))

    def kwargs(self) -> Dict[str, Any]:
        return dict(
            generate=self.generate,
            render=self.render,
            gate=self.gate,
            critic=self.critic,
            collage=self.collage,
            deliver=self.deliver,
        )


@pytest.fixture
def brief(gymbro_repo):
    return build_brief(gymbro_repo, "feed", "Too many filters on top; reduce them")


async def run(brief, steps: Steps):
    from src.designloop.loop import run_loop

    return await run_loop(brief, **steps.kwargs())


def notes_of(outcome, branch: str) -> str:
    found = [v for v in outcome.variants if v.branch == branch]
    assert found, f"{branch} missing from the outcome: {outcome.variants}"
    return str(found[0].notes)


# --------------------------------------------------------------------------
# approved scenarios
# --------------------------------------------------------------------------


async def test_three_variants_pass_the_gate_and_ready_send_one_collage(brief, tmp_path):
    steps = Steps(tmp_path, verdicts=lambda n, b: ready())

    outcome = await run(brief, steps)

    assert outcome.stop == "ready"
    assert outcome.rounds == 1
    assert steps.generations == 1
    assert steps.feedback[0] == {}
    # Every variant gated, then critiqued once with its own renders.
    assert sorted(b for b, _, _ in steps.gated) == sorted(BRANCHES)
    assert sorted(b for b, _ in steps.critiqued) == sorted(BRANCHES)
    assert [v.branch for v in outcome.variants] == list(BRANCHES)
    # One collage of the three, one message carrying exactly that image.
    assert len(steps.collages) == 1 and len(steps.collages[0]) == 3
    assert len(steps.delivered) == 1
    text, images = steps.delivered[0]
    assert images == [tmp_path / "collage.png"]
    for n in ("1", "2", "3"):
        assert n in text


async def test_gate_failure_goes_back_to_the_generator_and_is_not_a_round(
    brief, tmp_path
):
    failure = "EN: «Create a joint workout» clipped at 390 px"
    steps = Steps(
        tmp_path,
        verdicts=lambda n, b: ready(),
        gate_fail={(BRANCHES[1], 1): [failure]},
    )

    outcome = await run(brief, steps)

    # The generator ran again and was told what the gate found.
    assert steps.generations == 2
    assert failure in str(steps.feedback[1])
    # Not a round: the critic said ready in its first (only) round.
    assert outcome.stop == "ready"
    assert outcome.rounds == 1
    # The critic never saw the render that failed the gate.
    for branch, renders in steps.critiqued:
        if branch == BRANCHES[1]:
            assert all(Path(p).parent.name != "a1" for p in renders), renders
    assert sorted(b for b, _ in steps.critiqued) == sorted(BRANCHES)
    assert len(steps.delivered) == 1 and steps.delivered[0][1]


async def test_revise_three_rounds_stops_with_notes_on_the_variant(brief, tmp_path):
    def verdicts(n: int, branch: str) -> Critique:
        return revise(f"round {n}: {branch} bell chip repeats the bell button")

    steps = Steps(tmp_path, verdicts=verdicts)

    outcome = await run(brief, steps)

    assert outcome.stop == "rounds"
    assert outcome.rounds == 3
    assert steps.generations == 3
    # Round 2 and 3 generations get the critic's fixes back.
    assert "round 1:" in str(steps.feedback[1])
    assert "round 2:" in str(steps.feedback[2])
    # The unresolved notes go with each variant.
    for branch in BRANCHES:
        assert f"round 3: {branch} bell chip" in notes_of(outcome, branch)
    # Only ready reaches the owner as a choice: no collage, no pick.
    assert steps.collages == []
    assert all(images == [] for _, images in steps.delivered)
    assert len(steps.delivered) == 1
    assert "bell chip repeats the bell button" in steps.delivered[0][0]


async def test_same_notes_twice_stops_early_with_notes_on_the_variant(brief, tmp_path):
    def verdicts(n: int, branch: str) -> Critique:
        return revise(f"{branch}: the Saved chip competes with the filters")

    steps = Steps(tmp_path, verdicts=verdicts)

    outcome = await run(brief, steps)

    assert outcome.stop == "repeated"
    assert outcome.rounds == 2
    assert steps.generations == 2
    for branch in BRANCHES:
        assert "Saved chip competes" in notes_of(outcome, branch)
    assert steps.collages == []
    assert all(images == [] for _, images in steps.delivered)


# --------------------------------------------------------------------------
# added
# --------------------------------------------------------------------------


async def test_ready_in_round_two_offers_the_variants_with_one_collage(brief, tmp_path):
    """revise then ready: the fixes go back, the second round is offered."""

    def verdicts(n: int, branch: str) -> Critique:
        return revise(f"{branch}: stats row misaligned") if n == 1 else ready()

    steps = Steps(tmp_path, verdicts=verdicts)

    outcome = await run(brief, steps)

    assert (outcome.stop, outcome.rounds, steps.generations) == ("ready", 2, 2)
    assert "stats row misaligned" in str(steps.feedback[1])
    assert len(steps.collages) == 1
    assert len(steps.delivered) == 1
    assert steps.delivered[0][1] == [tmp_path / "collage.png"]


def test_design_generate_job_route_is_claude_and_not_opus():
    """Owner 27.10 on 3.2: generation runs on a cheaper model, not opus."""
    from src.model_router.routes import CLAUDE_HARNESS, load_call_routes
    from src.model_router.types import JOB_ROUTE_NAMES

    assert "design_generate" in JOB_ROUTE_NAMES
    route = load_call_routes(None).resolve_job("design_generate")
    assert route.harness == CLAUDE_HARNESS
    assert route.model and "opus" not in route.model
