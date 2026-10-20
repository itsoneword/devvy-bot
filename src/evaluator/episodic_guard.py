"""Deterministic backstop rejecting episodic memory candidates (dv-7517).

The extraction prompt's durability rules (prompts.py) are the primary
defense; a rule in a prompt is not a guarantee (docs/FINDINGS.md
2026-08-04), so the same rule is applied structurally here where it can be
counted. One pure function, one call site (service.py candidate loop) — a
one-line revert.

Honest spec: keyword matching over mixed RU/EN text. It will occasionally
reject a durable fact phrased episodically and miss episodic rows phrased
unusually. Mitigations: general tier only (pinned is NEVER rejected — an
allergy phrased with a date must not be lost); every rejection is logged
with full content and counted in op_stats.rejected_episodic; word lists are
tuned against the labeled corpus of 2026-08-14 (the 64 rows closed as
'cleanup-episodic-2026-08-14' = positive set, the deliberately preserved
durable facts = negative set — tests/unit/test_evaluator/
test_episodic_guard.py asserts zero false positives on the latter).
"""

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Durable vetoes. Any hit → the candidate is kept, whatever else matches.
# Two lists: WEAK vetoes lose to STRONG episodic signals (a day-summary row
# comparing intake to "the 160g target" is still a day summary); HARD vetoes
# always win (medical context, product references, stated patterns).
# ---------------------------------------------------------------------------

_HARD_VETO = re.compile(
    r"(?xi)"
    r"product\ reference | use\ (this\ )?for\ future | per\ package"
    r"| label | этикетк"  # figures corrected from a package label
    r"| recurring | pattern | паттерн | trend | тренд"
    r"| always | всегда | usually | обычно | every\b | кажд"
    r"| prefer | предпочита | allerg | аллерг"
    r"| injur | травм | pain | боль | spasm | спазм | doctor | врач"
    r"| medical | медицин | cycle | цикл | менстру | thyroid | щитовидк"
    r"| supplement | ferritin | феррит | железо"
    r"| advised\ as | hard\ no | рекомендовано"
    r"| schedule | расписани | weekly | еженедель | в\ неделю | x/week | /wk"
    r"| TDEE | maintenance | поддержани"
    # Protocol-defining weigh-in rows ("same conditions, comparable", trend
    # series) stay; bare readings do not (owner 2026-08-15: weight readings
    # are analysis data for data.db, not identity facts).
    r"| comparab | сопостав | future | будущ"
)

_WEAK_VETO = re.compile(
    r"(?xi)"
    r"goal | цель | target | норма\b | таргет"
    r"| program | программ | протокол | split\b"
    r"| plan\b | план\b"
)

# ---------------------------------------------------------------------------
# Episodic signals.
# ---------------------------------------------------------------------------

_MEAL_WORD = re.compile(
    r"(?i)\b(breakfast|lunch|dinner|snacks?|desserts?|meals?"
    r"|завтрак|обед|ужин|перекус|десерт)\b"
)
_FOOD_NUMBER = re.compile(
    r"(?i)(\d+[.,]?\d*\s*(kcal|ккал|кал\b)"  # calories
    r"|[БЖУ]\s?~?\d"  # macro shorthand Б125 / Ж62 / У140
    r"|\d+\s*(g|г|ml|мл|мл\.)\b)"  # weights/volumes
)
# Day-level nutrition close-outs: "Day 7 ... tracking closed: total ~1662
# kcal", "final nutrition total", "итого за день".
_DAY_CLOSE = re.compile(
    r"(?i)(tracking\ closed|nutrition\ (total|tracking)"
    r"|итого\ за\ день|total\ за\ день|day('s)?\ (summary|total))".replace("\\ ", "\\s")
)
# Wearable daily readouts: "Move 1054/600 kcal", "Exercise 138/40min".
_WATCH = re.compile(r"(?i)\b(Move|Exercise)\s*~?\d+\s*/\s*\d+")
# One night's sleep with an hour count and no frequency framing.
_SLEEP_NIGHT = re.compile(
    r"(?i)\b(slept|спал[аи]?)\b[^.]{0,25}?\d+[.,]?\d*\s*(hours?|h\b|час)"
)
# Set-by-set actuals in a session context.
_SETS = re.compile(r"(?i)\d+\s*[x×х]\s*\d+")
_SESSION_WORD = re.compile(
    r"(?i)\b(session|gym|workout|reps?|sets?"
    r"|трениров\w*|подход\w*|повтор\w*|сет(а|ов|ы)?)\b"
)
# A calendar date: 07.08 / 07.08.2026 / 2026-08-07.
_DATE = re.compile(r"\b\d{1,2}\.\d{2}(\.\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b")
# A log line: content that OPENS with a bare date ("11.08 evening: beer…").
_DATE_PREFIX = re.compile(r"^\s*\d{1,2}\.\d{2}(\.\d{2,4})?\s")
# Past-tense consumption verbs tying the date to a one-off ingestion event.
_CONSUMED = re.compile(
    r"(?i)\b(ate|eaten|drank|drinking|intake|smoked"
    r"|ел[аи]?|съел[аи]?|выпил[аи]?|попил[аи]?|выкурил[аи]?)\b"
)
# One night's sleep told by the clock ("отбой 22:30, уснул быстро, подъём
# 7:10", "460 мин сна") rather than by an hour count.
_SLEEP_CLOCK = re.compile(
    r"(?i)(уснул|отбой|подъ[её]м|проснул\w*|мин\s+сна|woke\s+(up\s+)?at|bedtime)"
)
# A bare scale reading. Protocol rows carry trend/comparability wording and
# are hard-vetoed above.
_WEIGHIN = re.compile(r"(?i)(взвешивани|weigh-?in|fasted\s+weight)")
_KG = re.compile(r"(?i)\d+[.,]?\d*\s*(kg|кг)\b")
# One night's bedroom conditions ("Night 11->12.08 bedroom temperature was
# 19.9C").
_NIGHT_TEMP = re.compile(
    r"(?i)(bedroom|спальн\w*)[^.]{0,40}(температур\w*|temperature)"
    r"|(температур\w*|temperature)[^.]{0,40}(bedroom|спальн\w*)"
)


