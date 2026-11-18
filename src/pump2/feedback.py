"""The owner's after-testing feature survey.

The unit of rating is a *feature*: a chain of done cards linked by ``deps``,
``parent`` or "owner feedback after testing <id>" in the title / description
(a bare id mentioned in a description does not link). Each link records how it was done (``exec_mode``:
``pump2`` = the stage pipeline, ``job`` = a direct bgjob, ``chat`` = an agent
in a chat), so the owner's one score for the feature lands on every link.

Once per ``FEEDBACK_INTERVAL_MIN``, and never while a question is open, the
pump2 tick sends one question (``agentctl notify --source pump2:feedback``,
the "Feedback" topic) about a chain that is merged, on staging or prod, at
least ``FEEDBACK_MIN_AGE_HOURS`` and at most ``FEEDBACK_LOOKBACK_DAYS`` old
and not answered yet. v2 (dv-f34d): the question is built from the card's
result and its real diff (``collect_facts``: ``git log --grep=<card id>``),
says what changed, how to try it, before -> after and what to look at; an
internal feature (class ``c``) is never asked, a measured one (``b``) only
with an owner complaint and numbers; an open question expires after
``FEEDBACK_OPEN_TTL_H``. The bot's
Feedback-topic handler (``src/bot/features/feedback.py``) stores the answer
in ``message_reactions`` and closes the question in the state file
``data/pump2/feedback.json``. This module never writes cards or the DB.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .pin import AGENTCTL, STATE_DIR, Complete, Notify, merged_sha, parse_time
from .state import STAGES, Board, Card

SOURCE = "pump2:feedback"
STATE = STATE_DIR / "feedback.json"
PURPOSE_QUESTION = "feedback_question"
CALLBACK_PREFIX = "fb:"
NOT_TESTED, SKIP = "nt", "skip"
RETRY_DAYS = 3  # "не тестил": asked again after this many days ...
MAX_ASKS = 3  # ... at most twice more
KEEP_DAYS = 60  # state entries older than this are dropped


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def enabled() -> bool:
    return os.environ.get("FEEDBACK_ENABLED", "true").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


INTERVAL_MIN = _env_float("FEEDBACK_INTERVAL_MIN", 45)
MIN_AGE_HOURS = _env_float("FEEDBACK_MIN_AGE_HOURS", 24)
LOOKBACK_DAYS = _env_float("FEEDBACK_LOOKBACK_DAYS", 5)
OPEN_TTL_H = _env_float("FEEDBACK_OPEN_TTL_H", 24)  # open question closes after
B_WAIT_DAYS = _env_float("FEEDBACK_B_WAIT_DAYS", 3)  # (b) without numbers: retry

_CARD_ID = re.compile(r"\b[a-z]{2,4}-[0-9a-f]{4}\b")
_PUMP_COMMENT = re.compile(
    r"^### @(?:[a-z0-9-]+/)?pump2 — (\S+)[^\n]*\n(.*?)(?=^### |\Z)", re.M | re.S
)
_RETURN = re.compile(r"returned to|tests RED \(attempt", re.I)
_RUN_STAGES = tuple(s for s in STAGES if s not in ("done", "stopped"))
_AFTER_TESTING = re.compile(
    r"owner feedback after testing\s+(?:[a-z0-9_-]+:)?([a-z]{2,4}-[0-9a-f]{4})\b",
    re.I,
)


@dataclass
class Link:
    board: str
    card_id: str
    title: str
    merge_sha: str = ""
    merged_at: datetime | None = None
    exec_mode: str = "chat"
    stages: list[str] = field(default_factory=list)
    returns: int = 0
    summary: str = ""  # the card's description head, for the question call

    def row(self) -> dict[str, Any]:
        """The ``feedback_feature_cards`` row (``save_feature_cards``)."""
        return {
            "board": self.board,
            "card_id": self.card_id,
            "title": self.title,
            "merge_sha": self.merge_sha,
            "merged_at": _iso(self.merged_at) if self.merged_at else None,
            "exec_mode": self.exec_mode,
            "stages": list(self.stages),
            "returns": self.returns,
        }


@dataclass
class Chain:
    board: str
    links: list[Link]

    @property
    def feature_id(self) -> str:
        key = self.board + "|" + ",".join(sorted(lk.card_id for lk in self.links))
        return hashlib.sha1(key.encode()).hexdigest()[:12]

    @property
    def last_merge(self) -> datetime | None:
        times = [lk.merged_at for lk in self.links if lk.merged_at]
        return max(times) if times else None


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _description(card: Card) -> str:
    return card.body.split("## Comments", 1)[0]


def _refs(card: Card) -> set[str]:
    out: set[str] = set()
    deps = card.fm.get("deps") or []
    for d in [deps] if isinstance(deps, str) else deps:
        out.add(str(d).rpartition(":")[2].strip())
    parent = str(card.fm.get("parent") or "").rpartition(":")[2].strip()
    if parent:
        out.add(parent)
    text = f"{card.fm.get('title') or ''}\n{_description(card)}"
    out |= {m.lower() for m in _AFTER_TESTING.findall(text)}
    out.discard(card.id)
    return out


def _link(
    board: Board,
    card: Card,
    job_cards: set[str],
    commits: dict[str, tuple[str, datetime]],
) -> Link:
    stages: list[str] = []
    returns = 0
    merged_at = None
    for m in _PUMP_COMMENT.finditer(card.body):
        text = m.group(2)
        word = text.strip().split(":", 1)[0].strip()
        run = word in _RUN_STAGES or board.custom(word) is not None
        if run and word not in stages:
            stages.append(word)
        returns += len(_RETURN.findall(text))
        if re.search(r"^merge: merged [0-9a-f]{7,40}", text, re.M):
            merged_at = parse_time(m.group(1)) or merged_at
    sha = merged_sha(card)
    if card.pump2 or stages:
        mode = "pump2"
    elif card.id in job_cards:
        mode = "job"
    else:
        mode = "chat"
    if not sha and card.id in commits:
        sha, merged_at = commits[card.id]
    elif sha and merged_at is None and card.id in commits:
        merged_at = commits[card.id][1]
    desc = " ".join(_description(card).replace("## Description", "").split())
    return Link(
        board=board.name,
        card_id=card.id,
        title=str(card.fm.get("title") or card.id),
        merge_sha=sha,
        merged_at=merged_at,
        exec_mode=mode,
        stages=stages,
        returns=returns,
        summary=desc[:600],
    )


def build_chains(
    board: Board,
    cards: list[Card],
    job_cards: Iterable[str] | None = None,
    commits: dict[str, tuple[str, datetime]] | None = None,
) -> list[Chain]:
    """Group ``cards`` into features (union over deps / parent / "owner
    feedback after testing <id>" among ``cards``). ``job_cards``: ids a bgjob was run for; ``commits``:
    card id -> (sha, time) of its commit on the branch, for cards pump2 did
    not merge."""
    jobs = set(job_cards or ())
    known = {c.id: c for c in cards}
    root = {cid: cid for cid in known}

    def find(x: str) -> str:
        while root[x] != x:
            root[x] = root[root[x]]
            x = root[x]
        return x

    for c in cards:
        for ref in _refs(c):
            if ref in known:
                a, b = find(c.id), find(ref)
                if a != b:
                    root[b] = a
    groups: dict[str, list[Card]] = {}
    for c in cards:
        groups.setdefault(find(c.id), []).append(c)
    far = datetime.max.replace(tzinfo=UTC)
    chains = []
    for members in groups.values():
        links = [_link(board, c, jobs, commits or {}) for c in members]
        links.sort(key=lambda lk: (lk.merged_at or far, lk.card_id))
        chains.append(Chain(board=board.name, links=links))
    return chains


def where(link: Link, view: dict[str, Any]) -> str:
    """``staging`` / ``prod`` / ``""`` (not deployed or deploy state unknown)."""
    if not link.merge_sha:
        return ""
    if any(cid == link.card_id for cid, _ in view.get("staging") or ()):
        return "staging"
    if str(view.get("prod_sha") or "unknown") not in ("unknown", "none"):
        return "prod"
    return ""


def eligible_chains(
    chains: list[Chain],
    *,
    view: dict[str, Any],
    now: datetime,
    min_age_hours: float = MIN_AGE_HOURS,
    answered: Iterable[str] = (),
    lookback_days: float = LOOKBACK_DAYS,
    reask: Iterable[str] = (),
) -> list[Chain]:
    """Chains the owner can test now and has not rated. ``reask``: feature
    ids already asked and due again (expired / "не тестил"): exempt from the
    freshness window, which their retry delay has already passed."""
    done = set(answered)
    again = set(reask)
    out = []
    for ch in chains:
        last = ch.last_merge
        if ch.feature_id in done or last is None:
            continue
        if not all(lk.merge_sha and lk.merged_at for lk in ch.links):
            continue
        if not all(where(lk, view) for lk in ch.links):
            continue
        if now - last < timedelta(hours=min_age_hours):
            continue
        if now - last > timedelta(days=lookback_days) and ch.feature_id not in again:
            continue
        out.append(ch)
    return out


# --- the facts (v2: from the result and the real diff) ---------------------

# Paths whose change the owner cannot see: a diff only here is class (c).
_INTERNAL = re.compile(
    r"^(?:tests?/|docs/(?!screens/|SCREENS\.md)|scripts/|config/|\.deviz/|"
    r"\.pump/|\.claude/|\.github/|src/agent_[^/]*/|src/security/|"
    r"pyproject\.toml$|poetry\.lock$|Makefile$|[^/]*\.lock$|"
    r"(?:[^/]+/)*package-lock\.json$|(?:[^/]+/)*migrations?/)"
)
_SCREEN = re.compile(r"(?:^|/)(?:docs/screens/|\.screens/).*\.png$", re.I)
_RESULT = re.compile(
    r"RESULT|Implementation complete|Merged|Live on prod|Deployed", re.I
)
_COMMENT = re.compile(r"^### @([^\n]*)\n(.*?)(?=^### |\Z)", re.M | re.S)
_COMPLAINT = re.compile(
    r"\bOwner\s+(?:\d{1,2}\.\d{1,2}|\d{4}-\d\d-\d\d)|Filed by|owner complain|жалоб|жаловал",
    re.I,
)
_NUMBER = r"\d+(?:[.,]\d+)?\s*(?:ms|мс|s|с|сек|%|KB|КБ|MB|МБ)\b"
_NUMBERS = re.compile(
    rf"(?:before|было|до)\W[^\n]{{0,80}}?{_NUMBER}[^\n]*?"
    rf"(?:after|стало|после|→|->)\W[^\n]{{0,80}}?{_NUMBER}"
    rf"|{_NUMBER}\s*(?:→|->)\s*{_NUMBER}",
    re.I,
)
_GONE_VERB = r"\b(?:revert|drop|remove)(?:s|ed|ing)?\b\W+(?:[\w-]+\W+){0,2}?"


def _gone(subject: str, card_id: str, title: str) -> bool:
    """``subject`` undoes the card: ``Revert "...<id>..."`` or a
    revert / drop / remove verb naming the id or the title within two words
    (``dv-1: remove the old path`` is the card's own work, not a revert)."""
    subj = subject.lower()
    names = [re.escape(card_id.lower())]
    if title.strip():
        names.append(re.escape(title.strip().lower()))
    for name in names:
        if re.search(rf"^revert\b.*{name}|{_GONE_VERB}[\"«]?{name}", subj):
            return True
    return False


FACT_DESC = 2000
FACT_RESULT = 1500
FACT_DIFF = 5000
FACT_FILE = 1200
# Where the owner-visible strings live, read first: ru strings, then other
# strings / screens / commands, then the rest of the code.
_UI = re.compile(
    r"locales?/|\.tsx$|SCREENS\.md$|orchestrator\.py$|/static/|"
    r"shared-capabilities\.md$|/i18n/",
    re.I,
)


def _rank(path: str) -> int:
    if re.search(r"(?:^|/)ru\.json$|/ru/", path):
        return 0
    return 1 if _UI.search(path) else 2


@dataclass
class Facts:
    """What the question call may say about one card: ``text`` (card
    description, last result comment, commit subjects, added diff lines),
    the changed ``paths``, screenshot files, an owner ``complaint``, measured
    before / after ``numbers``, ``reverted`` after merge."""

    text: str
    paths: list[str] = field(default_factory=list)
    screens: list[str] = field(default_factory=list)
    complaint: bool = False
    numbers: bool = False
    reverted: bool = False
    commits: list[str] = field(default_factory=list)


def internal_path(path: str) -> bool:
    return bool(_INTERNAL.match(path))


def _last_result(card: Card) -> str:
    comments = [
        m.group(2).strip()
        for m in _COMMENT.finditer(card.body)
        if not re.match(r"(?:[a-z0-9-]+/)?pump2\b", m.group(1))
    ]
    hits = [c for c in comments if _RESULT.search(c)]
    pick = hits[-1] if hits else ""
    return pick[:FACT_RESULT]


def _added_lines(patch: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    cur = ""
    for line in patch.splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("+") and cur and not line.startswith("+++"):
            add = line[1:].strip()
            if add:
                out.setdefault(cur, []).append(add)
    return out


def _screens(board: Board, found: list[tuple[str, str]]) -> list[str]:
    """Screenshot files for the question, best first (light, short name):
    the working-tree file, else the blob from the commit that added it
    (written to a temp dir; the board tree may have moved it since)."""
    import subprocess
    import tempfile

    last: dict[str, str] = {p: sha for sha, p in found}
    order = sorted(last, key=lambda p: ("dark" in p.lower(), len(p), p))
    out: list[str] = []
    for p in order[:3]:
        here = board.repo / p
        if here.is_file():
            out.append(str(here))
            continue
        try:
            r = subprocess.run(
                ["git", "show", f"{last[p]}:{p}"],
                cwd=board.repo,
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode or not r.stdout:
            continue
        dest = Path(tempfile.gettempdir()) / "pump2-feedback" / last[p][:9] / p
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.stdout)
        out.append(str(dest))
    return out


def collect_facts(board: Board, card: Card, git: Callable[..., str]) -> Facts:
    """The card's facts: its commits are those naming its id on the board
    branch (``git log --grep``), not ``merge_sha``, which often points at a
    follow-up. Full description, the last result comment, the added lines of
    the non-internal files, screenshots, the revert guard."""
    cid = card.id
    log = (
        git(
            board.repo, "log", board.branch, "-i", f"--grep={cid}", "--format=%H %cI %s"
        )
        or ""
    )
    commits: list[tuple[str, str, str]] = []
    for line in log.splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3:
            commits.append((parts[0], parts[1], parts[2]))
    commits.reverse()  # oldest first
    title = str(card.fm.get("title") or "")
    reverted = False
    work: list[tuple[str, str]] = []
    for sha, _when, subj in commits:
        if work and _gone(subj, cid, title):
            reverted = True
            continue
        work.append((sha, subj))
    if not reverted and title and commits:
        later = (
            git(
                board.repo,
                "log",
                board.branch,
                f"{commits[-1][0]}..",
                "-i",
                "-E",
                "--grep=revert|drop|remove",
                "--format=%s",
            )
            or ""
        )
        reverted = any(_gone(x, cid, title) for x in later.splitlines())
    paths: list[str] = []
    shown: list[tuple[str, str]] = []  # (commit, path) newest commit last
    added: dict[str, list[str]] = {}
    for sha, _subj in work:
        names = git(board.repo, "show", "--format=", "--name-only", sha) or ""
        for p in names.splitlines():
            p = p.strip()
            if p:
                shown.append((sha, p))
                if p not in paths:
                    paths.append(p)
        patch = git(board.repo, "show", "--format=", "-U0", "--no-color", sha) or ""
        for p, lines in _added_lines(patch).items():
            if not internal_path(p) and not p.endswith(".png"):
                added.setdefault(p, []).extend(lines)
    screens = _screens(board, [(sha, p) for sha, p in shown if _SCREEN.search(p)])
    desc = _description(card).replace("## Description", "").strip()[:FACT_DESC]
    result = _last_result(card)
    diff_parts: list[str] = []
    budget = FACT_DIFF
    has_ru = any(_rank(p) == 0 for p in added)
    for p in sorted(added, key=_rank):
        if has_ru and _rank(p) == 1 and re.search(r"(?:^|/)[a-z]{2}\.json$", p):
            continue  # the ru strings are enough; en / pt repeat them
        block = (f"{p}:\n" + "\n".join(f"+ {ln}" for ln in added[p]))[:FACT_FILE]
        block = block[:budget]
        budget -= len(block)
        diff_parts.append(block)
        if budget <= 0:
            break
    sections = [f"Карточка {cid}: {title}", desc]
    if result:
        sections.append(f"Итог карточки:\n{result}")
    if work:
        sections.append("Коммиты:\n" + "\n".join(f"{s[:9]} {t}" for s, t in work))
    if paths:
        sections.append("Файлы: " + ", ".join(paths[:40]))
    if diff_parts:
        sections.append("Добавленные строки:\n" + "\n".join(diff_parts))
    card_text = f"{title}\n{desc}\n{result}"
    return Facts(
        text="\n\n".join(x for x in sections if x),
        paths=paths,
        screens=screens,
        complaint=bool(_COMPLAINT.search(card_text)),
        numbers=bool(_NUMBERS.search(card.body)),
        reverted=reverted,
        commits=[s for s, _ in work],
    )


# --- the question ---------------------------------------------------------

# Verbs every step starts with; they say nothing about the feature, so the
# step check ignores them and looks for the rest in the facts.
_GENERIC = {
    "открой",
    "нажми",
    "напиши",
    "выбери",
    "посмотри",
    "зайди",
    "перейди",
    "введи",
    "впиши",
    "попроси",
    "отправь",
    "сохрани",
    "проверь",
    "снова",
    "затем",
    "потом",
    "через",
    "после",
    "чтобы",
    "любой",
    "любую",
    "любое",
}
_ANCHOR = re.compile(r"«([^»]+)»|\"([^\"]+)\"|`([^`]+)`|(?<!\w)(/[a-z_][a-z0-9_]*)")
_WORD = re.compile(r"[\w-]{4,}")


def _fold(text: str) -> str:
    return " ".join(text.casefold().replace("ё", "е").split())


def _step_ok(step: str, hay: str) -> bool:
    s = _fold(step).rstrip(".!;:")
    if not s:
        return False
    if s in hay:
        return True
    anchors = [next(g for g in m.groups() if g) for m in _ANCHOR.finditer(step)]
    if anchors:  # the checkable part: every quoted string / command is real
        return all(_fold(a) in hay for a in anchors)
    words = [w for w in _WORD.findall(s) if w not in _GENERIC]
    found = sum(1 for w in words if w[: max(4, len(w) - 2)] in hay)
    return bool(words) and found * 2 >= len(words)


def validate_steps(steps: Iterable[str], facts_text: str) -> list[str]:
    """The steps found in the facts: a step with quoted UI strings, ``code``
    or /commands is kept when each of them appears as is; a step with none,
    when at least half of its words (stems, generic verbs left out) do. A step the model made up is dropped.
    """
    hay = _fold(facts_text)
    return [str(s).strip() for s in steps if str(s).strip() and _step_ok(str(s), hay)]


@dataclass
class Question:
    """``text`` empty = not sent; ``reason`` says why (log word)."""

    text: str
    cls: str
    image: str = ""
    reason: str = ""


QUESTION_SYSTEM = (
    "You explain a shipped software feature to its owner, who is not a "
    "programmer, so he can try it in a minute and score it. Use ONLY the "
    "facts given (card text, its result, the commit diff): never invent a "
    "command, button, screen or string. Quote UI strings, buttons and "
    "commands exactly as they appear in the facts, in «» (commands like "
    "/limits as is). Answer with ONE JSON object, Russian text, no prose "
    "around it:\n"
    '{"class": "a|b|c", "title": "<what it is, plain words, <=60 chars>", '
    '"change": "<1 line: what he sees or can do now>", '
    '"steps": ["<1-3 exact steps: command / screen / button>"], '
    '"before": "<1 line: how it was>", "after": "<1 line: how it is now>", '
    '"look_for": ["<2-3 concrete things to check, what counts as a bug>"], '
    '"metric": "<b only: the measured before -> after numbers>"}\n'
    "class a: he can see or use the change (screen, command, text, "
    "button). class b: no visible change, an effect he could feel "
    "(speed, reliability) with measured numbers. class c: internal "
    "(refactor, tests, security plumbing, backend wiring) he cannot judge."
)


def _short_date(t: datetime | None) -> str:
    return f"{t:%d.%m}" if t else "?"


def _where_since(chain: Chain, view: dict[str, Any]) -> str:
    places = {where(lk, view) for lk in chain.links}
    when = _short_date(chain.last_merge)
    if "" in places:
        return "ещё не выкачено"
    if "staging" in places:
        return f"на staging с {when}"
    return f"на проде, влито {when}"


def _parse_answer(raw: str) -> dict[str, Any] | None:
    text = str(raw or "")
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        d = json.loads(text[a : b + 1])
    except ValueError:
        return None
    if not isinstance(d, dict) or str(d.get("class") or "") not in ("a", "b", "c"):
        return None
    return d


def _one(v: Any) -> str:
    return " ".join(str(v or "").split())


def _list(v: Any) -> list[str]:
    if isinstance(v, str):
        v = [v]
    return [_one(x) for x in (v or []) if _one(x)] if isinstance(v, list) else []


def _chain_facts(chain: Chain, facts: dict[str, Facts]) -> Facts:
    fs = [facts[lk.card_id] for lk in chain.links if lk.card_id in facts]
    return Facts(
        text="\n\n".join(f.text for f in fs),
        paths=[p for f in fs for p in f.paths],
        screens=[p for f in fs for p in f.screens],
        complaint=any(f.complaint for f in fs),
        numbers=any(f.numbers for f in fs),
        reverted=any(f.reverted for f in fs),
        commits=[c for f in fs for c in f.commits],
    )


async def compose(
    chain: Chain,
    view: dict[str, Any],
    complete: Complete | None,
    facts: dict[str, Facts],
    *,
    note: str = "",
) -> Question | None:
    """The v2 question for ``chain`` from ``facts`` (card id -> Facts): one
    model call, steps checked against the facts. None: the call failed or
    its answer did not parse (retried next tick, no v1 fallback). A Question
    with empty ``text`` is not sent: ``reason`` says why. ``note``: a line
    about the previous question closed without an answer."""
    f = _chain_facts(chain, facts)
    if f.reverted:
        return Question(text="", cls="gone", reason="skipped reverted")
    if f.paths and all(internal_path(p) for p in f.paths):
        return Question(text="", cls="c", reason="skipped internal")
    if complete is None:
        return None
    try:
        raw = await complete(
            f.text[:12000],
            purpose=PURPOSE_QUESTION,
            system_prompt=QUESTION_SYSTEM,
            timeout=180.0,
        )
    except Exception:
        return None
    d = _parse_answer(raw)
    if d is None:
        return None
    cls = str(d["class"])
    if cls == "c":
        return Question(text="", cls="c", reason="skipped internal")
    if cls == "b" and not (f.complaint and f.numbers and _one(d.get("metric"))):
        return Question(text="", cls="b", reason="waiting for numbers")
    steps = validate_steps(_list(d.get("steps"))[:3], f.text)
    if cls == "a" and not steps:
        return Question(text="", cls="a", reason="cannot describe")
    first = chain.links[0]
    product = chain.board[:1].upper() + chain.board[1:]
    title = _one(d.get("title")) or first.title
    head_id = chain.links[-1].card_id
    lines = [f"🧪 {product} · {title} ({head_id}), {_where_since(chain, view)}"]
    change = _one(d.get("change"))
    if change:
        lines.append(f"Что нового: {change}")
    if cls == "b":
        lines.append(f"Замер: {_one(d.get('metric'))}")
    if steps:
        head = "Как попробовать:" if cls == "a" else "Как проверить:"
        lines.append(head)
        lines += [f"{n}. {s}" for n, s in enumerate(steps, 1)]
    before, after = _one(d.get("before")), _one(d.get("after"))
    if before or after:
        lines.append(f"Было → стало: {before or '—'} → {after or '—'}")
    look = _list(d.get("look_for"))[:3]
    if look:
        lines.append("Смотри: " + "; ".join(look))
    if len(chain.links) > 1:
        lines.append("(карточки: " + " → ".join(lk.card_id for lk in chain.links) + ")")
    if note:
        lines.append(note)
    lines.append(
        "Оцени 1–10 или «не тестил»; можно ответить текстом или голосом "
        "(«есть баги: …»)."
    )
    return Question(
        text="\n".join(lines), cls=cls, image=f.screens[0] if f.screens else ""
    )


def buttons(feature_id: str) -> list[list[dict[str, str]]]:
    def b(text: str, v: str) -> dict[str, str]:
        return {"text": text, "callback_data": f"{CALLBACK_PREFIX}{feature_id}:{v}"}

    return [
        [b(str(n), str(n)) for n in range(1, 6)],
        [b(str(n), str(n)) for n in range(6, 11)],
        [b("не тестил", NOT_TESTED), b("пропустить", SKIP)],
    ]


def parse_callback(data: str) -> tuple[str, str] | None:
    """``fb:<feature_id>:<n|nt|skip>`` -> (feature_id, value)."""
    if not data.startswith(CALLBACK_PREFIX):
        return None
    fid, _, v = data[len(CALLBACK_PREFIX) :].partition(":")
    if not fid or not (v in (NOT_TESTED, SKIP) or (v.isdigit() and 1 <= int(v) <= 10)):
        return None
    return fid, v


# --- state ----------------------------------------------------------------


def load_state(path: Path = STATE) -> dict[str, Any]:
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    if not isinstance(st, dict):
        st = {}
    st.setdefault("open", None)
    st.setdefault("asked", {})
    st.setdefault("features", {})
    return st


def save_state(path: Path, st: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def close_open(
    path: Path, feature_id: str, *, retry: bool = False, now: datetime | None = None
) -> None:
    """The question on ``feature_id`` got its answer. ``retry`` ("не тестил"):
    ask it again after ``RETRY_DAYS``."""
    st = load_state(path)
    if (st.get("open") or {}).get("feature_id") == feature_id:
        st["open"] = None
    entry = st["asked"].setdefault(feature_id, {"n": 1})
    if retry:
        t = (now or datetime.now(UTC)) + timedelta(days=RETRY_DAYS)
        entry["retry_at"] = _iso(t)
    else:
        entry.pop("retry_at", None)
        entry["closed"] = True
    save_state(path, st)


def target_feature(path: Path = STATE) -> str | None:
    """The feature a free answer in the topic belongs to: the open question,
    else the one asked last."""
    st = load_state(path)
    return (st.get("open") or {}).get("feature_id") or st.get("last_feature")


def feature_links(path: Path, feature_id: str) -> list[dict[str, Any]]:
    return list((load_state(path)["features"].get(feature_id) or {}).get("links") or [])


def _askable(st: dict[str, Any], fid: str, now: datetime) -> bool:
    entry = st["asked"].get(fid)
    if entry is None:
        return True
    retry = parse_time(str(entry.get("retry_at") or ""))
    return retry is not None and retry <= now and int(entry.get("n", 1)) < MAX_ASKS


def _prune(st: dict[str, Any], now: datetime) -> None:
    cut = now - timedelta(days=KEEP_DAYS)
    for fid, e in list(st["asked"].items()):
        t = parse_time(str(e.get("last") or ""))
        if t is not None and t < cut:
            st["asked"].pop(fid, None)
            st["features"].pop(fid, None)


def expire_open(st: dict[str, Any], now: datetime, ttl_h: float | None = None) -> str:
    """Close an open question older than ``OPEN_TTL_H``: no answer is
    written (it is not "не тестил"), the feature comes back after
    ``RETRY_DAYS`` (``MAX_ASKS`` still caps it). Returns the closed feature
    id, "" when none."""
    op = st.get("open") or {}
    asked = parse_time(str(op.get("asked_at") or ""))
    ttl = OPEN_TTL_H if ttl_h is None else ttl_h
    if not op or asked is None or now - asked < timedelta(hours=ttl):
        return ""
    fid = str(op.get("feature_id") or "")
    st["open"] = None
    entry = st["asked"].setdefault(fid, {"n": 1})
    entry["expired"] = True
    entry["retry_at"] = _iso(now + timedelta(days=RETRY_DAYS))
    st["unanswered"] = fid
    return fid


def due(st: dict[str, Any], now: datetime, interval_min: float) -> str:
    """Why no question goes out now ("" = one may)."""
    if st.get("open"):
        return f"question open ({st['open'].get('feature_id')})"
    last = parse_time(str(st.get("last_sent") or ""))
    if last is not None and now - last < timedelta(minutes=interval_min):
        return "interval not passed"
    return ""


def _unanswered_note(st: dict[str, Any], fid: str) -> str:
    """The line on the question that expired unanswered (once)."""
    old = str(st.get("unanswered") or "")
    if not old or old == fid:
        return ""
    links = (st["features"].get(old) or {}).get("links") or []
    names = ", ".join(str(lk.get("card_id")) for lk in links if lk.get("card_id"))
    return f"(прошлый вопрос про {names or old} закрыт без ответа, ответить можно и сейчас)"


def _mark(st: dict[str, Any], chain: Chain, now: datetime, cls: str) -> None:
    """Record a chain whose question did not go out (its class, when)."""
    fid = chain.feature_id
    entry = st["asked"].get(fid) or {"n": 0}
    entry["class"] = cls
    entry["last"] = _iso(now)
    st["asked"][fid] = entry
    feat = st["features"].setdefault(
        fid, {"board": chain.board, "links": [lk.row() for lk in chain.links]}
    )
    feat["class"] = cls


def tick(
    chains: list[Chain],
    *,
    now: datetime,
    notify: Notify,
    state_path: Path = STATE,
    interval_min: float = INTERVAL_MIN,
    view: dict[str, Any] | None = None,
    complete: Complete | None = None,
    facts: dict[str, Facts] | None = None,
    views: dict[str, dict[str, Any]] | None = None,
    force: bool = False,
) -> list[str]:
    """Send at most one question about the first askable chain of ``chains``
    (already eligible). Expires a stale open question first. A chain whose
    question is not sent (internal, reverted, (b) without numbers, (a) with
    no checkable step) is recorded and the next one is tried; at most one
    model call per tick. ``force`` (``feedback ask``): ignore the open
    question and the interval, ask the first chain even if asked before."""
    st = load_state(state_path)
    out: list[str] = []
    gone = expire_open(st, now)
    if gone:
        out.append(f"feedback: expired {gone} (no answer in {OPEN_TTL_H:g} h)")
    why = "" if force else due(st, now, interval_min)
    if why:
        if gone:
            save_state(state_path, st)
        return out + [f"feedback: {why}"]
    if force and st.get("open"):
        expire_open(st, now, ttl_h=0)
    facts = facts or {}
    calls: list[int] = []
    errors: list[str] = []
    counted: Complete | None = None
    if complete is not None:
        model = complete

        async def _counted(*a: Any, **k: Any) -> str:
            calls.append(1)
            try:
                return await model(*a, **k)
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}"[:200])
                raise

        counted = _counted

    for chain in chains:
        fid = chain.feature_id
        if not force and not _askable(st, fid, now):
            continue
        if calls:
            break  # one paid call per tick; the rest wait for the next one
        ids = ", ".join(lk.card_id for lk in chain.links)
        v = (views or {}).get(fid, view or {})
        note = _unanswered_note(st, fid)
        q = asyncio.run(compose(chain, v, counted, facts, note=note))
        if q is None:
            why = (
                errors[-1] if errors else "no model" if complete is None else "bad JSON"
            )
            out.append(f"feedback: question failed {ids}: {why} (retry next tick)")
            break
        if not q.text:
            _mark(st, chain, now, q.cls)
            entry = st["asked"][fid]
            if q.cls in ("a", "b"):  # (b) without numbers / (a) no steps: later
                entry["retry_at"] = _iso(now + timedelta(days=B_WAIT_DAYS))
            else:  # internal / reverted: never asked
                entry.pop("retry_at", None)
            out.append(f"feedback: {q.reason} {ids}")
            continue
        argv = [str(AGENTCTL), "notify", q.text, "--source", SOURCE]
        argv += ["--buttons-json", json.dumps(buttons(fid))]
        if q.image:
            argv += ["--image", q.image]
        try:
            notify(argv)
        except Exception as e:
            out.append(f"feedback: send failed: {e}")
            break
        entry = st["asked"].get(fid) or {"n": 0}
        entry["n"] = int(entry.get("n", 0)) + 1
        entry["last"] = _iso(now)
        entry["class"] = q.cls
        entry.pop("retry_at", None)
        entry.pop("expired", None)
        st["asked"][fid] = entry
        st["open"] = {"feature_id": fid, "asked_at": _iso(now)}
        st["last_sent"] = _iso(now)
        st["last_feature"] = fid
        st.pop("unanswered", None)
        st["features"][fid] = {
            "board": chain.board,
            "links": [lk.row() for lk in chain.links],
            "class": q.cls,
        }
        out.append(f"feedback: asked {chain.board} {fid} ({ids})")
        break
    _prune(st, now)
    save_state(state_path, st)
    return out


# --- the real tick (pump2 tick) --------------------------------------------


def job_cards(jobs_root: Path, since: datetime) -> set[str]:
    """Card ids a non-pump bgjob ran for since ``since`` (its ``task``, or a
    card id in its name)."""
    out: set[str] = set()
    stamp = since.strftime("%Y%m%d")
    try:
        dirs = [d for d in jobs_root.iterdir() if d.name[:8] >= stamp]
    except OSError:
        return out
    for d in dirs:
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        name = str(meta.get("name") or "")
        if name.startswith(("pump2-", "pump-", "merge-")):
            continue
        task = str(meta.get("task") or "").rpartition(":")[2]
        if task:
            out.add(task)
        out |= set(_CARD_ID.findall(name))
    return out


def card_commits(
    board: Board, since: datetime, git: Callable[..., str]
) -> dict[str, tuple[str, datetime]]:
    """card id -> (sha, time) of the newest commit on the branch naming it."""
    log = git(
        board.repo,
        "log",
        board.branch,
        f"--since={since.isoformat()}",
        "--format=%H %cI %s",
    )
    out: dict[str, tuple[str, datetime]] = {}
    for line in log.splitlines():  # newest first
        parts = line.split(" ", 2)
        if len(parts) < 3:
            continue
        when = parse_time(parts[1])
        if when is None:
            continue
        for cid in _CARD_ID.findall(parts[2]):
            out.setdefault(cid, (parts[0], when))
    return out


def _extend_view(
    board: Board, view: dict[str, Any], chains: list[Chain], git: Callable[..., str]
) -> dict[str, Any]:
    """deploy_view knows only pump2 merges; place the other links too."""
    prod = str(view.get("prod_sha") or "")
    if prod in ("", "unknown", "none"):
        return view
    staged = {cid for cid, _ in view.get("staging") or ()}
    extra: list[tuple[str, str]] = []
    for ch in chains:
        for lk in ch.links:
            if lk.exec_mode == "pump2" or not lk.merge_sha or lk.card_id in staged:
                continue
            on_prod = git(board.repo, "merge-base", "--is-ancestor", lk.merge_sha, prod)
            if on_prod is None:
                extra.append((lk.card_id, lk.merge_sha[:7]))
    return {**view, "staging": [*view.get("staging", []), *extra]}


def _git_rc(repo: Path, *a: str) -> str | None:
    """stdout, or None on a non-zero exit (``merge-base --is-ancestor``)."""
    import subprocess

    r = subprocess.run(
        ["git", *a], cwd=repo, capture_output=True, text=True, timeout=30
    )
    return r.stdout.strip() if r.returncode == 0 else None


def _git_any(repo: Path, *a: str) -> Any:
    from . import pin

    return _git_rc(repo, *a) if a[:1] == ("merge-base",) else pin._git(repo, *a)


def _board_chains(
    board: Board,
    cards: list[Card],
    *,
    since: datetime,
    git: Callable[..., Any],
    jobs: Iterable[str] = (),
) -> list[Chain]:
    return build_chains(
        board, cards, job_cards=jobs, commits=card_commits(board, since, git)
    )


def _facts_for(
    board: Board, chains: Iterable[Chain], cards: list[Card], git: Callable[..., Any]
) -> dict[str, Facts]:
    by_id = {c.id: c for c in cards}
    out: dict[str, Facts] = {}
    for ch in chains:
        for lk in ch.links:
            card = by_id.get(lk.card_id)
            if card is not None and lk.card_id not in out:
                out[lk.card_id] = collect_facts(board, card, git)
    return out


def run(boards: dict[str, Board], *, now: datetime | None = None) -> list[str]:
    """The pump2 tick's survey step: best effort, a failure is a log line."""
    if not enabled():
        return []
    from . import pin
    from .state import list_cards

    now = now or datetime.now(UTC)
    st = load_state(STATE)
    gone = expire_open(st, now)  # before the open-question return below
    if gone:
        save_state(STATE, st)
    if due(st, now, INTERVAL_MIN):
        return [f"feedback: expired {gone}"] if gone else []
    since = now - timedelta(days=LOOKBACK_DAYS + 30)
    from src.storage.reactions import answered_features_sync
    from src.utils.paths import data_layout

    answered = answered_features_sync(data_layout().bot_db)
    jobs = job_cards(pin.REPO.parent / "jobs", since)
    reask = {
        fid
        for fid, e in st["asked"].items()
        if e.get("retry_at") and int(e.get("n", 0)) >= 1
    }
    pool: list[tuple[Chain, dict[str, Any], Board, list[Card]]] = []
    for board in boards.values():
        cards = [c for c in list_cards(board)[0] if c.fm.get("status") == "done"]
        chains = _board_chains(board, cards, since=since, git=_git_any, jobs=jobs)
        view = pin.board_deploy_view(board, cards)
        view = _extend_view(board, view, chains, _git_any)
        for ch in eligible_chains(
            chains, view=view, now=now, answered=answered, reask=reask
        ):
            pool.append((ch, view, board, cards))
    if not pool:
        return [f"feedback: expired {gone}"] if gone else []
    # oldest merge first: it has waited longest
    pool.sort(key=lambda p: p[0].last_merge or now)
    todo = [p for p in pool if _askable(st, p[0].feature_id, now)][:8]
    if not todo:
        return [f"feedback: expired {gone}"] if gone else []
    facts: dict[str, Facts] = {}
    for ch, _v, board, cards in todo:
        facts.update(_facts_for(board, [ch], cards, _git_any))
    return tick(
        [p[0] for p in todo],
        now=now,
        notify=pin.send,
        state_path=STATE,
        views={p[0].feature_id: p[1] for p in todo},
        complete=pin._complete,
        facts=facts,
    )


# --- by hand: preview / ask one card ---------------------------------------


def _target(
    board: Board,
    card_id: str,
    *,
    git: Callable[..., Any],
    now: datetime,
    cards: list[Card] | None,
) -> tuple[Chain, list[Card]]:
    from .state import list_cards

    if cards is None:
        cards = list_cards(board)[0]
    done = [c for c in cards if c.fm.get("status") == "done" or c.id == card_id]
    if not any(c.id == card_id for c in done):
        raise KeyError(f"{board.name}:{card_id}: no such card")
    since = now - timedelta(days=365)
    chains = _board_chains(board, done, since=since, git=git)
    chain = next(ch for ch in chains if any(lk.card_id == card_id for lk in ch.links))
    return chain, done


def preview(
    board: Board,
    card_id: str,
    *,
    complete: Complete | None,
    git: Callable[..., Any],
    now: datetime,
    cards: list[Card] | None = None,
    view: dict[str, Any] | None = None,
) -> str:
    """The v2 question for ``card_id``'s feature as it would go out, with no
    freshness / interval check, nothing sent, no state written."""
    chain, done = _target(board, card_id, git=git, now=now, cards=cards)
    facts = _facts_for(board, [chain], done, git)
    errors: list[str] = []
    model = complete

    async def _call(*a: Any, **k: Any) -> str:
        assert model is not None
        try:
            return await model(*a, **k)
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}"[:300])
            raise

    q = asyncio.run(compose(chain, view or {}, _call if model else None, facts))
    ids = ", ".join(lk.card_id for lk in chain.links)
    if q is None:
        why = errors[-1] if errors else "no model" if model is None else "bad JSON"
        return f"feedback: question failed {ids}: {why}"
    if not q.text:
        return f"feedback: {q.reason} {ids} (class {q.cls}, not sent)"
    tail = f"\n[фото: {q.image}]" if q.image else ""
    return f"{q.text}{tail}\n[class {q.cls}, feature {chain.feature_id}]"


def _split(boards: dict[str, Board], full: str) -> tuple[Board, str]:
    name, _, cid = full.partition(":")
    if not cid or name not in boards:
        raise KeyError(f"{full}: want <board:id> of a served board")
    return boards[name], cid


def _live_view(board: Board, chain: Chain, cards: list[Card]) -> dict[str, Any]:
    from . import pin

    view = pin.board_deploy_view(board, cards)
    return _extend_view(board, view, [chain], _git_any)


def preview_card(
    boards: dict[str, Board], full: str, *, now: datetime | None = None
) -> str:
    """``pump2 feedback preview <board:id>``: one paid model call."""
    from . import pin

    board, cid = _split(boards, full)
    now = now or datetime.now(UTC)
    chain, done = _target(board, cid, git=_git_any, now=now, cards=None)
    return preview(
        board,
        cid,
        complete=pin._complete,
        git=_git_any,
        now=now,
        cards=done,
        view=_live_view(board, chain, done),
    )


def ask_card(
    boards: dict[str, Board], full: str, *, now: datetime | None = None
) -> list[str]:
    """``pump2 feedback ask <board:id>``: send its v2 question now (no
    interval / freshness check; an open question is closed unanswered)."""
    from . import pin

    board, cid = _split(boards, full)
    now = now or datetime.now(UTC)
    chain, done = _target(board, cid, git=_git_any, now=now, cards=None)
    return tick(
        [chain],
        now=now,
        notify=pin.send,
        state_path=STATE,
        view=_live_view(board, chain, done),
        complete=pin._complete,
        facts=_facts_for(board, [chain], done, _git_any),
        force=True,
    )
