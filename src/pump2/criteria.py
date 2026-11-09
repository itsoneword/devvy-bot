"""Per-card criteria (dv-d5aa): the one place for the plan's criterion and
question format, the frozen ``criteria_sha``, review remark gating, the
shared return budget and the closing summary comment. analyze, tests,
review and merge import it; do not parse these formats anywhere else.

Criterion: ``Cn [BLOCK|ADVICE] text | because: why | check: test|script|model|owner``
(an optional trailing ``| source: …``). Question: ``Q: … | default: … |
matters: … | found: …``. Review remark: ``N. [Cn|D1..D4] text | evidence: …``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOUCHES = ("code", "db", "ui", "docs")
SIZES = ("S", "M", "L")
KINDS = ("bug", "feature", "new screen", "docs", "ops")
CHECKS = ("test", "script", "model", "owner")
MAX_BLOCK = {"S": 3, "M": 5, "L": 6}
MAX_TOTAL = {"S": 3, "M": 8, "L": 14}
MAX_QUESTIONS = {"S": 1, "M": 3, "L": 5}
MAX_OWNER = 3
DEFECTS = {
    "D1": "red tests",
    "D2": "security",
    "D3": "regression with a reproduction",
    "D4": "change outside the plan",
}
RETURN_BUDGET = 1  # judge returns per card; the next one stops it
JOB_CEILING = 12  # model jobs per card
NONE = {"", "none", "none.", "n/a", "-", "(none)", "no questions"}

_CRIT = re.compile(
    r"^(C\d+)\s+\[(BLOCK|ADVICE)\]\s+(\S.*?)\s*\|\s*because:\s*(\S.*?)\s*"
    r"\|\s*check:\s*(\w+)\s*(?:\|\s*source:.*)?$"
)
_Q = re.compile(
    r"^Q:\s*(\S.*?)\s*\|\s*default:\s*(.*?)\s*\|\s*matters:\s*(.*?)\s*"
    r"\|\s*found:\s*(.*?)\s*$"
)
_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+")
_REMARK = re.compile(r"^\s*\d+[.)]\s+(\S.*)$")
_TAG = re.compile(r"^\[(C\d+|D[1-4])\]\s*")


class FormatError(ValueError):
    pass


@dataclass(frozen=True)
class Criterion:
    id: str
    level: str  # BLOCK | ADVICE
    text: str
    because: str
    check: str


def sections(text: str) -> dict[str, str]:
    """Level-2 sections of a plan, bodies stripped; the first heading wins."""
    secs: dict[str, str] = {}
    cur: str | None = None
    for ln in text.split("\n"):
        m = re.match(r"##\s+(.+?)\s*$", ln)
        if m:
            cur = m.group(1)
            secs.setdefault(cur, "")
        elif cur is not None:
            secs[cur] += ln + "\n"
    return {k: v.strip() for k, v in secs.items()}


def replace_section(text: str, name: str, body: str) -> str:
    """``text`` with the body of section ``name`` replaced by ``body``."""
    pat = re.compile(rf"(^##\s+{re.escape(name)}\s*\n)(.*?)(?=^##\s|\Z)", re.M | re.S)
    return pat.sub(lambda m: m.group(1) + body.rstrip() + "\n", text, count=1)


def lines(body: str) -> list[str]:
    return [_ITEM.sub("", ln.strip()) for ln in body.split("\n") if ln.strip()]


def parse_criteria(body: str) -> list[Criterion]:
    out: list[Criterion] = []
    seen: set[str] = set()
    for ln in lines(body):
        m = _CRIT.match(ln)
        if not m:
            raise FormatError(
                f"criterion line does not match `Cn [BLOCK|ADVICE] <text> | "
                f"because: <why> | check: <{'|'.join(CHECKS)}>`: {ln!r}"
            )
        cid, level, text, because, check = m.groups()
        if check not in CHECKS:
            raise FormatError(f"{cid}: check must be one of {CHECKS}, got {check!r}")
        if cid in seen:
            raise FormatError(f"duplicate criterion id {cid}")
        seen.add(cid)
        out.append(Criterion(cid, level, text, because, check))
    if not out:
        raise FormatError("Acceptance criteria has no `Cn [BLOCK|ADVICE] …` line")
    return out


def check_criteria(crit: list[Criterion], size: str, touches: list[str]) -> None:
    blocks = sum(c.level == "BLOCK" for c in crit)
    if blocks > MAX_BLOCK[size]:
        raise FormatError(
            f"{blocks} BLOCK criteria, Size {size} allows {MAX_BLOCK[size]}"
        )
    if len(crit) > MAX_TOTAL[size]:
        raise FormatError(f"{len(crit)} criteria, Size {size} allows {MAX_TOTAL[size]}")
    owner = [c.id for c in crit if c.check == "owner"]
    if len(owner) > MAX_OWNER:
        raise FormatError(
            f"{len(owner)} criteria with check: owner ({', '.join(owner)}), "
            f"at most {MAX_OWNER}"
        )
    tests = [c.id for c in crit if c.check == "test"]
    if tests and not set(touches) & {"code", "db", "ui"}:
        raise FormatError(
            f"{', '.join(tests)}: check: test, but Touches has none of code/db/ui"
        )


def parse_questions(body: str) -> list[str]:
    """The question lines (normalised), [] when the section is empty."""
    if body.strip().lower() in NONE:
        return []
    out = []
    for ln in lines(body):
        m = _Q.match(ln)
        if not m:
            raise FormatError(
                "question line does not match `Q: … | default: … | matters: … "
                f"| found: …`: {ln!r}"
            )
        if not m.group(2):
            raise FormatError(f"question without a default: {ln!r}")
        out.append(ln)
    return out


def effective_size(size: str, touches: list[str]) -> str:
    """Code raises Size, never lowers it: ui+db -> L, ui or db -> M."""
    t = set(touches)
    floor = "L" if {"ui", "db"} <= t else "M" if t & {"ui", "db"} else "S"
    return max(size, floor, key=SIZES.index)


def criteria_sha(plan_text: str) -> str:
    """sha256 of the normalised Acceptance criteria section."""
    body = sections(plan_text).get("Acceptance criteria", "")
    norm = "\n".join(" ".join(ln.split()) for ln in body.split("\n") if ln.strip())
    return hashlib.sha256(norm.encode()).hexdigest()


def frozen(pump2: dict[str, Any]) -> bool:
    """A card planned in the dv-d5aa format; older cards keep the old rules."""
    return bool(pump2.get("criteria_sha"))


def plan_criteria(wt: Path) -> list[Criterion]:
    f = wt / ".pump" / "plan.md"
    if not f.exists():
        return []
    try:
        return parse_criteria(sections(f.read_text()).get("Acceptance criteria", ""))
    except FormatError:
        return []


# ---- review remarks --------------------------------------------------------
@dataclass(frozen=True)
class Remark:
    tag: str  # Cn, Dn or ""
    text: str  # the whole remark line(s)
    blocking: bool


def remarks(body: str) -> list[str]:
    """Numbered items of a remarks section, continuation lines joined."""
    items: list[str] = []
    for ln in body.split("\n"):
        m = _REMARK.match(ln)
        if m:
            items.append(m.group(1).strip())
        elif ln.strip() and items:
            items[-1] += " " + ln.strip()
    return items


def classify(body: str, crit: list[Criterion]) -> list[Remark]:
    """Blocking = names a BLOCK Cn of the frozen plan or a D-class, with
    non-empty evidence. Everything else is advice."""
    block = {c.id for c in crit if c.level == "BLOCK"}
    out = []
    for item in remarks(body):
        m = _TAG.match(item)
        tag = m.group(1) if m else ""
        ev = re.search(r"\|\s*evidence:\s*(.*?)\s*$", item)
        has_ev = bool(ev and ev.group(1))
        ok = bool(tag) and (tag in block or tag in DEFECTS) and has_ev
        out.append(Remark(tag, item, ok))
    return out


def judge(body: str, crit: list[Criterion]) -> tuple[list[Remark], list[Remark], str]:
    """(blocking, advice, the advice tail for the comment) of a judge
    stage's remarks: review and every custom stage go through this."""
    rs = classify(body, crit)
    blocking = [r for r in rs if r.blocking]
    advice = [r for r in rs if not r.blocking]
    tail = (
        "\nadvice (не блокирует):\n" + "\n".join(f"- {r.text}" for r in advice)
        if advice
        else ""
    )
    return blocking, advice, tail


