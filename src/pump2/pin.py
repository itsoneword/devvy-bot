"""The board's pinned summary: one post per board in its agent's
chat, edited in place through the bot's pin mechanism (``agentctl notify
--pin-as``, ``NotificationService._send_or_edit_pinned``). A new owner
question or a new stop also goes out as one short message. No tap targets:
the owner answers the agent in the chat and it acts.

Rendered from card state; ``data/pump2/pin-<board>.json`` is only a
presentation cache (fingerprint, last recommendation, what was notified).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import series
from .render import _COMMENT, _title, last_line, minutes, question_block
from .state import HOME_BOARD, Board, Card, list_cards

REPO = Path(__file__).resolve().parents[2]
AGENTCTL = REPO / "scripts" / "agentctl"
STATE_DIR = REPO / "data" / "pump2"
SOURCE = "pump2:status"
NEWS_SOURCE = "pump2:news"  # merges, questions, stops: one line each, Pump topic
PURPOSE = "pump_summary"
TITLE_WORDS = 6
SEND_TIMEOUT = 60
CALL_TIMEOUT = 120.0
# Telegram's limit is 4096; keep headroom for markdown escaping.
PIN_MAX = 3900
# (question lines kept, list rows kept) tried in order until the pin fits.
SHRINK_STEPS: list[tuple[int | None, int | None]] = [
    (None, None),
    (8, None),
    (3, 15),
    (0, 10),
    (0, 5),
    (0, 2),
]
# claude-bot's start: this repo's prod runs main as of that moment.
HOME_STARTED = [
    "systemctl",
    "show",
    "claude-bot",
    "-p",
    "ActiveEnterTimestamp",
    "--value",
    "--timestamp=unix",
]
# deploy.py's journal (ported from the old pump's read_deploy_log):
# `<ts> <old> -> <new> build=<b> ok` puts <new> on prod, `... rolled-back` puts
# the second sha back. `<ts> staging <old> -> <new> ...` never matches.
DEPLOY_LINE = re.compile(
    r"^(?P<ts>\S+)\s+[0-9a-f]{3,40}\s+->\s+(?P<to>[0-9a-f]{4,40})\s+(?P<rest>.*)$"
)
_MERGED = re.compile(r"^merge: merged ([0-9a-f]{7,40}) into", re.M)

Notify = Callable[[list[str]], None]
Complete = Callable[..., Coroutine[Any, Any, str]]


def send(argv: list[str]) -> None:
    """Run one ``agentctl notify``; raise on any failure (bot down)."""
    r = subprocess.run(argv, capture_output=True, text=True, timeout=SEND_TIMEOUT)
    if r.returncode:
        raise RuntimeError(f"agentctl notify rc={r.returncode}: {r.stderr.strip()}")


async def _complete(prompt: str, **kw: Any) -> str:
    """The one model-call path: the job router."""
    from src.model_router import complete_text
    from src.model_router.job import install_job_router

    install_job_router(REPO)
    return await complete_text(prompt, **kw)


# --- time / git ---------------------------------------------------------


def parse_time(raw: str) -> datetime | None:
    raw = raw.strip()
    try:
        if raw.startswith("@"):
            return datetime.fromtimestamp(float(raw[1:]), UTC)
        t = datetime.fromisoformat(
            re.sub(r"(T[\d:]+)\.\d+", r"\1", raw).replace("Z", "+00:00")
        )
    except ValueError:
        return None
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def _git(repo: Path, *a: str) -> str:
    r = subprocess.run(
        ["git", *a], cwd=repo, capture_output=True, text=True, timeout=30
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def read_deploy_log(path: Path) -> list[tuple[datetime, str]]:
    """(time, sha prod runs after it) per prod line of deploy.py's journal."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        m = DEPLOY_LINE.match(line.strip())
        when = parse_time(m["ts"]) if m else None
        if not (m and when):
            continue
        rest = m["rest"].split()
        if len(rest) > 1 and rest[0].startswith("build=") and rest[1] == "ok":
            out.append((when, m["to"]))
        elif rest and rest[0] == "rolled-back":
            out.append((when, m["to"]))
    return out


