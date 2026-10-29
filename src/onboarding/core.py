"""Onboarding logic with no Telegram in it: state, extraction, outputs.

One source of truth for "configured" (dv-bf1e): the default profile's persona
file no longer starts with ``PERSONA_UNCONFIGURED_MARKER``. Finishing the
interview rewrites that file, which is what flips the gate. Existing installs
have a persona without the marker, so they are configured from day one.
"""

import asyncio
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from ..config.env_file import write_env
from ..model_router import complete_text
from ..utils.constants import PERSONA_UNCONFIGURED_MARKER

logger = structlog.get_logger()

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Agents offered in the interview; devops (this one) is always present.
OPTIONAL_AGENTS: Dict[str, str] = {
    "general": "Личный ассистент",
    "pm": "Планирование и задачи",
    "chef": "Еда",
    "coach": "Спорт и тренировки",
}

TONES: Dict[str, str] = {
    "concise": "Short and to the point: answer first, no filler.",
    "friendly": "Warm and conversational, still concise.",
    "formal": "Formal and precise; no slang.",
}

#: Ready-made starting points: (label, tone, extra agents).
PRESETS: Dict[str, Dict[str, Any]] = {
    "dev": {
        "label": "🛠 Разработчик / сервер",
        "tone": "concise",
        "agents": [],
    },
    "personal": {
        "label": "🏠 Личный ассистент / семья",
        "tone": "friendly",
        "agents": ["general", "pm"],
    },
    "fit": {
        "label": "🥗 Еда и спорт",
        "tone": "friendly",
        "agents": ["chef", "coach"],
    },
}

#: Interview order. Every field is optional; ``None`` means "use the default".
FIELDS = (
    "name",
    "language",
    "timezone",
    "assistant_name",
    "tone",
    "push_back",
    "agents",
    "facts",
)

DEFAULT_ASSISTANT_NAME = "Assistant"


@dataclass
class Answers:
    """What the interview collected so far. ``None`` = not answered/skipped."""

    name: Optional[str] = None
    language: Optional[str] = None
    timezone: Optional[str] = None
    assistant_name: Optional[str] = None
    tone: Optional[str] = None
    push_back: Optional[bool] = None
    agents: Optional[List[str]] = None
    facts: Optional[str] = None
    skipped: List[str] = field(default_factory=list)

    def missing(self) -> List[str]:
        return [f for f in FIELDS if getattr(self, f) is None and f not in self.skipped]


# ---------------------------------------------------------------- the gate


def default_persona_path(deps: Dict[str, Any], settings: Any) -> Path:
    """The persona file whose marker means "not configured"."""
    registry = deps.get("profile_registry")
    if registry:
        return Path(registry.get_default().persona)
    return Path(settings.soul_md_path).parent / "agent.md"


def is_unconfigured(deps: Dict[str, Any], settings: Any) -> bool:
    """True while the default persona still carries the template marker."""
    try:
        with default_persona_path(deps, settings).open(encoding="utf-8") as fh:
            return fh.readline().strip() == PERSONA_UNCONFIGURED_MARKER
    except OSError:
        return False


# ------------------------------------------------------------- validation


def valid_timezone(value: str) -> Optional[str]:
    """The IANA name if ``value`` is one, else None."""
    value = value.strip()
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None
    return value or None


_LANG_RE = re.compile(r"^[A-Za-z][A-Za-z -]{1,30}$")


def clean_language(value: str) -> Optional[str]:
    value = value.strip()
    return value if _LANG_RE.match(value) else None


# ------------------------------------------------- free-form extraction

EXTRACT_SYSTEM = (
    "You extract onboarding answers from a user's message. Reply with ONE JSON "
    "object and nothing else. Never invent values: omit a key the message does "
    "not state."
)

EXTRACT_PROMPT = """\
Keys (all optional):
- name: how to address the user
- language: language to reply in, English name ("Russian")
- timezone: IANA zone ("Europe/Lisbon"), only if stated or clearly implied
- assistant_name: what to call the assistant
- tone: one of concise, friendly, formal
- push_back: true if the assistant should disagree/push back, false if not
- agents: subset of {agents} the user wants
- facts: other durable facts about the user, one short string

Already collected: {known}

User message:
{message}
"""