# ---- scores (dv-3b99) --------------------------------------------------------
_SCORE = re.compile(r"^\s*(?:[-*]\s+)?(C\d+)\s*:\s*(\d+)\s*(?:\|.*)?$")


def parse_scores(text: str) -> list[tuple[str, int]]:
    """The ``## Scores`` lines of a report (``C1: 8 | evidence``), score
    1-10; a line that does not parse or is out of range is left out."""
    out: list[tuple[str, int]] = []
    for ln in sections(text).get("Scores", "").split("\n"):
        m = _SCORE.match(ln)
        if m and 1 <= int(m.group(2)) <= 10:
            out.append((m.group(1), int(m.group(2))))
    return out


def scores_warning(text: str, got: list[tuple[str, int]]) -> str:
    """A warning line for the comment, '' when the scores are fine. Scores
    never change the verdict."""
    body = sections(text).get("Scores")
    if body is None:
        return "warning: scores: report has no ## Scores section"
    bad = [ln for ln in lines(body) if not _SCORE.match(ln)]
    if not got or bad:
        return f"warning: scores: {len(bad)} line(s) unparsable, {len(got)} kept"
    return ""


def scores_db() -> Path:
    """bot.db, where stage scores go (tests patch this)."""
    from src.utils.paths import data_layout

    return data_layout().bot_db


