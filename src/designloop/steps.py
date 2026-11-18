"""The design loop's real steps on a board repo (phase 3).

``pump2 design run`` builds a :class:`BoardSteps` and hands its methods to
:func:`src.designloop.loop.run_loop`:

- generate: one ``scripts/agentjob`` turn per variant on the
  ``design_generate`` job route (the one job mechanism, a model_calls row
  each), in its own worktree ``<worktrees>/<board>/<card>-vN`` on branch
  ``design/<card>/vN`` cut from the board repo's HEAD. The job edits the real
  app; this process commits what it left (jobs never write git, as in the
  pump).
- current: the board's render argv, once per run, on the base commit in a
  branchless worktree ``<worktrees>/<board>/<card>-current``, into
  ``<out>/current/``. That render, not ``docs/screens``, is the
  reference: the gate's ``{refs}``, the critic's reference images and the
  collage's tile 0 (same commit, language and size as the variants).
- render / gate: the board's argv (``boards.json`` ``design`` block). The gate
  also fails, without the board's script, a variant with no commit against
  the current app or with no renders.
- critic: :func:`src.designloop.critic.critique` through the model router.
- collage: ``review_pdf.py`` (a PDF and its cover PNG) run by the board's
  python (Pillow lives there).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import structlog

from ..pump2.template import fill_template
from .brief import Brief
from .critic import Critique, Remarks, critique, critique_mockup, critique_remarks
from .inventory import inventory_lines
from .loop import Variant, notes_of, scores_line
from .references import IMAGE_SUFFIXES, OwnerInputs, kit_paths, read_inputs
from .review_pdf import cover_path
from .review_pdf import item as review_item

log = structlog.get_logger(__name__)


def _variant_of(name: str) -> int:
    """The variant number of a renders folder ``vN``, 0 when it is not one."""
    return int(name[1:]) if name[:1] == "v" and name[1:].isdigit() else 0


REVIEW_SCRIPT = Path(__file__).with_name("review_pdf.py")
# Block remarks (dv-696e): the render's DOM sidecar, the crop script (board
# python), where the crops go (a subfolder: render / gate glob ``aN/*.png``)
# and the per-round critic answers.
CROPS_SCRIPT = Path(__file__).with_name("crops.py")
SIDECAR_SUFFIX = ".boxes.json"
CROPS_DIR = "critic"
CRITIC_JSON = "critic.json"
BLOCK_CALLS_MAX = 12
BLOCK_CALLS_PARALLEL = 4
JOB_ROUTE = "design_generate"
VARIANTS = 3
REFERENCE_IMAGES = 4  # default cap of owner reference images per critic call
REFERENCE_IMAGES_MAX = 8
KIT_MAX = 6  # UI kit images per critic call, outside ``reference_images``
COMPONENTS_MAX = 12  # ``design.components`` paths / globs
NOTES_FILE = "DESIGN_NOTES.md"
TRADEOFF = "trade-off:"
THRESHOLD = 7  # mockup mode: the lowest dimension needed for ready (5..9)
MOCKUP_PRESETS = ("best", "quality", "fast")
MOCKUP_COUNT = 4

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class MockupConfig:
    """``design.mockups``: step 0 on, FLUX ``preset``, ``count``
    mockups (3..4). The block is the board's opt-in: briefs go to a
    third-party server only for boards that have it."""

    preset: str = "best"
    count: int = MOCKUP_COUNT
    # a run whose paid mockups cost at most this many dollars
    # draws them on OpenRouter (fast) instead of FLUX; 0 = FLUX always.
    paid_max_usd: float = 0.0
    # "<service>:<NAME>" of the OpenRouter key paid mockups (and the FLUX
    # fallback) bill to; None = the default openrouter key.
    key: Optional[str] = None


@dataclass(frozen=True)
class DesignConfig:
    """A board's ``design`` block.

    ``render``: argv, ``{worktree}`` / ``{out}`` / ``{screen}`` substituted;
    writes the variant's PNGs into ``{out}``. ``gate``: argv, ``{worktree}``
    / ``{renders}`` / ``{refs}`` / ``{screen}``; prints a JSON list of
    failures (or ``{"failures": [...]}``), ``[]`` = pass. ``python``: the
    board's python with Pillow, for the collage.

    Owner inputs: ``references`` (path in the board repo, the owner's references
    in his words; None = off), ``reference_images`` (max images named in it
    attached to the critic, 0..8), ``verdicts`` (the owner's verdict log;
    None = ``owner-verdicts.md`` next to ``references``), ``kit`` (the
    board's UI kit images, generated once outside the loop: attached to the
    critic first, outside ``reference_images``, and named to the designer;
    at most 6). See :mod:`src.designloop.references`. ``components``
    (paths or globs in the board repo, at most 12): the kit component
    inventory named to the builder, :mod:`src.designloop.inventory`.

    Mockup mode: ``style_spec`` (path in the board repo, the
    style spec YAML, :mod:`src.designloop.stylespec`), ``mockups`` (step 0,
    :class:`MockupConfig`; None = off), ``threshold`` (5..9, the lowest
    dimension a ready render needs).
    """

    render: Tuple[str, ...]
    gate: Tuple[str, ...]
    python: str
    variants: int = VARIANTS
    references: Optional[str] = None
    reference_images: int = REFERENCE_IMAGES
    verdicts: Optional[str] = None
    kit: Tuple[str, ...] = ()
    style_spec: Optional[str] = None
    mockups: Optional[MockupConfig] = None
    threshold: int = THRESHOLD
    # None = the key is malformed: no inventory (optional, so the block
    # stays valid); ``pump2 doctor`` reports it.
    components: Optional[Tuple[str, ...]] = ()
    # the pictures root (dv-design-map): None = <board workspace>/design;
    # :mod:`src.designloop.assetmap`
    assets: Optional[str] = None


def _parse_mockups(raw: object) -> Optional[MockupConfig]:
    """``design.mockups``: ``{preset?, count?, paid_max_usd?}``; raises ValueError when
    malformed (the caller turns it into None for the whole block)."""
    if raw is None or raw is False:
        return None
    if raw is True:
        return MockupConfig()
    if not isinstance(raw, dict):
        raise ValueError("mockups")
    preset = raw.get("preset", "best")
    count = raw.get("count", MOCKUP_COUNT)
    if preset not in MOCKUP_PRESETS:
        raise ValueError("mockups.preset")
    if isinstance(count, bool) or not isinstance(count, int):
        raise ValueError("mockups.count")
    paid_max = raw.get("paid_max_usd", 0.0)
    if (
        isinstance(paid_max, bool)
        or not isinstance(paid_max, (int, float))
        or paid_max < 0
    ):
        raise ValueError("mockups.paid_max_usd")
    key = raw.get("key")
    if key is not None and (not isinstance(key, str) or ":" not in key):
        raise ValueError("mockups.key")
    return MockupConfig(
        preset=preset,
        count=max(3, min(4, count)),
        paid_max_usd=float(paid_max),
        key=key,
    )


def _parse_kit(raw: object) -> Optional[Tuple[str, ...]]:
    """``design.kit``: a list of at most 6 image paths; None = malformed."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > KIT_MAX:
        return None
    if not all(isinstance(x, str) and x.lower().endswith(IMAGE_SUFFIXES) for x in raw):
        return None
    return tuple(raw)


