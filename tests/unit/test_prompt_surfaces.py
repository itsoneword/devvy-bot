"""dv-9d5f: prompt surfaces tell each agent how the pump and the key store work.

Trigger: the gymbro agent filed gb-1a73/75/77/78 as ``todo`` and never started
them; the pump only takes ``in_progress`` cards, so nothing ran. One home per
mechanism: the rules live in ``data/shared-capabilities.md`` (injected into
every chat turn), the mechanics in the MCP tool descriptions, and personas
only point to them. These tests pin the rules, not the exact wording.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

import pytest

from src.claude.agent_tools import AGENT_TOOL_SPECS

ROOT = Path(__file__).resolve().parents[2]
SHARED_CAPS = ROOT / "data" / "shared-capabilities.md"
TEMPLATES = ROOT / "data" / "templates" / "local"
BUILDER_PERSONAS = {
    "gymbro": TEMPLATES / "gymbro" / "persona.md",
    "pl": TEMPLATES / "pl" / "persona.md",
}
# Key-store service each builder persona must name (profiles.yaml / key store).
PERSONA_KEY_SERVICE = {"gymbro": "gymbro", "pl": "pl-openrouter"}
# data/templates/local/ is the owner's own profiles and is not exported, so the
# public tree runs without them.
needs_builder_personas = pytest.mark.skipif(
    not all(p.exists() for p in BUILDER_PERSONAS.values()),
    reason="owner-local builder personas not present (public export)",
)

# Line count of data/shared-capabilities.md before dv-9d5f.
SHARED_CAPS_LINES_BEFORE = 273


def _caps() -> str:
    return SHARED_CAPS.read_text(encoding="utf-8")


def _paragraphs(text: str) -> List[str]:
    """Blank-line separated blocks, lowercased (a bullet list is one block)."""
    return [p.lower() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _tool_description(name: str) -> str:
    for spec in AGENT_TOOL_SPECS:
        if spec.name == name:
            return str(spec.description).lower()
    raise AssertionError(f"tool {name} not registered")


_NOT_START = re.compile(r"(does not|doesn't|do not|don't|never|not) start")


# -- Scenario 1: asked to do a board task -> file it AND job_start(task_id) ----


def test_shared_caps_tell_agent_to_file_and_start_board_task_with_id() -> None:
    blocks = [
        p
        for p in _paragraphs(_caps())
        if "job_start(task_id)" in p
        and ("task_add" in p or "file" in p)
        and re.search(r"\bids?\b", p)
    ]
    assert blocks, (
        "shared caps must say: owner asks to do a board task -> file it (if new) "
        "and call job_start(task_id), reply with the id"
    )


@needs_builder_personas
def test_builder_personas_start_board_tasks_with_job_start_task_id() -> None:
    for slug, path in BUILDER_PERSONAS.items():
        text = path.read_text(encoding="utf-8")
        assert (
            "job_start(task_id)" in text
        ), f"{slug} persona: start = job_start(task_id)"


# -- Scenario 2: only filed -> tell the owner "filed, not started" -------------


def test_shared_caps_say_filing_does_not_start_and_report_filed_not_started() -> None:
    caps = _caps().lower()
    assert re.search(r"filed,? not started", caps), (
        'shared caps must tell an agent that only filed a task to say "filed, '
        'not started"'
    )
    board_blocks = [p for p in _paragraphs(_caps()) if "job_start(task_id)" in p]
    assert any(
        _NOT_START.search(p) for p in board_blocks
    ), "shared caps must say that filing a task does not start it"


def test_task_add_description_says_it_does_not_start_work() -> None:
    desc = _tool_description("task_add")
    assert _NOT_START.search(desc), "task_add description: a new task is parked"
    assert "job_start" in desc or "in_progress" in desc


# -- Scenario 3: owner wants it fast / task is small -> pump is slow ----------


def test_shared_caps_say_pump_is_slow_and_offer_direct_fix_for_small_work() -> None:
    blocks = [
        p
        for p in _paragraphs(_caps())
        if "pump" in p
        and re.search(r"not fast|slow|hours", p)
        and "small" in p
        and re.search(r"directly|yourself|right now|in chat", p)
    ]
    assert blocks, (
        "shared caps must say the pump is not fast (multi-stage, slots, owner "
        "questions) and offer to do small / urgent work directly"
    )


# -- Scenario 4: keys explained once in shared caps; personas name service ----


def test_shared_caps_explain_key_store_once() -> None:
    caps = _caps()
    assert re.search(r"\*\*keys", caps, re.IGNORECASE), "shared caps: a Keys section"
    key_blocks = [
        p
        for p in _paragraphs(caps)
        if "/setkey" in p and "sandbox_keys" in p and "withkeys" in p
    ]
    assert key_blocks, "one Keys section names /setkey, sandbox_keys and withkeys"
    assert caps.count("/setkey") <= 2, "the /setkey contract is stated once"


@needs_builder_personas
def test_builder_personas_name_their_key_service_but_do_not_restate_contract() -> None:
    for slug, path in BUILDER_PERSONAS.items():
        text = path.read_text(encoding="utf-8")
        assert PERSONA_KEY_SERVICE[slug] in text, f"{slug} persona names its service"
        assert "/setkey" not in text, f"{slug} persona restates /setkey (use Keys)"
        assert (
            "scripts/withkeys" not in text
        ), f"{slug} persona restates the withkeys contract (use Keys)"


# -- Scenario 5: shared caps shorter; false "one board" claim gone -------------


def test_shared_caps_shorter_than_before() -> None:
    lines = len(_caps().splitlines())
    assert lines < SHARED_CAPS_LINES_BEFORE, f"{lines} lines, must be < 273"


def test_shared_caps_drop_false_single_board_claim() -> None:
    caps = _caps().lower()
    assert "no second, agent-only board" not in caps
    assert "one board, shared by every agent" not in caps


# -- added ---------------------------------------------------------------------


@needs_builder_personas
def test_builder_personas_do_not_job_start_board_tasks_as_prompts() -> None:
    # The persona line that contradicted the job_start tool description.
    for slug, path in BUILDER_PERSONAS.items():
        text = path.read_text(encoding="utf-8").lower()
        assert "self-contained prompt\n   naming" not in text
        assert not re.search(r"self-contained prompt\s+naming", text), slug


def test_job_start_description_says_filing_does_not_start() -> None:
    desc = _tool_description("job_start")
    assert "task_id" in desc
    assert _NOT_START.search(desc) and re.search(
        r"fil(e|ing)|creat", desc
    ), "job_start description: creating a task does not start it"


def test_shared_caps_keep_verbatim_pump_answer_rule() -> None:
    # Guard: compressing the pipeline section must not lose the answer rule.
    caps = _caps()
    assert "pump_pending" in caps
    assert "pump_answer" in caps
    assert "verbatim" in caps.lower()


# --- dv-cf07: task status is not a memory fact (remember surfaces) ---------


def test_remember_tells_model_task_status_is_not_a_fact():
    """dv-cf07: the remember tool wrote both members of the stale pump
    concurrency pair (3 vs 5). Its description must say task/ticket status
    (merged, started, raised X to N) belongs on the board, and 'general' no
    longer advertises status."""
    desc = _tool_description("remember").lower()
    for word in ("merged", "started", "raised"):
        assert word in desc, word
    assert "board" in desc
    assert "history, status" not in desc

    caps = _caps()
    block = next(p for p in _paragraphs(caps) if "save a fact yourself" in p)
    assert "history, status" not in block
    assert "merged" in block and "board" in block