def record_scores(
    board: str, card_id: str, stage: str, pump2: dict[str, Any], text: str
) -> str:
    """Store a report's ``## Scores`` (one ``stage_score`` row per criterion)
    and return the comment's warning line ('' when none). Never raises."""
    from src.storage.reactions import record_stage_scores_sync

    got = parse_scores(text)
    warn = scores_warning(text, got)
    err = record_stage_scores_sync(
        scores_db(),
        board=board,
        card_id=card_id,
        stage=stage,
        scores=got,
        size=pump2.get("size"),
        kind=pump2.get("kind"),
    )
    if err:
        warn = (warn + "\n" if warn else "") + f"warning: scores not stored: {err}"
    return warn


# ---- the shared return budget ----------------------------------------------
def take_return(pump2: dict[str, Any]) -> tuple[int, bool]:
    """(the card's return count after this one, over budget). One key
    ``returns`` per card, for every judge stage that returns work."""
    n = int(pump2.get("returns") or 0) + 1
    return n, n > RETURN_BUDGET


# ---- the closing summary ---------------------------------------------------
def tested_ids(wt: Path, files: list[str], ids: list[str]) -> set[str]:
    """The ids among ``ids`` with a ``test_Cn_…`` in one of ``files``."""
    text = ""
    for rel in files:
        f = wt / rel
        if f.is_file():
            text += f.read_text(errors="replace") + "\n"
    return {i for i in ids if re.search(rf"\btest_{i}_\w*", text)}


def summary(wt: Path, outcome: str, advice: list[str] | None = None) -> str:
    """The closing ``проверено / не проверено / советы / посмотри сам`` comment,
    from plan.md, tests.json and the review's advice remarks."""
    crit = plan_criteria(wt)
    files: list[str] = []
    tj = wt / ".pump" / "tests.json"
    try:
        files = list(json.loads(tj.read_text())["files"]) if tj.exists() else []
    except (ValueError, KeyError, TypeError):
        files = []
    have = tested_ids(wt, files, [c.id for c in crit])
    done, not_done, own = [], [], []
    for c in crit:
        ln = f"- {c.id} [{c.level}] {c.text} (check: {c.check})"
        if c.check == "test" and c.id in have:
            done.append(ln + " — test_" + c.id)
        elif c.check == "model" and outcome == "done":
            done.append(ln + " — review")
        elif c.check == "owner":
            own.append(ln)
        else:
            not_done.append(ln)
    if not crit:
        not_done.append("- (plan has no Cn criteria: pre-dv-d5aa format)")
    if files:
        done.append("- tests: " + ", ".join(files))
    if advice is None:
        advice = review_advice(wt, crit)
    assumptions = sections(_read(wt / ".pump" / "plan.md")).get("Assumptions", "")
    if assumptions.lower() not in NONE:
        own += [f"- assumption: {a}" for a in lines(assumptions)]

    def block(head: str, items: list[str]) -> str:
        return f"{head}:\n" + ("\n".join(items) if items else "- —")

    return "\n".join(
        [
            f"summary ({outcome})",
            block("проверено", done),
            block("не проверено", not_done),
            block("советы", [f"- {a}" for a in advice]),
            block("посмотри сам", own),
        ]
    )


def review_advice(wt: Path, crit: list[Criterion]) -> list[str]:
    body = sections(_read(wt / ".pump" / "review.md")).get("Remarks", "")
    return [r.text for r in classify(body, crit) if not r.blocking]


def _read(f: Path) -> str:
    return f.read_text() if f.exists() else ""
