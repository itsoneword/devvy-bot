from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_claude_md_has_no_persona_directive():
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    lowered = text.lower()
    # CLAUDE.md must not instruct readers to adopt the bot soul/persona.
    assert "read `data/soul.md`" not in lowered
    assert "read data/soul.md" not in lowered
    assert "## soul & memory" not in lowered


def test_persona_template_has_no_file_memory_protocol():
    # data/agent.md is gitignored (seeded per-deployment); the tracked
    # source-of-truth for the bot-injected persona is the template.
    # Memory is DB-backed (evaluator -> memory_facts -> PromptComposer), so the
    # persona must NOT carry the retired file-based protocol -- it would send
    # the agent reading and writing memory/*.md that nothing consumes.
    text = (
        (ROOT / "data" / "templates" / "agent.md").read_text(encoding="utf-8").lower()
    )
    assert "index.md" not in text
    assert "memory/sessions" not in text
    assert "session lifecycle" not in text


def test_memory_guidance_lives_in_shared_capabilities():
    # One source of truth for how memory works, injected for every profile.
    text = (
        (ROOT / "data" / "shared-capabilities.md").read_text(encoding="utf-8").lower()
    )
    assert "memory" in text
    assert "never store secrets" in text


def test_claude_md_has_workspace_index():
    text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8").lower()
    assert "profiles.yaml" in text or "workspace" in text
