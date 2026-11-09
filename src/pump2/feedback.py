"""The owner's after-testing feature survey (dv-a42c).

The unit of rating is a *feature*: a chain of done cards linked by ``deps``,
``parent`` or a card id named in the title / description ("owner feedback
after testing gb-1aec"). Each link records how it was done (``exec_mode``:
``pump2`` = the stage pipeline, ``job`` = a direct bgjob, ``chat`` = an agent
in a chat), so the owner's one score for the feature lands on every link.

Once per ``FEEDBACK_INTERVAL_MIN``, and never while a question is open, the
pump2 tick sends one question (``agentctl notify --source pump2:feedback``,
the "Feedback" topic) about a chain that is merged, on staging or prod, at
least ``FEEDBACK_MIN_AGE_HOURS`` old and not answered yet. The bot's
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
LOOKBACK_DAYS = _env_float("FEEDBACK_LOOKBACK_DAYS", 14)

_CARD_ID = re.compile(r"\b[a-z]{2,4}-[0-9a-f]{4}\b")
_PUMP_COMMENT = re.compile(r"^### @pump2 — (\S+)[^\n]*\n(.*?)(?=^### |\Z)", re.M | re.S)
_RETURN = re.compile(r"returned to|tests RED \(attempt", re.I)
_RUN_STAGES = tuple(s for s in STAGES if s not in ("done", "stopped"))


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
    out |= set(_CARD_ID.findall(text))
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
    """Group ``cards`` into features (union over deps / parent / named ids
    among ``cards``). ``job_cards``: ids a bgjob was run for; ``commits``:
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
) -> list[Chain]:
    """Chains the owner can test now and has not rated."""
    done = set(answered)
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
        if now - last > timedelta(days=lookback_days):
            continue
        out.append(ch)
    return out


# --- the question ---------------------------------------------------------


def _mode_text(lk: Link) -> str:
    if lk.exec_mode != "pump2":
        return lk.exec_mode
    tail = "→".join(lk.stages)
    ret = f", возвратов {lk.returns}" if lk.returns else ""
    return f"pump2 ({tail}{ret})" if tail else f"pump2{ret}"


def _since(lk: Link, view: dict[str, Any]) -> str:
    at = where(lk, view)
    if at == "staging":
        return f"на staging с {lk.merged_at:%Y-%m-%d}" if lk.merged_at else "на staging"
    if at == "prod":
        # prod_time is the current deploy (the home board: the last
        # restart), so the link has been on prod since that day at the latest
        prod = str(view.get("prod_time") or "")[:10]
        return f"на prod (не позже {prod})" if prod else "на prod"
    return "не выкачено"


QUESTION_SYSTEM = (
    "You describe a finished software feature to its owner in ONE short "
    "sentence in Russian: what it lets the user do. No preamble, no lists."
)


async def question_text(
    chain: Chain, view: dict[str, Any], complete: Complete | None = None
) -> str:
    """The question: each link (id, title, merge date, where it runs, how it
    was done), one model sentence on what the feature does (titles only when
    the call fails), then the answer hint."""
    lines = [f"🧪 Фича на оценку ({len(chain.links)} карточ.):"]
    for lk in chain.links:
        when = f"{lk.merged_at:%Y-%m-%d}" if lk.merged_at else "?"
        lines.append(
            f"• {lk.card_id} «{lk.title}» — влито {when}, {_since(lk, view)}; "
            f"сделано: {_mode_text(lk)}"
        )
    if complete is not None:
        prompt = "\n\n".join(f"{lk.title}\n{lk.summary}" for lk in chain.links)
        try:
            what = await complete(
                prompt,
                purpose=PURPOSE_QUESTION,
                system_prompt=QUESTION_SYSTEM,
                timeout=120.0,
            )
            what = " ".join(str(what or "").split())[:400]
            if what:
                lines.append(f"Что делает: {what}")
        except Exception:
            pass  # titles only
    lines.append(
        "Ты потестил? Понравилось? Нажми оценку или ответь голосом/текстом в "
        "этом топике: что хорошо, что плохо, есть ли баги («есть баги: …»)."
    )
    return "\n".join(lines)


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


