from pathlib import Path

from src.claude.prompt_composer import PromptComposer
from src.claude.sdk_integration import ClaudeSDKManager
from src.profiles.registry import ProfileDefinition, ProfileRegistry


def _profile(slug, root: Path, default=False):
    return ProfileDefinition(
        slug=slug,
        name=slug,
        working_dir=root,
        memory_dir=root / "memory",
        persona=root / "persona.md",
        default=default,
    )


def test_selects_composer_by_working_dir(tmp_path):
    dev = tmp_path / "dev"
    chan = tmp_path / "chan"
    # Persona content carries the slug so compose() output is distinguishable.
    personas = {dev: "devops", chan: "channel"}
    for d, slug in personas.items():
        (d / "memory").mkdir(parents=True)
        (d / "persona.md").write_text(f"I am the {slug} persona", encoding="utf-8")
    reg = ProfileRegistry(
        [_profile("devops", dev, default=True), _profile("channel", chan)]
    )

    mgr = ClaudeSDKManager.__new__(ClaudeSDKManager)  # bypass full __init__
    mgr.prompt_composer = PromptComposer(dev / "memory", dev / "persona.md")
    mgr.profile_registry = reg
    mgr._profile_composers = {}

    c_dev = mgr._composer_for(dev)
    c_chan = mgr._composer_for(chan)
    assert c_dev is not c_chan
    assert "devops" in c_dev.compose(dev)
    assert "channel" in c_chan.compose(chan)


def test_falls_back_to_default_composer_when_no_registry(tmp_path):
    base = tmp_path / "base"
    (base / "memory").mkdir(parents=True)
    mgr = ClaudeSDKManager.__new__(ClaudeSDKManager)
    mgr.prompt_composer = PromptComposer(base / "memory", base / "agent.md")
    mgr.profile_registry = None
    mgr._profile_composers = {}
    assert mgr._composer_for(Path("/anything")) is mgr.prompt_composer