def classify(content: str, tier: str = "general") -> Optional[str]:
    """Return a rejection reason if ``content`` looks episodic, else None.

    Pinned- and core-tier candidates are never rejected.
    """
    if tier in ("pinned", "core"):
        return None
    if _HARD_VETO.search(content):
        return None

    strong: Optional[str] = None
    if _DAY_CLOSE.search(content) and _FOOD_NUMBER.search(content):
        strong = "day nutrition summary"
    elif _WATCH.search(content):
        strong = "daily activity readout"
    elif (
        _MEAL_WORD.search(content)
        and _FOOD_NUMBER.search(content)
        and _DATE.search(content)
    ):
        # Date required: an undated meal+numbers line is usually a routine
        # ("protein breakfast 9-10am, 120-150g/day"), not a log entry.
        strong = "single meal log"
    elif _DATE_PREFIX.search(content) and _FOOD_NUMBER.search(content):
        strong = "dated log line"
    if strong:
        return strong

    if _WEAK_VETO.search(content):
        return None
    if _SLEEP_NIGHT.search(content):
        return "single night's sleep"
    if _DATE.search(content) and _SLEEP_CLOCK.search(content):
        return "single night's sleep"
    if _DATE.search(content) and _WEIGHIN.search(content) and _KG.search(content):
        return "single weigh-in"
    if _DATE.search(content) and _NIGHT_TEMP.search(content):
        return "single night's conditions"
    if _SETS.search(content) and _SESSION_WORD.search(content):
        return "single session actuals"
    if _DATE.search(content) and _CONSUMED.search(content):
        return "dated one-off consumption"
    return None


# ---------------------------------------------------------------------------
# Ticket / job status (dv-7e11, b2). The extraction prompt already says "a
# ticket's current status ... Emit nothing" (prompts.py, Durability rules);
# the Jev screen's targeted extractor wrote MORE of them (29 -> 49 on devops),
# so the same rule is counted structurally here. Used on the screen path only.
# ---------------------------------------------------------------------------

_TICKET_ID = re.compile(r"(?i)\bdv-[0-9a-f]{4}\b|\b(ticket|тикет\w*|задач\w*)\b")
_COMMIT_REF = re.compile(
    r"(?i)\bcommit\w*\s+`?[0-9a-f]{7,40}\b|\b[0-9a-f]{7,40}\s+commit"
)
_JOB_WORD = re.compile(r"(?i)\b(bgjob|taskpump|job|джоб\w*|agentjob)\b")
_STATUS_WORD = re.compile(
    r"(?i)(in[\s_-]progress|in[\s_-]review|\bwaiting[\s_]user\b|\bdone\b|\bshipped\b"
    r"|\blanded\b|\bmerged\b|\bcommitted\b|\bimplemented\b|\bcompleted\b"
    r"|\bstarted\b|\bfinished\b|\bqueued\b|\bclosed\b|\bmoved to\b"
    r"|\bstatus\b|в работе|на ревью|выполнен\w*|завершен\w*|завершён\w*"
    r"|запущен\w*|закоммичен\w*|влит\w*|готов\w*)"
)
# A decision, rule or lasting design fact keeps the row even if it names a ticket.
_TICKET_VETO = re.compile(
    r"(?i)(decid|decision|rule|always|never|must\b|should\b|owner|policy|because"
    r"|реши\w*|правил\w*|всегда|никогда|нельзя|нужно|владелец)"
)


def classify_ticket_status(content: str, tier: str = "general") -> Optional[str]:
    """Reason if ``content`` is a ticket/commit/job status line, else None.

    Pinned/core are never rejected. A decision/rule wording vetoes."""
    if tier in ("pinned", "core"):
        return None
    if _TICKET_VETO.search(content):
        return None
    if not _STATUS_WORD.search(content):
        return None
    if _TICKET_ID.search(content):
        return "ticket status"
    if _COMMIT_REF.search(content):
        return "commit status"
    if _JOB_WORD.search(content):
        return "job status"
    return None
