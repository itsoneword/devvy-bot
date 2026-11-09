"""External project grants and guides use the frozen bundle's composer."""

from pathlib import Path

from src.claude.window_bundle import WindowBundleProvider
from src.profiles.registry import load_profile_registry


def test_frozen_bundle_composer_uses_explicit_source_and_write_roots(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    readonly = tmp_path / "readonly"
    for path in (workspace, repo, readonly):
        path.mkdir()
    (workspace / "persona.md").write_text("Builder persona")
    (repo / "CLAUDE.md").write_text("Use the repo board for draft cards.")
    config = tmp_path / "profiles.yaml"
    config.write_text(
        "profiles:\n"
        f"  - slug: builder\n    name: Builder\n    working_dir: {workspace}\n"
        f"    memory_dir: {workspace}/memory\n    persona: {workspace}/persona.md\n"
        "    default: true\n"
        f"    extra_dirs: [{repo}]\n"
        f"    sandbox_read_dirs: [{readonly}]\n"
        f"    project_instructions: {repo}/CLAUDE.md\n"
    )
    registry = load_profile_registry(config, data_root=None)
    provider = WindowBundleProvider(None, None, None, profile_registry=registry)  # type: ignore[arg-type]
    composer = provider._composer_for(workspace)
    assert composer is not None
    bundle = composer.compose(working_directory=workspace)

    scope = bundle.split("\n\n", 1)[0]
    assert str(workspace) in scope
    assert str(repo) in scope
    assert str(readonly) not in scope
    assert bundle.count("Use the repo board for draft cards.") == 1
    assert "<project_instructions" in bundle