def _parse_components(raw: object) -> Optional[Tuple[str, ...]]:
    """``design.components``: at most 12 paths / globs; None = malformed."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or len(raw) > COMPONENTS_MAX:
        return None
    if not all(isinstance(x, str) and x for x in raw):
        return None
    return tuple(raw)


def parse_config(raw: object) -> Optional[DesignConfig]:
    """The ``design`` block, or None when absent or malformed."""
    if not isinstance(raw, dict):
        return None
    render, gate, python = raw.get("render"), raw.get("gate"), raw.get("python")
    argvs = (render, gate)
    if not all(
        isinstance(a, list) and a and all(isinstance(x, str) for x in a) for a in argvs
    ):
        return None
    if not isinstance(python, str) or not python:
        return None
    variants = raw.get("variants", VARIANTS)
    if isinstance(variants, bool) or not isinstance(variants, int):
        return None
    references, verdicts = raw.get("references"), raw.get("verdicts")
    if references is not None and not isinstance(references, str):
        return None
    if verdicts is not None and not isinstance(verdicts, str):
        return None
    images = raw.get("reference_images", REFERENCE_IMAGES)
    if isinstance(images, bool) or not isinstance(images, int):
        return None
    kit = _parse_kit(raw.get("kit"))
    if kit is None:
        return None
    components = _parse_components(raw.get("components"))
    style_spec = raw.get("style_spec")
    if style_spec is not None and not isinstance(style_spec, str):
        return None
    threshold = raw.get("threshold", THRESHOLD)
    if isinstance(threshold, bool) or not isinstance(threshold, int):
        return None
    if not 5 <= threshold <= 9:
        return None
    try:
        mockups = _parse_mockups(raw.get("mockups"))
    except ValueError:
        return None
    return DesignConfig(
        render=tuple(render),  # type: ignore[arg-type]
        gate=tuple(gate),  # type: ignore[arg-type]
        python=python,
        variants=max(2, min(3, variants)),
        references=references or None,
        reference_images=max(0, min(REFERENCE_IMAGES_MAX, images)),
        verdicts=verdicts or None,
        kit=kit,
        style_spec=style_spec or None,
        mockups=mockups,
        threshold=threshold,
        components=components,
        assets=raw.get("assets") if isinstance(raw.get("assets"), str) else None,
    )


def owner_inputs(config: DesignConfig, repo: Path) -> OwnerInputs:
    """The board's owner references, verdict log and reference images, read
    from the board repo's main checkout. ``FileNotFoundError`` when the
    configured references file is missing."""
    return read_inputs(
        repo, config.references, config.verdicts, config.reference_images, config.kit
    )


def missing_kit(config: DesignConfig, repo: Path) -> List[str]:
    """The ``design.kit`` entries absent from ``repo``: ``pump2 design run``
    refuses, ``pump2 doctor`` reports."""
    return [
        name
        for name, path in zip(config.kit, kit_paths(repo, config.kit))
        if not path.is_file()
    ]


def missing_scripts(config: DesignConfig, repo: Path) -> List[str]:
    """The render / gate ``.py`` arguments absent from ``repo`` (relative ones
    resolve against it): ``pump2 design run`` refuses, ``pump2 doctor`` reports."""
    return [
        a
        for a in (*config.render, *config.gate)
        if a.endswith(".py")
        and not (Path(a) if Path(a).is_absolute() else repo / a).exists()
    ]


def _fill(argv: Sequence[str], values: Mapping[str, str]) -> List[str]:
    out = []
    for arg in argv:
        for key, value in values.items():
            arg = arg.replace("{" + key + "}", value)
        out.append(arg)
    return out


def parse_gate_output(text: str) -> List[str]:
    """The board gate's stdout: a JSON list, or an object with ``failures``."""
    data = json.loads(text or "[]")
    if isinstance(data, dict):
        data = data.get("failures") or []
    if not isinstance(data, list):
        raise ValueError(f"gate output is not a list: {text[:200]!r}")
    return [str(x) for x in data if str(x).strip()]