def due(st: dict[str, Any], now: datetime, interval_min: float) -> str:
    """Why no question goes out now ("" = one may)."""
    if st.get("open"):
        return f"question open ({st['open'].get('feature_id')})"
    last = parse_time(str(st.get("last_sent") or ""))
    if last is not None and now - last < timedelta(minutes=interval_min):
        return "interval not passed"
    return ""


def tick(
    chains: list[Chain],
    *,
    now: datetime,
    notify: Notify,
    state_path: Path = STATE,
    interval_min: float = INTERVAL_MIN,
    view: dict[str, Any] | None = None,
    complete: Complete | None = None,
) -> list[str]:
    """Send at most one question about the first askable chain of
    ``chains`` (already eligible)."""
    st = load_state(state_path)
    why = due(st, now, interval_min)
    if why:
        return [f"feedback: {why}"]
    chain = next((c for c in chains if _askable(st, c.feature_id, now)), None)
    if chain is None:
        return []
    fid = chain.feature_id
    text = asyncio.run(question_text(chain, view or {}, complete=complete))
    try:
        notify(
            [
                str(AGENTCTL),
                "notify",
                text,
                "--source",
                SOURCE,
                "--buttons-json",
                json.dumps(buttons(fid)),
            ]
        )
    except Exception as e:
        return [f"feedback: send failed: {e}"]
    entry = st["asked"].get(fid) or {"n": 0}
    entry["n"] = int(entry.get("n", 0)) + 1
    entry["last"] = _iso(now)
    entry.pop("retry_at", None)
    st["asked"][fid] = entry
    st["open"] = {"feature_id": fid, "asked_at": _iso(now)}
    st["last_sent"] = _iso(now)
    st["last_feature"] = fid
    st["features"][fid] = {
        "board": chain.board,
        "links": [lk.row() for lk in chain.links],
    }
    _prune(st, now)
    save_state(state_path, st)
    ids = ", ".join(lk.card_id for lk in chain.links)
    return [f"feedback: asked {chain.board} {fid} ({ids})"]


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


def run(boards: dict[str, Board], *, now: datetime | None = None) -> list[str]:
    """The pump2 tick's survey step: best effort, a failure is a log line."""
    if not enabled():
        return []
    from . import pin
    from .state import list_cards

    now = now or datetime.now(UTC)
    st = load_state(STATE)
    why = due(st, now, INTERVAL_MIN)
    if why:
        return []
    since = now - timedelta(days=LOOKBACK_DAYS + 30)
    from src.storage.reactions import answered_features_sync
    from src.utils.paths import data_layout

    answered = answered_features_sync(data_layout().bot_db)
    jobs = job_cards(pin.REPO.parent / "jobs", since)

    def git(repo: Path, *a: str) -> Any:
        return _git_rc(repo, *a) if a[:1] == ("merge-base",) else pin._git(repo, *a)

    pool: list[tuple[Chain, dict[str, Any]]] = []
    for board in boards.values():
        cards = [c for c in list_cards(board)[0] if c.fm.get("status") == "done"]
        chains = build_chains(
            board, cards, job_cards=jobs, commits=card_commits(board, since, git)
        )
        view = pin.board_deploy_view(board, cards)
        view = _extend_view(board, view, chains, git)
        for ch in eligible_chains(chains, view=view, now=now, answered=answered):
            pool.append((ch, view))
    if not pool:
        return []
    # oldest merge first: it has waited longest
    pool.sort(key=lambda cv: cv[0].last_merge or now)
    views = {ch.feature_id: v for ch, v in pool}
    st_now = load_state(STATE)
    first = next((ch for ch, _ in pool if _askable(st_now, ch.feature_id, now)), None)
    if first is None:
        return []
    return tick(
        [first],
        now=now,
        notify=pin.send,
        state_path=STATE,
        view=views[first.feature_id],
        complete=pin._complete,
    )
