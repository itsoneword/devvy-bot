"""Pump v2 state: boards, cards, the one ``pump2:`` frontmatter key, comments.

A card is a ``.deviz/tasks/<id>.md`` file. This module changes only the
``pump2:`` block of its frontmatter and appends ``### @pump2`` comment
sections to the body; every other byte stays as it was. No database.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .criteria import KINDS, SIZES, TOUCHES

KEY = "pump2"
WORKTREES = ".pump-worktrees"
STAGES = ("analyze", "tests", "build", "review", "merge", "mergefix", "done", "stopped")
# the stages a board's `pipeline` may list by name (mergefix is merge's fallback)
BUILTIN_PIPELINE = ("analyze", "tests", "build", "review", "merge")
RESERVED = frozenset(STAGES) | {"waiting_user"}
_CUSTOM_NAME = re.compile(r"[a-z0-9]+")
_SPEC_KEYS = {"stage", "prompt", "report", "writes", "return_to", "when"}
# `when` keys of a custom stage and their closed value lists ("labels": the
# board's `stage_labels`); AND across keys, OR within one
WHEN_VALUES: dict[str, tuple[str, ...]] = {
    "touches": TOUCHES,
    "size": SIZES,
    "kind": KINDS,
}


@dataclass(frozen=True)
class StageSpec:
    """A board's custom stage (dv-3b99): a prompt job whose ``.pump/<report>``
    carries a review-style verdict; ``return`` sends the card to ``return_to``."""

    stage: str
    prompt: Path
    report: str
    writes: bool
    return_to: str
    # {touches|size|kind|labels: [values]}; {} = always runs
    when: dict[str, list[str]] = field(default_factory=dict)


PipelineEntry = str | StageSpec
RUN_STAGES = STAGES[:-2]  # the stages a card can stop in (and restart at)


@dataclass
class Board:
    name: str
    repo: Path
    profile: str = "devops"
    test: str = ""  # pump2_test, else the board's `test`
    test_core: str = ""  # pump2_test_core
    branch: str = "main"  # the integration branch
    mem: str = ""  # merge.mem, else PUMP_TEST_MEMORY_MAX / testrun default
    python: str = ""  # pump2_python, else derived from the test command
    deploy_log: str = ""  # deploy.log: deploy.py's journal (the pin's prod)
    # argv run in the card worktree after each merge of the integration branch
    # into the card branch, before the tests; [] = none
    after_sync: list[str] = field(default_factory=list)
    # pump2_test_files: command template for the build gate, {files} = the
    # shell-quoted tests.json paths; "" = `<python> -m pytest <files>`
    test_files: str = ""
    # the board's raw `design` block (render / gate argv, python): the
    # designloop runner's config, parsed by src/designloop/steps.parse_config
    design: Any = None
    # `pipeline`: the ordered stages (built-in names and StageSpec); [] = the
    # built-in order (analyze, tests, build, review, merge)
    pipeline: list[PipelineEntry] = field(default_factory=list)
    # the closed list of card labels a stage's `when.labels` may name
    stage_labels: list[str] = field(default_factory=list)

    def custom(self, stage: str) -> StageSpec | None:
        for e in self.pipeline:
            if isinstance(e, StageSpec) and e.stage == stage:
                return e
        return None

    def stage_names(self) -> list[str]:
        return [e if isinstance(e, str) else e.stage for e in self.pipeline]

    def test_python(self) -> str:
        """The board venv's python: pump2_python, else the first token of the
        test command when it ends with /pytest (replaced by /python)."""
        if self.python:
            return self.python
        first = (self.test.split() or [""])[0]
        if first.endswith("/pytest"):
            return first[: -len("pytest")] + "python"
        return "python3"


HOME_REPO = Path(__file__).resolve().parents[2]
HOME_BOARD = "devvy"


def _board(b: dict[str, Any], name: str, repo: Path) -> Board:
    return Board(
        name=name,
        repo=repo,
        profile=b.get("profile", "devops"),
        test=b.get("pump2_test", b.get("test", "")),
        test_core=b.get("pump2_test_core", ""),
        branch=b.get("pump2_branch", "main"),
        mem=(b.get("merge") or {}).get("mem", ""),
        python=b.get("pump2_python", ""),
        deploy_log=str((b.get("deploy") or {}).get("log") or ""),
        after_sync=_argv(b.get("after_sync")),
        test_files=str(b.get("pump2_test_files") or ""),
        design=b.get("design"),
        pipeline=_pipeline(
            b.get("pipeline"), name, repo, _stage_labels(b.get("stage_labels"), name)
        ),
        stage_labels=_stage_labels(b.get("stage_labels"), name),
    )


def _stage_labels(raw: object, board: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) and x for x in raw):
        raise ValueError(
            f"board {board!r}: stage_labels must be a list of label names, "
            f"got {raw!r}"
        )
    return list(raw)


def _pipeline(
    raw: object, board: str, repo: Path, labels: list[str]
) -> list[PipelineEntry]:
    """The board's `pipeline`, checked at load; any broken rule is a loud
    ValueError naming the board and the entry (dv-3b99)."""
    if raw is None:
        return []

    def bad(msg: str) -> ValueError:
        return ValueError(f"board {board!r}: pipeline: {msg}")

    if not isinstance(raw, list):
        raise bad(f"must be a list of stages, got {raw!r}")
    out: list[PipelineEntry] = []
    seen: list[str] = []
    for e in raw:
        if isinstance(e, str):
            if e not in BUILTIN_PIPELINE:
                raise bad(
                    f"unknown stage {e!r}; built-in stages are "
                    f"{list(BUILTIN_PIPELINE)}, a custom one is an object"
                )
            name: str = e
            out.append(e)
        elif isinstance(e, dict):
            spec = _stage_spec(e, seen, repo, bad, labels)
            name = spec.stage
            out.append(spec)
        else:
            raise bad(f"entry {e!r} is neither a stage name nor an object")
        if name in seen:
            raise bad(f"stage {name!r} is listed twice")
        seen.append(name)
    if not seen or seen[0] != "analyze":
        raise bad(f"must start with 'analyze', got {seen[:1]}")
    if seen[-1] != "merge":
        raise bad(f"must end with 'merge', got {seen[-1:]}")
    if "build" not in seen:
        raise bad("must contain 'build'")
    if "review" in seen:
        after = seen[seen.index("review") :]
        late = [
            x.stage
            for x in out
            if isinstance(x, StageSpec) and x.writes and x.stage in after
        ]
        if late:
            raise bad(
                f"stage {late[0]!r} has writes: true at/after 'review'; only "
                "stages before review may write (nothing would review it)"
            )
    return out


def _stage_spec(
    e: dict[str, Any],
    seen: list[str],
    repo: Path,
    bad: Callable[[str], ValueError],
    labels: list[str],
) -> StageSpec:
    name = e.get("stage")
    if not isinstance(name, str) or not _CUSTOM_NAME.fullmatch(name):
        raise bad(f"custom stage name {name!r} must match [a-z0-9]+")
    if name in RESERVED:
        raise bad(f"custom stage {name!r} clashes with a built-in stage name")
    extra = set(e) - _SPEC_KEYS
    if extra:
        raise bad(f"stage {name!r}: unknown keys {sorted(extra)}")
    prompt = e.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise bad(f"stage {name!r}: `prompt` (a file path) is required")
    path = Path(prompt) if Path(prompt).is_absolute() else repo / prompt
    if not path.is_file():
        raise bad(f"stage {name!r}: prompt file {str(path)!r} does not exist")
    report = e.get("report", f"{name}.md")
    if not isinstance(report, str) or not report or Path(report).name != report:
        raise bad(f"stage {name!r}: `report` must be a file name in .pump/")
    writes = e.get("writes", False)
    if not isinstance(writes, bool):
        raise bad(f"stage {name!r}: `writes` must be true or false")
    back = e.get("return_to")
    if back not in seen:
        raise bad(
            f"stage {name!r}: return_to {back!r} must be a stage listed "
            f"before it ({seen})"
        )
    when = _stage_when(e.get("when"), name, labels, bad)
    return StageSpec(name, path, report, writes, str(back), when)


def _stage_when(
    raw: object, name: str, labels: list[str], bad: Callable[[str], ValueError]
) -> dict[str, list[str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise bad(f"stage {name!r}: `when` must be an object, got {raw!r}")
    allowed = {**WHEN_VALUES, "labels": tuple(labels)}
    out: dict[str, list[str]] = {}
    for key, vals in raw.items():
        if key not in allowed:
            raise bad(
                f"stage {name!r}: unknown `when` key {key!r}; allowed: "
                f"{sorted(allowed)}"
            )
        if not isinstance(vals, list) or not vals:
            raise bad(f"stage {name!r}: when.{key} must be a non-empty list")
        wrong = [v for v in vals if v not in allowed[key]]
        if wrong:
            where = "the board's stage_labels" if key == "labels" else "allowed"
            raise bad(
                f"stage {name!r}: when.{key} has {wrong}, not in {where} "
                f"{list(allowed[key])}"
            )
        out[key] = list(vals)
    return out


def _argv(v: object) -> list[str]:
    """A non-empty list of strings, else [] (a bad value is ignored)."""
    if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
        return list(v)
    return []


def load_boards(path: Path, *, guard: bool = False) -> dict[str, Board]:
    """``guard``: keep only boards with ``"pump2": true`` (the default boards
    file); an explicit ``--boards`` file (proofs) is taken whole.

    This repo's own board is not an entry of ``boards`` (the old pump has it
    built in and would serve a listed copy twice): it is the top-level
    ``pump2_home`` object of the same file (profile, pump2_test, pump2_python,
    pump2_branch), which the old pump ignores. Present = enabled.
    ``boards`` is a list of entries with ``name``, or a ``{name: entry}`` map."""
    data = json.loads(path.read_text())
    out: dict[str, Board] = {}
    home = data.get("pump2_home")
    if isinstance(home, dict):
        out[HOME_BOARD] = _board(home, HOME_BOARD, HOME_REPO)
    entries = data.get("boards", [])
    if isinstance(entries, dict):  # also accept {name: entry}
        entries = [{"name": k, **v} for k, v in entries.items()]
    for b in entries:
        if guard and b.get("pump2") is not True:
            continue
        out[b["name"]] = _board(b, b["name"], Path(b["repo"]))
    return out


def worktree_path(board: Board, card_id: str) -> Path:
    return board.repo / WORKTREES / card_id


@dataclass
class Card:
    path: Path
    fm: dict[str, Any]
    body: str

    @property
    def id(self) -> str:
        return str(self.fm.get("id") or self.path.stem)

    @property
    def pump2(self) -> dict[str, Any]:
        v = self.fm.get(KEY)
        return v if isinstance(v, dict) else {}


def _split(text: str) -> tuple[list[str], str]:
    """(frontmatter lines without the fences, body). Raises on no frontmatter."""
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        raise ValueError("no frontmatter")
    end = lines.index("---", 1)
    return lines[1:end], "\n".join(lines[end + 1 :])


_KEY_LINE = re.compile(r"^([A-Za-z_][\w-]*)\s*:(?:\s+(.*))?$")


def parse_frontmatter(lines: list[str]) -> dict[str, Any]:
    """Line-wise, like the deviz format: a top-level ``key: rest`` line takes
    ``rest`` as YAML when that parses (lists, dicts, quoted, numbers, nested
    blocks) and as a raw string when it does not (``title: a: b``). A nested
    block (``key:`` + indented lines) must be valid YAML, else ValueError."""
    fm: dict[str, Any] = {}
    i = 0
    while i < len(lines):
        ln = lines[i]
        i += 1
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        m = _KEY_LINE.match(ln)
        if not m:
            raise ValueError(f"frontmatter line not understood: {ln!r}")
        key, rest = m.group(1), (m.group(2) or "").strip()
        j = i
        while j < len(lines) and (
            not lines[j].strip()
            or lines[j].startswith((" ", "\t"))
            or (not rest and lines[j].startswith("- "))
        ):
            j += 1
        chunk = [ln, *lines[i:j]]
        i = j
        try:
            val = yaml.safe_load("\n".join(chunk))
            fm[key] = val[key] if isinstance(val, dict) and key in val else val
        except yaml.YAMLError as e:
            if not rest:
                raise ValueError(f"frontmatter key {key!r}: {e}") from e
            fm[key] = " ".join(x.strip() for x in [rest, *chunk[1:]] if x.strip())
    return fm


def read_card(path: Path) -> Card:
    fm_lines, body = _split(path.read_text())
    return Card(path=path, fm=parse_frontmatter(fm_lines), body=body)


def list_cards(board: Board) -> tuple[list[Card], list[tuple[Path, str]]]:
    """(cards, [(file, one-line error)] for the files that did not parse,
    or the one tasks dir when it is missing)."""
    d = board.repo / ".deviz" / "tasks"
    cards: list[Card] = []
    bad: list[tuple[Path, str]] = []
    if not d.is_dir():
        # a board without its tasks dir: say so, not an empty status
        hint = f"missing .deviz/tasks/ -- run `mkdir -p {d}` (pump2 doctor)"
        return cards, [(d, hint)]
    for p in sorted(d.glob("*.md")):
        try:
            cards.append(read_card(p))
        except ValueError as e:
            bad.append((p, " ".join(str(e).split())))
    return cards, bad


def find_card(board: Board, card_id: str) -> Card:
    p = board.repo / ".deviz" / "tasks" / f"{card_id}.md"
    if not p.exists():
        raise FileNotFoundError(f"no card {card_id} in {p.parent}")
    return read_card(p)


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def set_pump2(path: Path, **changes: Any) -> dict[str, Any]:
    """Merge ``changes`` into the card's pump2 block (None removes a key) and
    rewrite only that block. Returns the new state."""
    text = path.read_text()
    fm_lines, body = _split(text)
    kept: list[str] = []
    skipping = False
    for ln in fm_lines:
        if re.match(rf"{KEY}\s*:", ln):
            skipping = True
            continue
        if skipping and (ln.startswith((" ", "\t")) or not ln.strip()):
            continue
        skipping = False
        kept.append(ln)
    cur = parse_frontmatter(fm_lines)
    state = dict(cur.get(KEY) or {}) if isinstance(cur.get(KEY), dict) else {}
    if "stage" in changes and changes["stage"] != state.get("stage"):
        changes.setdefault("since", now_iso())
        # every stop path records where it stopped: `pump2 start` restarts there
        if changes["stage"] == "stopped" and state.get("stage") in RUN_STAGES:
            changes.setdefault("stopped_at", state["stage"])
    for k, v in changes.items():
        if v is None:
            state.pop(k, None)
        else:
            state[k] = v
    block = [f"{KEY}:"] + [f"  {k}: {json.dumps(v)}" for k, v in state.items()]
    out = "\n".join(["---", *kept, *block, "---"]) + "\n" + body
    path.write_text(out)
    return state


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    tmp.replace(path)


def set_keys(path: Path, **keys: str) -> None:
    """Rewrite (or append) top-level one-line frontmatter keys, raw values;
    every other line stays. For keys outside the ``pump2:`` block (the
    designloop runner's ``design_*``). Written atomically."""
    lines = path.read_text().split("\n")
    end = lines.index("---", 1)
    for key, value in keys.items():
        for i in range(1, end):
            if re.match(rf"{re.escape(key)}\s*:", lines[i]):
                lines[i] = f"{key}: {value}"
                break
        else:
            lines.insert(end, f"{key}: {value}")
            end += 1
    _write_atomic(path, "\n".join(lines))


def add_comment(path: Path, text: str) -> None:
    """Append ``### @pump2 — <time>`` + text at the end; earlier text is untouched."""
    cur = path.read_text()
    sep = "" if cur.endswith("\n\n") else ("\n" if cur.endswith("\n") else "\n\n")
    path.write_text(f"{cur}{sep}### @pump2 — {now_iso()}\n{text.rstrip()}\n")


def enter_pump2(path: Path, why: str) -> None:
    """A card enters pump2 at stage analyze: `pump2 start` and the
    tick's pickup of a hand-moved in_progress card share this one write."""
    set_pump2(path, stage="analyze", attempt=0)
    add_comment(path, why)


def _when(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, str) and v.strip():
        d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    return None


def hold_reason(card: Card, board: Board, now: datetime | None = None) -> str:
    """Why this card may not run yet ('' = free). Read-only: deps, start_at."""
    now = now or datetime.now(UTC)
    start = _when(card.fm.get("start_at"))
    if start and now < start:
        return f"start_at {start.strftime('%Y-%m-%dT%H:%MZ')}"
    deps = card.fm.get("deps") or []
    open_deps = []
    for dep in deps:
        status = None
        for p in (
            board.repo / ".deviz/tasks" / f"{dep}.md",
            board.repo / ".deviz/tasks/archive" / f"{dep}.md",
        ):
            if p.exists():
                status = read_card(p).fm.get("status")
                break
        if status != "done":
            open_deps.append(f"{dep}({status or 'missing'})")
    return "deps " + ", ".join(open_deps) if open_deps else ""


def tail(text: str, n: int = 20) -> str:
    return "\n".join(text.rstrip("\n").split("\n")[-n:])


@dataclass
class Outcome:
    """What a model stage's finish() decides: the next stage (or waiting_user
    / stopped), the card comment, and extra pump2 keys to set."""

    next: str  # a stage name, "waiting_user" (stage stays) or "stopped"
    comment: str
    state: dict[str, Any] = field(default_factory=dict)


def set_status(path: Path, value: str) -> None:
    """Rewrite the card's ``status:`` line (frontmatter only)."""
    lines = path.read_text().split("\n")
    end = lines.index("---", 1)
    for i in range(1, end):
        if re.match(r"status\s*:", lines[i]):
            lines[i] = f"status: {value}"
            break
    else:
        lines.insert(end, f"status: {value}")
    path.write_text("\n".join(lines))


def add_owner_answer(path: Path, text: str) -> None:
    """Append ``### @owner — <time>`` and the owner's words, verbatim."""
    cur = path.read_text()
    sep = "" if cur.endswith("\n\n") else ("\n" if cur.endswith("\n") else "\n\n")
    path.write_text(f"{cur}{sep}### @owner — {now_iso()}\n{text}\n")


_COMMENT = re.compile(r"^### @(\S+)", re.M)
# machine comment authors: pump2 and the old pump (off since 2026-11-06, whose
# comments are most of a long card's bytes); the last machine comment is kept
MACHINE = ("### @pump2", "### @taskpump")
_COMMENTS_HEAD = re.compile(r"^## Comments\s*$", re.M)


def card_digest(card: Card) -> str:
    """What a stage prompt carries of the card: the frontmatter and
    the head (everything before ``## Comments``) verbatim, every comment not
    written by a pump (``@owner`` answers, ``@devops`` notes) verbatim, the
    last machine comment (``@pump2`` / old ``@taskpump``), and the card path
    for the rest. Older machine comments (74-91% of a long card) are dropped;
    order is kept."""
    text = card.path.read_text()
    m = _COMMENTS_HEAD.search(text)
    if m:
        cut = m.end()
    else:
        first = _COMMENT.search(text)
        cut = first.start() if first else len(text)
    head, rest = text[:cut], text[cut:]
    starts = [c.start() for c in _COMMENT.finditer(rest)]
    pre = rest[: starts[0]] if starts else rest
    secs = [rest[a:b] for a, b in zip(starts, starts[1:] + [len(rest)])]
    machine = [i for i, s in enumerate(secs) if s.startswith(MACHINE)]
    drop = set(machine[:-1])
    kept = [s for i, s in enumerate(secs) if i not in drop]
    note = f"\n(card file: {card.path}"
    note += (
        f"; {len(drop)} older pump comments not shown, read the file for them)"
        if drop
        else ")"
    )
    body = head + pre + "".join(kept)
    return body.rstrip("\n") + "\n" + note + "\n"