def tradeoff_of(notes: str) -> str:
    """The ``Trade-off:`` line of DESIGN_NOTES.md, else its first line."""
    lines = [line.strip().lstrip("#-* ").strip() for line in notes.splitlines()]
    lines = [line for line in lines if line]
    for line in lines:
        if line.lower().startswith(TRADEOFF):
            return line[len(TRADEOFF) :].strip()[:90]
    return lines[0][:90] if lines else ""


class BoardSteps:
    """The loop's steps for one designloop card on one board repo."""

    def __init__(
        self,
        *,
        repo: Path,
        card: str,
        screen: str,
        worktrees: Path,
        out: Path,
        config: DesignConfig,
        profile: str,
        agentjob: Path,
        template: Path,
        ask_context: str = "",
        run: Runner = subprocess.run,
        restart: bool = False,
        board: str = "",
    ) -> None:
        self.repo = Path(repo)
        # dv-925f: the board name for the critic's judge rows; "" = none.
        self.board = board
        self._scores_warned = False
        self.card = card
        self.screen = screen
        self.worktrees = Path(worktrees)
        self.out = Path(out)
        self.config = config
        self.profile = profile
        self.agentjob = agentjob
        self.template = template
        self.ask_context = ask_context
        self.run = run
        self.base = ""
        self.attempts: Dict[str, int] = {}
        self.current_pngs: Optional[List[Path]] = None
        self.fresh: List[str] = []
        # A run on a card the owner rejected starts every variant
        # from the base: reworking the rejected ones anchors on them.
        self.restart = restart
        self._owner: Optional[OwnerInputs] = None
        # mockup mode: the chosen target mockup and the board's
        # approved style spec (file + text); set by the runner after step 0.
        self.mockup: Optional[Path] = None
        self.spec_file: Optional[Path] = None
        self.spec_text = ""

    def use_mockup(self, mockup: Path, spec_file: Path, spec_text: str) -> None:
        """Build to ``mockup``: one variant, the critic scores dimensions."""
        self.mockup = Path(mockup)
        self.spec_file = Path(spec_file)
        self.spec_text = spec_text

    @property
    def variant_count(self) -> int:
        """One variant in mockup mode (one target), else the config's."""
        return 1 if self.mockup is not None else self.config.variants

    # -- git -----------------------------------------------------------------

    def branch(self, n: int) -> str:
        return f"design/{self.card}/v{n}"

    def worktree(self, branch: str) -> Path:
        return self.worktrees / f"{self.card}-{branch.rsplit('/', 1)[-1]}"

    @property
    def current_worktree(self) -> Path:
        return self.worktrees / f"{self.card}-current"

    @property
    def current_dir(self) -> Path:
        return self.out / "current"

    def _git(self, *args: str, cwd: Optional[Path] = None) -> str:
        r = self.run(
            ["git", *args], cwd=str(cwd or self.repo), capture_output=True, text=True
        )
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:400]}")
        return str(r.stdout).strip()

    def prepare(self) -> List[str]:
        """One worktree per variant, cut from the board repo's HEAD (the
        current app). A rerun reuses them (their commits are the work) and
        merges the current base into each, so the variant's own render
        scripts are not stale; a variant whose merge fails starts fresh from
        the base and is listed in ``self.fresh``."""
        self.base = self._git("rev-parse", "HEAD")
        self.fresh = []
        self._scores_warned = False
        branches = [self.branch(n) for n in range(1, self.variant_count + 1)]
        for branch in branches:
            path = self.worktree(branch)
            if (path / ".git").exists():
                self._update(branch)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            exists = (
                self.run(
                    ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
                    cwd=str(self.repo),
                    capture_output=True,
                    text=True,
                ).returncode
                == 0
            )
            if exists:
                self._git("worktree", "add", "-q", str(path), branch)
                self._update(branch)
            else:
                self._git("worktree", "add", "-q", "-b", branch, str(path), self.base)
        current = self.current_worktree
        if (current / ".git").exists():
            self._git("checkout", "-q", "--detach", self.base, cwd=current)
        else:
            current.parent.mkdir(parents=True, exist_ok=True)
            self._git("worktree", "add", "-q", "--detach", str(current), self.base)
        return branches

    def _update(self, branch: str) -> None:
        """Merge the base into a reused variant; on failure reset it to the
        base (the variant worktree is the loop's own, old work stays in the
        reflog) and note it for the run summary. A restart (the owner
        rejected every variant) resets each one to the base."""
        path = self.worktree(branch)
        n = branch.rsplit("/", 1)[-1]
        self._commit(branch, f"design {self.card} {n}: leftovers before rerun")
        if self.restart:
            self._git("reset", "-q", "--hard", self.base, cwd=path)
            self._git("clean", "-q", "-fd", cwd=path)
            self.fresh.append(f"{n} started fresh: the owner rejected every variant")
            return
        merged = self.run(
            ["git", "merge", "--no-edit", "-q", self.base],
            cwd=str(path),
            capture_output=True,
            text=True,
        )
        if merged.returncode == 0:
            return
        self.run(
            ["git", "merge", "--abort"], cwd=str(path), capture_output=True, text=True
        )
        self._git("reset", "-q", "--hard", self.base, cwd=path)
        self._git("clean", "-q", "-fd", cwd=path)
        self.fresh.append(f"{n} started fresh: merging {self.base[:7]} conflicted")

    def _commit(self, branch: str, message: str) -> None:
        path = self.worktree(branch)
        if not self._git("status", "--porcelain", cwd=path):
            return
        self._git("add", "-A", cwd=path)
        self._git("commit", "-q", "-m", message, cwd=path)

    # -- the owner's references ----------------------------------------------

    def owner(self) -> OwnerInputs:
        """Read once per run (the log does not change during it)."""
        if self._owner is None:
            self._owner = owner_inputs(self.config, self.repo)
        return self._owner

    def with_owner(self, brief: Brief) -> Brief:
        """The brief plus the owner's references text and verdicts."""
        owner = self.owner()
        if not (owner.references or owner.verdicts):
            return brief
        return dataclasses.replace(
            brief, owner_references=owner.references, owner_verdicts=owner.verdicts
        )

    # -- the loop's steps ----------------------------------------------------

    async def generate(
        self, brief: Brief, feedback: Mapping[str, Sequence[str]]
    ) -> List[str]:
        branches = [self.branch(n) for n in range(1, self.variant_count + 1)]
        first = not feedback
        for n, branch in enumerate(branches, 1):
            if not first and branch not in feedback:
                continue  # passed, or ready: left as it is
            prompt = self.prompt(brief, n, branches, feedback.get(branch, ()))
            await asyncio.to_thread(self._agentjob, branch, prompt)
            why = "first draft" if first else "; ".join(feedback[branch])[:200]
            self._commit(branch, f"design {self.card} v{n}: {why}")
        return branches

    def prompt(
        self, brief: Brief, n: int, branches: Sequence[str], fixes: Sequence[str]
    ) -> str:
        others = []
        for other in branches:
            if other == branches[n - 1]:
                continue
            notes = self.worktree(other) / NOTES_FILE
            line = (
                tradeoff_of(notes.read_text(encoding="utf-8")) if notes.exists() else ""
            )
            if line:
                others.append(f"- {other.rsplit('/', 1)[-1]}: {line}")
        values = {
            "BRIEF": self.with_owner(brief).text,
            "VARIANT": str(n),
            "COUNT": str(len(branches)),
            "BRANCH": branches[n - 1],
            "WORKTREE": str(self.worktree(branches[n - 1])),
            "NOTES_FILE": NOTES_FILE,
            "OTHERS": "\n".join(others) or "(none written yet)",
            "FEEDBACK": (
                "\n".join(f"- {fix}" for fix in fixes)
                if fixes
                else "(first draft: no feedback yet)"
            ),
            "KIT": self.kit_section(branches[n - 1]),
            "COMPONENTS": self.components_section(branches[n - 1]),
            "MOCKUP": self.mockup_section(),
        }
        return fill_template(self.template.read_text(encoding="utf-8"), values)

    def kit_section(self, branch: str) -> str:
        """The designer prompt's UI kit section ("" without a kit). A kit
        file committed in the variant's worktree is named there, else in the
        board repo's main checkout."""
        if not self.config.kit:
            return ""
        wt = self.worktree(branch)
        lines = []
        for name, path in zip(self.config.kit, kit_paths(self.repo, self.config.kit)):
            local = wt / name
            shown = local if not Path(name).is_absolute() and local.is_file() else path
            lines.append(f"- `{shown}`")
        return (
            "\n## UI kit (the owner's approved components)\n\n"
            "Open these images and match their components (buttons, chips, "
            "tabs, cards, badges) in the app's code; the shipped UI stays "
            "code, never an image:\n\n" + "\n".join(lines) + "\n"
        )

    def components_section(self, branch: str) -> str:
        """The builder prompt's kit component inventory ("" without
        ``design.components``), read from the variant's worktree when it
        exists, else the board repo's main checkout."""
        if not self.config.components:
            return ""
        wt = self.worktree(branch)
        root = wt if wt.is_dir() else self.repo
        lines = inventory_lines(root, self.config.components)
        if not lines:
            return ""
        return (
            "\n## Kit components (from the code; build with these before "
            "writing a new one)\n\n" + "\n".join(lines) + "\n"
        )

    def mockup_section(self) -> str:
        """The designer prompt's target mockup section ("" without one)."""
        if self.mockup is None:
            return ""
        spec = f" and the style spec `{self.spec_file}`" if self.spec_file else ""
        return (
            "\n## Target mockup (step 0, the one picked)\n\n"
            f"Open `{self.mockup}`{spec}. Build the screen in the app's code to "
            "this mockup: its layout, components and look, with the spec's exact "
            "colours, type, radii, shadows and spacing. Texts come from the brief "
            "(the mockup's lettering may be garbled). The shipped UI stays code, "
            "never an image.\n"
        )

    def _agentjob(self, branch: str, prompt: str) -> None:
        path = self.worktree(branch)
        prompt_file = self.out / f"{branch.rsplit('/', 1)[-1]}-prompt.md"
        prompt_file.parent.mkdir(parents=True, exist_ok=True)
        prompt_file.write_text(prompt, encoding="utf-8")
        cmd = [
            str(self.agentjob),
            "--profile",
            self.profile,
            "--prompt-file",
            str(prompt_file),
            "--cwd",
            str(path),
            "--route",
            JOB_ROUTE,
            "--name",
            f"design-{self.card}-{branch.rsplit('/', 1)[-1]}",
            "--setting-sources",
            "project",
        ]
        if self.ask_context:
            cmd += ["--ask-context", self.ask_context]
        r = self.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(
                f"generator for {branch} failed (rc {r.returncode}): "
                f"{(r.stderr or r.stdout or '').strip()[-400:]}"
            )

    def _render_current(self) -> List[Path]:
        """The current app at ``self.base``, rendered once per run."""
        if self.current_pngs is not None:
            return self.current_pngs
        out = self.current_dir
        out.mkdir(parents=True, exist_ok=True)
        for old in out.glob("*.png"):
            old.unlink()
        cwd = self.current_worktree
        argv = _fill(
            self.config.render,
            {"worktree": str(cwd), "out": str(out), "screen": self.screen},
        )
        r = self.run(argv, cwd=str(cwd), capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(
                f"render of the current app failed (rc {r.returncode}): "
                f"{(r.stderr or r.stdout or '').strip()[-400:]}"
            )
        pngs = sorted(out.glob("*.png"))
        if not pngs:
            raise RuntimeError("render of the current app produced no PNG")
        self.current_pngs = pngs
        return pngs

    async def current(self) -> List[Path]:
        return await asyncio.to_thread(self._render_current)

    async def render(self, branch: str) -> List[Path]:
        await self.current()
        self.attempts[branch] = self.attempts.get(branch, 0) + 1
        out = self.out / branch.rsplit("/", 1)[-1] / f"a{self.attempts[branch]}"
        out.mkdir(parents=True, exist_ok=True)
        argv = _fill(
            self.config.render,
            {
                "worktree": str(self.worktree(branch)),
                "out": str(out),
                "screen": self.screen,
            },
        )
        r = await asyncio.to_thread(
            self.run,
            argv,
            cwd=str(self.worktree(branch)),
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"render of {branch} failed (rc {r.returncode}): "
                f"{(r.stderr or r.stdout or '').strip()[-400:]}"
            )
        return sorted(out.glob("*.png"))

    async def gate(self, branch: str, renders: Sequence[Path]) -> List[str]:
        """Mechanical only, no model. The built-in checks first: a variant
        must be a commit on the real app and must have renders."""
        changed = self._git("rev-list", "--count", f"{self.base}..{branch}")
        if not self.base or changed in ("", "0"):
            return [
                f"{branch}: no commit against the current app (not a real-app variant)"
            ]
        if not renders:
            return [f"{branch}: the render produced no PNG"]
        folder = Path(renders[0]).parent
        await self.current()
        argv = _fill(
            self.config.gate,
            {
                "worktree": str(self.worktree(branch)),
                "renders": str(folder),
                "refs": str(self.current_dir),
                "screen": self.screen,
            },
        )
        r = await asyncio.to_thread(
            self.run,
            argv,
            cwd=str(self.worktree(branch)),
            capture_output=True,
            text=True,
        )
        try:
            return parse_gate_output(str(r.stdout).strip())
        except ValueError as exc:
            # A gate that cannot say is a broken gate, not a failed variant.
            raise RuntimeError(
                f"gate of {branch} gave no JSON (rc {r.returncode}): {exc}; "
                f"{(r.stderr or '').strip()[-300:]}"
            ) from exc

    async def critic(self, brief: Brief, renders: Sequence[Path]) -> Critique:
        """The current render is the "before"; ``brief.refs`` (docs/screens)
        only when it left no PNG. With board references, the owner's text,
        verdicts and reference images are what the critic judges against.

        Mockup mode (dv-696e): renders with a DOM sidecar are judged as
        copies cut to the content height, plus one block-remarks call per
        sidecar block (crop of the render, mockup whole); without sidecars
        one remarks call on the whole renders. Every round's answers go to
        ``<renders dir>/critic.json``."""
        current = sorted(self.current_dir.glob("*.png"))
        if current:
            brief = dataclasses.replace(brief, refs=tuple(current))
        brief = self.with_owner(brief)
        owner = self.owner()
        if self.mockup is None:
            result = await critique(
                brief, renders, reference_images=owner.images, kit_images=owner.kit
            )
            self._save_critic(renders, result, [])
            return result
        pages, blocks, skipped = await asyncio.to_thread(self._crops, renders)
        result = await critique_mockup(
            brief,
            pages,
            mockup=self.mockup,
            spec_text=self.spec_text,
            threshold=self.config.threshold,
            kit_images=owner.kit,
        )
        limit = asyncio.Semaphore(BLOCK_CALLS_PARALLEL)
        mockup = self.mockup

        async def one(block: str, images: Sequence[Path]) -> Remarks:
            async with limit:
                return await critique_remarks(
                    brief,
                    images,
                    mockup=mockup,
                    spec_text=self.spec_text,
                    block=block,
                    kit_images=owner.kit,
                )

        calls = (
            [one(block, [png]) for block, png in blocks.items()]
            if blocks
            else [one("", pages)]
        )
        answers = list(await asyncio.gather(*calls))
        result = dataclasses.replace(
            result, remarks=tuple(r for a in answers for r in a.remarks)
        )
        self._save_critic(renders, result, answers, skipped)
        return result

    def _crops(
        self, renders: Sequence[Path]
    ) -> Tuple[List[Path], Dict[str, Path], List[str]]:
        """(page images for the dimensions call, block id -> crop, skipped).
        Renders without a sidecar go as they are; blocks come from the dark
        renders (all renders when none is dark), each id once."""
        pages: List[Path] = []
        blocks: Dict[str, Path] = {}
        skipped: List[str] = []
        dark = {p for p in renders if "dark" in Path(p).name} or set(renders)
        for png in renders:
            png = Path(png)
            sidecar = png.with_name(png.stem + SIDECAR_SUFFIX)
            if not sidecar.exists():
                pages.append(png)
                continue
            cmd = [
                self.config.python,
                str(CROPS_SCRIPT),
                "--png",
                str(png),
                "--sidecar",
                str(sidecar),
                "--out",
                str(png.parent / CROPS_DIR),
            ]
            r = self.run(cmd, capture_output=True, text=True)
            try:
                if r.returncode != 0:
                    raise ValueError(f"rc {r.returncode}")
                made = json.loads(str(r.stdout))
            except ValueError as exc:
                raise RuntimeError(
                    f"crops of {png.name} failed ({exc}): "
                    f"{(r.stderr or '').strip()[-400:]}"
                ) from exc
            pages.append(Path(made["page"]))
            for why in made.get("skipped") or []:
                skipped.append(f"{png.name}: {why}")
                log.warning("design_critic_crop_skipped", render=str(png), why=why)
            if png in dark:
                for block, path in (made.get("blocks") or {}).items():
                    if block not in blocks and len(blocks) < BLOCK_CALLS_MAX:
                        blocks[block] = Path(path)
        return pages, blocks, skipped

    def _save_critic(
        self,
        renders: Sequence[Path],
        result: Critique,
        answers: Sequence[Remarks],
        skipped: Sequence[str] = (),
    ) -> None:
        """``critic.json`` beside the round's renders: what the critic saw,
        answered and what went back to the builder."""
        if not renders:
            return
        folder = Path(renders[0]).parent
        name = folder.name
        rnd = int(name[1:]) if name[:1] == "a" and name[1:].isdigit() else 0
        data = {
            "round": rnd,
            "renders": [str(p) for p in renders],
            "images": {
                "critic": list(result.images),
                "remarks": [list(a.images) for a in answers],
            },
            "answers": {"critic": result.raw, "remarks": [a.raw for a in answers]},
            "verdict": result.verdict,
            "score": result.score,
            "scores": {k: n for k, (n, _) in result.dimensions.items()},
            "dimensions": {
                k: {"score": n, "evidence": ev}
                for k, (n, ev) in result.dimensions.items()
            },
            "criteria": dict(result.criteria),
            "remarks": [dataclasses.asdict(r) for r in result.remarks],
            "data_remarks": sum(r.kind == "data" for r in result.remarks),
            "skipped_blocks": list(skipped),
            "notes": list(notes_of(result)),
        }
        (folder / CRITIC_JSON).write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        self._store_critic(result, rnd, _variant_of(folder.parent.name))

    def _store_critic(self, result: Critique, rnd: int, variant: int) -> None:
        """The round's judge rows (dv-925f) in ``message_reactions``: one
        ``design_critic`` row per dimension and one ``overall`` (score, the
        verdict), one ``design_critic_remark`` row per block remark. A failed
        write is printed and noted in ``self.fresh`` once per run."""
        if not self.board:
            return
        from ..pump2.criteria import EVIDENCE_CAP, scores_db
        from ..storage.reactions import StageRow, record_stage_scores_sync

        at = {"round": rnd, "variant": variant}
        rows: List[StageRow] = [
            (k, n, (ev or "").strip()[:EVIDENCE_CAP] or None, at)
            for k, (n, ev) in result.dimensions.items()
        ]
        rows.append(("overall", result.score, result.verdict or None, at))
        for r in result.remarks:
            text = f"mockup = {r.mockup}; render = {r.render}; change = {r.change}"
            extras = {
                **at,
                "stage": "design_critic_remark",
                "element": r.element or None,
                "remark_kind": r.kind or None,
            }
            rows.append((r.block, None, text[:EVIDENCE_CAP], extras))
        try:
            err = record_stage_scores_sync(
                scores_db(),
                board=self.board,
                card_id=self.card,
                stage="design_critic",
                scores=rows,
                size=None,
                kind=None,
            )
        except Exception as exc:  # noqa: BLE001 - a judge row never stops a run
            err = str(exc)
        if err and not self._scores_warned:
            self._scores_warned = True
            line = f"warning: critic scores not stored: {err}"
            print(line)
            self.fresh.append(line)
            log.warning("design_critic_scores_not_stored", card=self.card, error=err)

    async def describe(self, branch: str) -> str:
        notes = self.worktree(branch) / NOTES_FILE
        return tradeoff_of(notes.read_text(encoding="utf-8")) if notes.exists() else ""

    async def collage(self, variants: Sequence[Variant]) -> Path:
        """The review PDF of the variants (``review_pdf.py`` run by the
        board's python) and, next to it, its cover PNG. Mockup mode: one
        compare pair per variant (target mockup | render); else a grid, the
        current screen first and unnumbered. Returns the PDF."""
        path = self.out / "review.pdf"
        items: List[Dict[str, Any]] = []
        for i, variant in enumerate(variants, 1):
            dark = [p for p in variant.renders if "dark" in p.name]
            tile = (dark or list(variant.renders))[0]
            caption = variant.tradeoff or variant.name
            if variant.critique is not None and variant.critique.dimensions:
                caption += "\n" + scores_line(variant.critique)
            if self.mockup is not None:
                items.append(review_item(self.mockup, "target mockup", label="mockup"))
                items.append(review_item(tile, caption, label=f"variant {i}"))
            else:
                items.append(review_item(tile, caption))
        sections: List[Dict[str, Any]] = []
        current = sorted(self.current_dir.glob("*.png"))
        title = f"Design variants {self.card} {self.screen}"
        if self.mockup is not None:
            sections.append(
                {
                    "heading": "Target mockup vs variants",
                    "layout": "compare",
                    "items": items,
                }
            )
        else:
            if current:
                dark = [p for p in current if "dark" in p.name]
                sections.append(
                    {
                        "heading": "Current screen",
                        "layout": "single",
                        "numbered": False,
                        "items": [review_item((dark or current)[0], "current")],
                    }
                )
            sections.append(
                {
                    "heading": "Variants",
                    "layout": "grid",
                    "columns": 3,
                    "note": "Answer with the variant number.",
                    "items": items,
                }
            )
        spec_file = self.out / "review-spec.json"
        self.out.mkdir(parents=True, exist_ok=True)
        spec_file.write_text(
            json.dumps({"title": title, "sections": sections}, ensure_ascii=False),
            encoding="utf-8",
        )
        cmd = [self.config.python, str(REVIEW_SCRIPT), "--spec", str(spec_file)]
        cmd += ["--out", str(path), "--cover", str(cover_path(path))]
        r = await asyncio.to_thread(self.run, cmd, capture_output=True, text=True)
        if r.returncode != 0 or not path.exists():
            raise RuntimeError(
                f"review pdf failed (rc {r.returncode}): "
                f"{(r.stderr or '').strip()[-400:]}"
            )
        return path

    def kwargs(self) -> Dict[str, Any]:
        """Every step but ``deliver``, which is the caller's."""
        return dict(
            generate=self.generate,
            render=self.render,
            gate=self.gate,
            critic=self.critic,
            collage=self.collage,
            describe=self.describe,
        )
