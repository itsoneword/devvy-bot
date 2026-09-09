"""A window roll on the Codex backend must reseed exactly like Claude (dv-0c01).

Owner's fourth question after the first live Codex turns: does rotation still
work? Two things have to hold on the Codex path.

1. The turn after a roll materializes a NEW `window_bundles` row for the new
   window id — that is the frozen context the fresh Codex session opens on.
2. The seed the roll arms is actually built, which requires the outgoing
   window's `messages` rows to be reachable. `window_conversation_text` joins
   `messages` to `sessions` on `project_path`, so a Codex turn that never
   wrote a `sessions` row produced an EMPTY conversation and therefore an
   empty seed — the FK bug and the rotation bug are the same bug.
"""

import stat
import tempfile
from pathlib import Path

import pytest

from src.agent_backends.codex_cli import CodexCliIntegration, CodexSessionStore
from src.bot.orchestration.windowing import window_conversation_text
from src.claude.prompt_composer import PromptComposer
from src.claude.session import SessionManager
from src.claude.window_bundle import WindowBundleProvider
from src.config.settings import Settings
from src.storage.facade import Storage
from src.storage.memory_repository import MemoryRepository
from src.storage.session_storage import SQLiteSessionStorage
from src.storage.window_bundle_repository import WindowBundleRepository
from src.storage.window_repository import WindowRepository

USER_ID = 501


def _fake_codex(tmp_path: Path) -> Path:
    """Stand-in `codex` that mints a fresh thread id per invocation."""
    script = tmp_path / "codex"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        'outfile=""\n'
        'args=("$@")\n'
        "for ((i=0; i<${#args[@]}; i++)); do\n"
        '  if [[ "${args[$i]}" == "-o" ]]; then outfile="${args[$((i+1))]}"; fi\n'
        "done\n"
        'tid="th-$(date +%s%N)"\n'
        'echo "{\\"type\\":\\"thread.started\\",\\"thread_id\\":\\"$tid\\"}"\n'
        'echo \'{"type":"item.completed","item":{"type":"agent_message",'
        '"text":"reply"}}\'\n'
        'echo \'{"type":"turn.completed","usage":{"input_tokens":90000,'
        '"output_tokens":500}}\'\n'
        'if [[ -n "$outfile" ]]; then echo -n "reply" > "$outfile"; fi\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return script


@pytest.fixture
async def storage():
    with tempfile.TemporaryDirectory() as temp_dir:
        st = Storage(f"sqlite:///{Path(temp_dir) / 'bot.db'}")
        await st.initialize()
        yield st
        await st.close()


@pytest.fixture
def settings():
    return Settings(telegram_bot_token="x", telegram_bot_username="x")


async def test_roll_on_codex_materializes_a_new_bundle_and_a_usable_seed(
    storage, settings, tmp_path
):
    workdir = tmp_path / "ws"
    workdir.mkdir()
    persona = tmp_path / "agent.md"
    persona.write_text("You are the coach.", encoding="utf-8")

    window_repo = WindowRepository(storage.db_manager)
    bundle_repo = WindowBundleRepository(storage.db_manager)
    memory_repo = MemoryRepository(storage.db_manager)
    await memory_repo.add_fact("devops", "general", "trains on tuesdays")

    provider = WindowBundleProvider(
        window_repo=window_repo,
        bundle_repo=bundle_repo,
        memory_repo=memory_repo,
        profile_registry=None,
        default_composer=PromptComposer(memory_path=tmp_path, agent_md_path=persona),
    )
    integ = CodexCliIntegration(
        config=settings,
        codex_bin=str(_fake_codex(tmp_path)),
        session_store=CodexSessionStore(tmp_path / "codex_sessions.json"),
        bundle_provider=provider,
        session_manager=SessionManager(
            settings, SQLiteSessionStorage(storage.db_manager)
        ),
    )

    state = await window_repo.get_or_create(USER_ID, workdir)
    first_window = state.window_id

    response = await integ.run_command(
        prompt="как мне тренироваться",
        working_directory=workdir,
        user_id=USER_ID,
        force_new=True,
    )
    await storage.save_claude_interaction(
        user_id=USER_ID,
        session_id=response.session_id,
        prompt="как мне тренироваться",
        response=response,
        window_id=first_window,
    )
    await window_repo.record_turn(
        USER_ID,
        workdir,
        backend="openai_codex",
        added_tokens=90_500,
        context_tokens=90_000,
    )

    # The outgoing window's conversation must be readable — this is what the
    # evaluator and the rotation seed both consume, and it is empty without
    # the `sessions` row the Codex turn now writes.
    state = await window_repo.get_or_create(USER_ID, workdir)
    conversation = await window_conversation_text(storage, USER_ID, workdir, state)
    assert "как мне тренироваться" in conversation
    assert "reply" in conversation

    first_bundle = await bundle_repo.get(USER_ID, workdir, first_window)
    assert first_bundle is not None

    # Roll, then take one more Codex turn.
    old, new = await window_repo.roll_window(
        USER_ID, workdir, new_backend="openai_codex"
    )
    assert (old, new) == (first_window, first_window + 1)
    assert await bundle_repo.get(USER_ID, workdir, new) is None

    await integ.run_command(
        prompt="а сегодня?",
        working_directory=workdir,
        user_id=USER_ID,
        force_new=True,
    )

    rolled_bundle = await bundle_repo.get(USER_ID, workdir, new)
    assert rolled_bundle is not None
    assert rolled_bundle.window_id == new
    # The old window's frozen text is untouched by the roll.
    assert (await bundle_repo.get(USER_ID, workdir, old)).bundle_sha256 == (
        first_bundle.bundle_sha256
    )
