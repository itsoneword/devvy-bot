from pathlib import Path

from src.agent_backends.codex_cli import CodexSessionStore


def test_record_and_lookup_by_user_and_dir(tmp_path):
    store = CodexSessionStore(tmp_path / "codex_sessions.json")
    store.record(
        user_id=7,
        working_dir=Path("/home/me/proj"),
        thread_id="th-1",
        input_tokens=10,
        output_tokens=5,
    )
    assert store.thread_for(7, Path("/home/me/proj")) == "th-1"
    assert store.thread_for(7, Path("/other")) is None
    assert store.thread_for(99, Path("/home/me/proj")) is None


def test_record_updates_cumulative_tokens(tmp_path):
    store = CodexSessionStore(tmp_path / "s.json")
    store.record(7, Path("/p"), "th-1", 10, 5)
    store.record(7, Path("/p"), "th-1", 3, 2)
    info = store.info_for(7, Path("/p"))
    assert info["cumulative_tokens"] == 20
    assert info["session_id"] == "th-1"


def test_persists_across_instances(tmp_path):
    path = tmp_path / "s.json"
    CodexSessionStore(path).record(7, Path("/p"), "th-1", 1, 1)
    assert CodexSessionStore(path).thread_for(7, Path("/p")) == "th-1"


def test_sessions_for_user_returns_all_dirs(tmp_path):
    store = CodexSessionStore(tmp_path / "s.json")
    store.record(7, Path("/a"), "th-a", 1, 1)
    store.record(7, Path("/b"), "th-b", 1, 1)
    dirs = {Path(s["working_dir"]) for s in store.sessions_for_user(7)}
    assert dirs == {Path("/a"), Path("/b")}