def home_started() -> datetime | None:
    try:
        r = subprocess.run(HOME_STARTED, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_time(r.stdout) if r.returncode == 0 and r.stdout.strip() else None


def merged_sha(card: Card) -> str:
    """The card's merge commit: the pump2 ``merged`` key, else its merge comment."""
    sha = str(card.pump2.get("merged") or "")
    if sha:
        return sha
    found = _MERGED.findall(card.body)
    return found[-1] if found else ""


def branch_at(repo: Path, branch: str, when: datetime) -> str:
    """The sha ``branch`` pointed at at ``when``.

    From the branch reflog (when the ref moved), since pump2 fast-forwards a
    card's commits with their build-time dates: commit dates would call a card
    live that landed after the restart. No reflog entry at or before ``when``:
    fall back to the last commit dated before it."""
    cutoff = int(when.timestamp())
    log = _git(
        repo, "reflog", "show", "--date=unix", "--format=%H %gd", f"refs/heads/{branch}"
    )
    for line in log.splitlines():  # newest first
        m = re.match(r"^([0-9a-f]{40}) \S*@\{(\d+)\}$", line.strip())
        if m and int(m[2]) <= cutoff:
            return m[1]
    return _git(repo, "rev-list", "-1", f"--before={cutoff}", branch)


def deploy_view(
    board: Board,
    *,
    deploy_log: Path | None = None,
    activated_at: datetime | None = None,
    cards: list[Card] | None = None,
) -> dict[str, Any]:
    """{prod_sha, prod_time, staging: [(card id, merge sha)]}.

    With a deploy log prod is its last prod line; with ``activated_at`` (the
    home board) prod is the branch head at that time. Neither: unknown, and
    nothing is called "on staging"."""
    view: dict[str, Any] = {"prod_sha": "unknown", "prod_time": "", "staging": []}
    prod = ""
    if deploy_log is not None:
        events = read_deploy_log(deploy_log)
        if not events:
            return view
        when, prod = events[-1]
        view["prod_time"] = when.strftime("%Y-%m-%d %H:%MZ")
    elif activated_at is not None:
        prod = branch_at(board.repo, board.branch, activated_at)
        view["prod_time"] = activated_at.strftime("%Y-%m-%d %H:%MZ")
    else:
        return view
    full = _git(board.repo, "rev-parse", "--verify", "-q", f"{prod}^{{commit}}")
    view["prod_sha"] = (full or prod)[:7] if prod else "none"
    rng = f"{full or prod}..{board.branch}" if prod else board.branch
    ahead = set(_git(board.repo, "rev-list", rng).split())
    if cards is None:
        cards = list_cards(board)[0]
    for c in cards:
        sha = merged_sha(c)
        if not sha:
            continue
        hit = next((a for a in ahead if a.startswith(sha) or sha.startswith(a)), "")
        if hit:
            view["staging"].append((c.id, sha[:7]))
    return view


def board_deploy_view(board: Board, cards: list[Card]) -> dict[str, Any]:
    if board.deploy_log:
        return deploy_view(board, deploy_log=Path(board.deploy_log), cards=cards)
    if board.name == HOME_BOARD:
        started = home_started()
        if started:
            return deploy_view(board, activated_at=started, cards=cards)
    return deploy_view(board, cards=cards)


# --- render --------------------------------------------------------------


def short_title(card: Card) -> str:
    words = _title(card).split()
    return " ".join(words[:TITLE_WORDS])


def stop_reason(card: Card) -> str:
    """The first line of the card's last ``@pump2`` comment."""
    heads = list(_COMMENT.finditer(card.body))
    for i in range(len(heads) - 1, -1, -1):
        if heads[i].group(1) != "pump2":
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(card.body)
        for ln in card.body[heads[i].end() : end].splitlines():
            if ln.strip():
                return ln.strip()[:160]
    return "stopped"


def is_waiting(card: Card) -> bool:
    return card.fm.get("status") == "waiting_user" or card.pump2.get("stage") == (
        "stopped"
    )


def is_in_work(card: Card) -> bool:
    st = card.pump2.get("stage")
    return (
        bool(st)
        and st not in ("done", "stopped")
        and card.fm.get("status") == "in_progress"
    )


def is_held(card: Card) -> bool:
    return is_in_work(card) or (bool(card.pump2.get("stage")) and is_waiting(card))


def _ask_excerpt(card: Card, lines: int | None) -> str:
    """The card's question block, cut to ``lines`` lines (None: whole)."""
    block = question_block(card) or "(no question block found)"
    rows = block.splitlines()
    if lines is None or len(rows) <= lines:
        return block
    return "\n".join(rows[:lines] + ["  … full question sent as a message"])


def _capped(rows: list[str], cap: int | None) -> list[str]:
    if cap is None or len(rows) <= cap:
        return rows
    return rows[:cap] + [f"• +{len(rows) - cap} more"]


def _render(
    board: Board,
    cards: list[Card],
    view: dict[str, Any],
    recommendation: str,
    now: datetime,
    ask_lines: int | None,
    cap: int | None,
    series_text: str = "",
) -> str:
    by_id = {c.id: c for c in cards}
    out = [f"pump2 · {board.name} · {now.strftime('%d.%m %H:%MZ')}"]
    out += [series_text] if series_text else []
    out += ["", "In work"]
    rows = []
    for c in (c for c in cards if is_in_work(c)):
        log = last_line(board, c)
        rows.append(
            f"• {c.id} {short_title(c)} — {c.pump2['stage']} {minutes(c, now)}"
            + (f" — {log}" if log else "")
        )
    out += _capped(rows, cap) or ["• nothing"]
    out += ["", "Waiting for you"]
    # every waiting card keeps its row: only its question text is shortened
    rows = []
    for c in (c for c in cards if c.pump2.get("stage") and is_waiting(c)):
        if c.pump2.get("stage") == "stopped":
            rows.append(f"• {c.id} {short_title(c)} — stopped: {stop_reason(c)}")
        elif ask_lines == 0:
            rows.append(
                f"• {c.id} {short_title(c)} — {c.pump2['stage']} asks"
                " (question sent as a message)"
            )
        else:
            rows.append(f"• {c.id} {short_title(c)} — {c.pump2['stage']} asks:")
            rows.append(_ask_excerpt(c, ask_lines))
    out += rows or ["• nothing"]
    out += ["", "On staging, not on prod"]
    rows = []
    for cid, sha in view.get("staging") or []:
        sc = by_id.get(cid)
        name = f" {short_title(sc)}" if sc else ""
        rows.append(f"• {cid}{name} — {sha}")
    out += _capped(rows, cap) or ["• nothing"]
    out += ["", "On prod"]
    prod = view.get("prod_sha") or "unknown"
    since = f" since {view['prod_time']}" if view.get("prod_time") else ""
    out.append(f"• {prod}{since}")
    out += ["", "Recommendation", recommendation.strip()[:600] or "—"]
    return "\n".join(out)


def render_pin(
    board: Board,
    cards: list[Card],
    view: dict[str, Any],
    recommendation: str,
    now: datetime | None = None,
) -> str:
    """The pin text, kept under ``PIN_MAX``: Telegram rejects a message over
    4096 chars and a failed pin edit is only logged, so an over-long pin would
    silently stop updating. Shrinks step by step: question blocks to a few
    lines, then to one row each, then the in-work/staging lists to "+K more".
    Full questions always go out as their own message (``news_text``)."""
    now = now or datetime.now(UTC)
    line = series.series_line(board, cards, now)  # never raises
    text = ""
    for ask_lines, cap in SHRINK_STEPS:
        text = _render(board, cards, view, recommendation, now, ask_lines, cap, line)
        if len(text) <= PIN_MAX:
            return text
    return text[: PIN_MAX - 2] + "\n…"


# --- state diff ----------------------------------------------------------


def fingerprint(cards: list[Card], view: dict[str, Any]) -> str:
    """The card set the recommendation depends on: ids + stage + status of
    held cards, staging ids, prod sha. No minutes or log lines."""
    held = sorted(
        (c.id, str(c.pump2.get("stage")), str(c.fm.get("status")))
        for c in cards
        if is_held(c)
    )
    key = {
        "held": held,
        "staging": sorted(cid for cid, _ in view.get("staging") or []),
        "prod": view.get("prod_sha"),
    }
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()[:16]


def news_key(card: Card) -> str:
    """What makes a waiting card news: its question block, or its stop."""
    if card.pump2.get("stage") == "stopped":
        mark = f"stop@{card.pump2.get('since', '')}"
    else:
        mark = "ask@" + hashlib.sha256(question_block(card).encode()).hexdigest()[:12]
    return f"{card.id}:{mark}"


def news_text(board: Board, card: Card) -> str:
    head = f"pump2 {board.name}:{card.id} {short_title(card)}"
    if card.pump2.get("stage") == "stopped":
        return f"⏹ {head} stopped: {stop_reason(card)}"
    return f"❓ {head} asks:\n{question_block(card) or '(no question block found)'}"


_MERGED = re.compile(r"merge: merged ([0-9a-f]{7,40})")


def topic_news(board: Board, card: Card, since: datetime) -> tuple[str, str] | None:
    """(key, text) of a card's line in the Pump topic, or None: a merge after
    ``since`` (the owner's rule: every merge reported), a question or a
    stop. The topic is the one place that shows all of them for every board."""
    stage = card.pump2.get("stage")
    if stage == "done":
        at = _when_iso(card.pump2.get("since"))
        if at is None or at < since:
            return None
        shas = _MERGED.findall(card.body)
        sha = f" {shas[-1][:8]}" if shas else ""
        head = f"{board.name}:{card.id} {short_title(card)}"
        return f"{card.id}:done@{card.pump2.get('since')}", f"✅ {head} merged{sha}"
    if stage and is_waiting(card):
        return news_key(card), news_text(board, card)
    return None


def _when_iso(v: Any) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _recommend_prompt(text: str) -> str:
    return (
        "Below is the pinned task-pump summary of one board. Write the "
        "Recommendation: 1-3 short lines for the owner. Say what to promote to "
        "prod and in which order, what to answer first and what to wait for. "
        "Plain text, no preamble.\n\n" + text
    )


def refresh(
    board: Board,
    *,
    notify: Notify | None = None,
    complete: Complete | None = None,
    state_dir: Path | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> str:
    """Render the pin, call the recommendation model when the card set
    changed, send new questions / stops once, edit the pin. Never raises on a
    send or router failure; returns the pin text."""
    notify = notify or send
    complete = complete or _complete
    path = (state_dir or STATE_DIR) / f"pin-{board.name}.json"
    state = _load(path)
    cards = list_cards(board)[0]
    view = board_deploy_view(board, cards)
    rec = str(state.get("recommendation") or "")
    fp = fingerprint(cards, view)
    if dry_run:
        return render_pin(board, cards, view, rec, now)
    if fp != state.get("fingerprint"):
        held = [c for c in cards if is_held(c)]
        if held or view.get("staging"):
            try:
                draft = render_pin(board, cards, view, "", now)
                rec = asyncio.run(
                    complete(
                        _recommend_prompt(draft),
                        purpose=PURPOSE,
                        system_prompt="You advise the owner of a software task board.",
                        timeout=CALL_TIMEOUT,
                    )
                ).strip()
            except Exception as e:  # keep the previous text
                print(f"pump2 pin {board.name}: recommendation failed: {e}")
        else:
            rec = "Nothing in flight."
        state["fingerprint"] = fp
        state["recommendation"] = rec
    notified = set(state.get("notified") or [])
    current: set[str] = set()
    for c in cards:
        if not (c.pump2.get("stage") and is_waiting(c)):
            continue
        key = news_key(c)
        current.add(key)
        if key in notified:
            continue
        try:
            notify(
                [
                    str(AGENTCTL),
                    "notify",
                    news_text(board, c),
                    "--profile",
                    board.profile,
                    "--deliver",
                    "--ref",
                    f"pump:{board.name}:{c.id}",
                ]
            )
            notified.add(key)
        except Exception as e:  # retried next tick
            print(f"pump2 pin {board.name}: message for {c.id} not sent: {e}")
    state["notified"] = sorted(notified & current)
    # the same news plus merges, once each, in the Pump topic; merges only
    # after the first tick that knew this (no backlog of old done cards)
    start = _when_iso(
        state.setdefault(
            "topic_from", (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    )
    sent = set(state.get("topic_sent") or [])
    shown: set[str] = set()
    for c in cards:
        news = topic_news(board, c, start or datetime.now(UTC))
        if news is None:
            continue
        key, msg = news
        shown.add(key)
        if key in sent:
            continue
        try:
            notify([str(AGENTCTL), "notify", msg, "--source", NEWS_SOURCE])
            sent.add(key)
        except Exception as e:  # retried next tick
            print(f"pump2 pin {board.name}: topic line for {c.id} not sent: {e}")
    state["topic_sent"] = sorted(sent & shown)
    text = render_pin(board, cards, view, rec, now)
    state["text"] = text
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=1) + "\n")
    except OSError as e:
        print(f"pump2 pin {board.name}: state not saved: {e}")
    return text


def _quiet(text: str) -> bool:
    """Nothing in work, waiting or on staging: the board collapses to one line."""
    return text.count("\n• nothing") >= 3


def send_all(
    boards: list[Board],
    *,
    notify: Notify | None = None,
    state_dir: Path | None = None,
    dry_run: bool = False,
) -> str:
    """ONE pin for every board, built from each board's last rendered text.

    Per-board pins in the shared Pump topic fought: each new topic pin clears
    the others (NotificationService._pin_new), so the owner saw only the last
    board's pin, an empty one."""
    notify = notify or send
    parts: list[str] = []
    quiet: list[tuple[str, str]] = []
    for board in boards:
        state = _load((state_dir or STATE_DIR) / f"pin-{board.name}.json")
        text = str(state.get("text") or "")
        if not text:
            continue
        if _quiet(text):
            line = next((x for x in text.splitlines() if x.startswith("series: ")), "")
            quiet.append((board.name, line))
        else:
            parts.append(text)
    if quiet:
        names = ", ".join(name for name, _ in quiet)
        rows = [f"pump2 · {names}: nothing in work or waiting"]
        rows += [f"{name} · {line}" for name, line in quiet if line]
        parts.append("\n".join(rows))
    if not parts:
        return ""
    cap = PIN_MAX // len(parts)
    text = "\n\n".join(t if len(t) <= cap else t[: cap - 2] + " …" for t in parts)
    if dry_run:
        return text
    try:
        notify(
            [
                str(AGENTCTL),
                "notify",
                text,
                "--source",
                SOURCE,
                "--pin-as",
                "pump2",
            ]
        )
    except Exception as e:  # best-effort: next tick redraws
        print(f"pump2 pin: pin not updated: {e}")
    return text
