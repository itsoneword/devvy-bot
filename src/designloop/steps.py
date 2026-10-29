"""The design loop's real steps on a board repo (dv-9133 phase 3).

``taskpump design run`` builds a :class:`BoardSteps` and hands its methods to
:func:`src.designloop.loop.run_loop`:

- generate: one ``scripts/agentjob`` turn per variant on the
  ``design_generate`` job route (the one job mechanism, a model_calls row
  each), in its own worktree ``<worktrees>/<board>/<card>-vN`` on branch
  ``design/<card>/vN`` cut from the board repo's HEAD. The job edits the real
  app; this process commits what it left (jobs never write git, as in the
  pump).
- render / gate: the board's argv (``boards.json`` ``design`` block). The gate
  also fails, without the board's script, a variant with no commit against
  the current app or with no renders.
- critic: :func:`src.designloop.critic.critique` through the model router.
- collage: ``collage.py`` run by the board's python (Pillow lives there, not
  in the bot venv).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .brief import Brief
from .critic import Critique, critique
from .loop import Variant

COLLAGE_SCRIPT = Path(__file__).with_name("collage.py")
JOB_ROUTE = "design_generate"
VARIANTS = 3
NOTES_FILE = "DESIGN_NOTES.md"
TRADEOFF = "trade-off:"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class DesignConfig:
    """A board's ``design`` block.

    ``render``: argv, ``{worktree}`` / ``{out}`` / ``{screen}`` substituted;
    writes the variant's PNGs into ``{out}``. ``gate``: argv, ``{worktree}``
    / ``{renders}`` / ``{refs}`` / ``{screen}``; prints a JSON list of
    failures (or ``{"failures": [...]}``), ``[]`` = pass. ``python``: the
    board's python with Pillow, for the collage.
    """

    render: Tuple[str, ...]
    gate: Tuple[str, ...]
    python: str
    variants: int = VARIANTS


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
    return DesignConfig(
        render=tuple(render),  # type: ignore[arg-type]
        gate=tuple(gate),  # type: ignore[arg-type]
        python=python,
        variants=max(2, min(3, variants)),
    )


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
    ) -> None:
        self.repo = Path(repo)
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

    # -- git -----------------------------------------------------------------

    def branch(self, n: int) -> str:
        return f"design/{self.card}/v{n}"

    def worktree(self, branch: str) -> Path:
        return self.worktrees / f"{self.card}-{branch.rsplit('/', 1)[-1]}"

    def _git(self, *args: str, cwd: Optional[Path] = None) -> str:
        r = self.run(
            ["git", *args], cwd=str(cwd or self.repo), capture_output=True, text=True
        )
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:400]}")
        return str(r.stdout).strip()

    def prepare(self) -> List[str]:
        """One worktree per variant, cut from the board repo's HEAD (the
        current app). A rerun reuses them: their commits are the work."""
        self.base = self._git("rev-parse", "HEAD")
        branches = [self.branch(n) for n in range(1, self.config.variants + 1)]
        for branch in branches:
            path = self.worktree(branch)
            if (path / ".git").exists():
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
            else:
                self._git("worktree", "add", "-q", "-b", branch, str(path), self.base)
        return branches

    def _commit(self, branch: str, message: str) -> None:
        path = self.worktree(branch)
        if not self._git("status", "--porcelain", cwd=path):
            return
        self._git("add", "-A", cwd=path)
        self._git("commit", "-q", "-m", message, cwd=path)

    # -- the loop's steps ----------------------------------------------------

    async def generate(
        self, brief: Brief, feedback: Mapping[str, Sequence[str]]
    ) -> List[str]:
        branches = [self.branch(n) for n in range(1, self.config.variants + 1)]
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
            "BRIEF": brief.text,
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
        }
        text = self.template.read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        return text

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

    async def render(self, branch: str) -> List[Path]:
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
        argv = _fill(
            self.config.gate,
            {
                "worktree": str(self.worktree(branch)),
                "renders": str(folder),
                "refs": str(self.repo / "docs" / "screens" / self.screen),
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
        return await critique(brief, renders)

    async def describe(self, branch: str) -> str:
        notes = self.worktree(branch) / NOTES_FILE
        return tradeoff_of(notes.read_text(encoding="utf-8")) if notes.exists() else ""

    async def collage(self, variants: Sequence[Variant]) -> Path:
        path = self.out / "collage.png"
        cmd = [self.config.python, str(COLLAGE_SCRIPT), "--out", str(path)]
        for i, variant in enumerate(variants, 1):
            dark = [p for p in variant.renders if "dark" in p.name]
            tile = (dark or list(variant.renders))[0]
            cmd += [
                "--tile",
                str(tile),
                "--label",
                f"{i}. {variant.tradeoff or variant.name}",
            ]
        r = await asyncio.to_thread(self.run, cmd, capture_output=True, text=True)
        if r.returncode != 0 or not path.exists():
            raise RuntimeError(
                f"collage failed (rc {r.returncode}): {(r.stderr or '').strip()[-400:]}"
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