def parse_extraction(raw: str) -> Dict[str, Any]:
    """Pull the JSON object out of a model reply; junk -> empty."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def merge_extraction(answers: Answers, data: Dict[str, Any]) -> None:
    """Fold validated extraction results into ``answers`` (never overwrite)."""
    name = data.get("name")
    if isinstance(name, str) and name.strip() and answers.name is None:
        answers.name = name.strip()[:60]
    lang = data.get("language")
    if isinstance(lang, str) and answers.language is None:
        answers.language = clean_language(lang)
    tz = data.get("timezone")
    if isinstance(tz, str) and answers.timezone is None:
        answers.timezone = valid_timezone(tz)
    an = data.get("assistant_name")
    if isinstance(an, str) and an.strip() and answers.assistant_name is None:
        answers.assistant_name = an.strip()[:40]
    tone = data.get("tone")
    if tone in TONES and answers.tone is None:
        answers.tone = str(tone)
    pb = data.get("push_back")
    if isinstance(pb, bool) and answers.push_back is None:
        answers.push_back = pb
    agents = data.get("agents")
    if isinstance(agents, list) and answers.agents is None:
        picked = [a for a in agents if a in OPTIONAL_AGENTS]
        if picked:
            answers.agents = picked
    facts = data.get("facts")
    if isinstance(facts, str) and facts.strip() and answers.facts is None:
        answers.facts = facts.strip()[:600]


async def extract_answers(
    text: str, answers: Answers, model: Optional[str] = None
) -> None:
    """Free-form message -> answers, via the model router (never raises).

    ``model`` None = the onboarding call route (models.yaml)."""
    known = {f: getattr(answers, f) for f in FIELDS if getattr(answers, f) is not None}
    prompt = EXTRACT_PROMPT.format(
        agents=sorted(OPTIONAL_AGENTS), known=json.dumps(known), message=text
    )
    try:
        raw = await complete_text(
            prompt, purpose="onboarding", model=model, system_prompt=EXTRACT_SYSTEM
        )
    except Exception as exc:  # the step-by-step path still works without it
        logger.warning("Onboarding extraction failed", error=str(exc))
        return
    merge_extraction(answers, parse_extraction(raw))


# ----------------------------------------------------------------- outputs


def render_identity(answers: Answers) -> str:
    """The identity section that replaces the template's placeholder."""
    name = answers.assistant_name or DEFAULT_ASSISTANT_NAME
    tone = TONES[answers.tone or "concise"]
    lines = [f"You are {name}, the assistant for this bot, accessed via Telegram."]
    if answers.name:
        lines.append(f"You are talking to {answers.name}.")
    if answers.language:
        lines.append(f"Reply in {answers.language} unless asked otherwise.")
    lines.append(f"Tone: {tone}")
    if answers.push_back is False:
        lines.append("Do not argue with the user's decisions; flag risks once.")
    else:
        lines.append(
            "If the user is wrong or a plan is risky, say so plainly and why; "
            "do not just agree."
        )
    lines.append(
        "You have persistent memory and a continuous relationship with your "
        'user; when drawing on something you remember, say so plainly ("from '
        'our last session...").'
    )
    return "# Agent Identity\n\n" + "\n".join(lines) + "\n\n"


def rewrite_persona(path: Path, answers: Answers) -> None:
    """Swap the marker + placeholder identity for the configured one.

    Everything from the template's ``## Environment`` heading on is kept.
    Written last-step-of-the-write via rename so a crash never leaves a
    half-file; removing the marker is what marks the bot configured.
    """
    old = path.read_text(encoding="utf-8")
    if not old.startswith(PERSONA_UNCONFIGURED_MARKER):
        # A re-run over a persona someone has tuned: keep the old one.
        path.with_name(path.name + ".bak").write_text(old, encoding="utf-8")
    idx = old.find("\n## ")
    rest = old[idx + 1 :] if idx != -1 else ""
    new = render_identity(answers) + rest
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(path)


def memory_facts(answers: Answers) -> List[Dict[str, Any]]:
    """Facts about the user, as writer ops (tier core for the stable ones)."""
    ops: List[Dict[str, Any]] = []

    def add(text: str, tier: str, label: str) -> None:
        ops.append(
            {
                "op": "add",
                "kind": "user_fact",
                "tier": tier,
                "content": text,
                "label": label,
                "scope": "private",
            }
        )

    if answers.name:
        add(f"The user's name is {answers.name}.", "core", "name")
    if answers.timezone:
        add(f"The user's timezone is {answers.timezone}.", "core", "timezone")
    if answers.facts:
        add(answers.facts, "general", "about the user")
    return ops


def install_agents(slugs: List[str], config_path: Path) -> List[str]:
    """Append agents' profiles via scripts/install_profiles.py; returns failures."""
    failed: List[str] = []
    script = REPO_ROOT / "scripts" / "install_profiles.py"
    for slug in slugs:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, slug from allowlist
            [sys.executable, str(script), "--add", slug, "--config", str(config_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            logger.warning("install_profiles failed", slug=slug, err=proc.stderr[-300:])
            failed.append(slug)
    return failed


@dataclass
class ApplyReport:
    persona: bool = False
    facts: int = 0
    timezone: Optional[str] = None
    agents_added: List[str] = field(default_factory=list)
    agents_failed: List[str] = field(default_factory=list)


async def apply_answers(
    answers: Answers,
    *,
    persona_path: Path,
    settings: Any,
    env_file: Path,
    memory_writer: Any,
    memory_agent_id: str,
    profiles_config: Optional[Path],
) -> ApplyReport:
    """Write every output. Persona goes LAST: it flips the gate, so a failure
    earlier leaves the bot unconfigured and the owner can simply retry."""
    report = ApplyReport()

    if answers.timezone:
        write_env(env_file, {"USER_TIMEZONE": answers.timezone}, backup=False)
        settings.user_timezone = answers.timezone
        report.timezone = answers.timezone

    ops = memory_facts(answers)
    if ops and memory_writer is not None:
        applied = await memory_writer.apply_ops(memory_agent_id, ops)
        report.facts = int(applied.added)

    if answers.agents and profiles_config is not None:
        failed = await asyncio.to_thread(
            install_agents, list(answers.agents), profiles_config
        )
        report.agents_failed = failed
        report.agents_added = [a for a in answers.agents if a not in failed]

    rewrite_persona(persona_path, answers)
    report.persona = True
    return report
