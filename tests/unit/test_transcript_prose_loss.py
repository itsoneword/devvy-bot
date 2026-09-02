"""Detector for pure-prose assistant turns dropped from a resumed transcript.

Regression guard for the context-loss bug where chat-style personas (e.g. a
chef) lost their own recipe replies on resume — the CLI back-filled a synthetic
"No response requested." placeholder and the model went blind to its own words.
Tool-driven turns must NOT trip the detector, since their results persist.
"""

import json
from pathlib import Path

from src.claude import sdk_integration
from src.claude.sdk_integration import transcript_lost_prose_turns


def _write(tmp_path: Path, monkeypatch, records: list) -> tuple:
    wd = Path("/home/someone/chef-workspace")
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(sdk_integration, "_project_jsonl_dir", lambda _wd: proj)
    sid = "sess-123"
    (proj / f"{sid}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records), encoding="utf-8"
    )
    return sid, wd


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant_text(text):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _assistant_thinking_only():
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "hmm"}],
        },
    }


def _synthetic_placeholder():
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": "<synthetic>",
            "content": [{"type": "text", "text": "No response requested."}],
        },
    }


def _assistant_tool_use(name="Bash"):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": name, "input": {}}],
        },
    }


def _tool_result():
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
    }


def test_pure_prose_loss_is_detected(tmp_path, monkeypatch):
    # chef pattern: user -> thinking -> synthetic placeholder (text never committed)
    sid, wd = _write(
        tmp_path,
        monkeypatch,
        [
            _user("расскажи про суп"),
            _assistant_thinking_only(),
            _synthetic_placeholder(),
        ],
    )
    assert transcript_lost_prose_turns(sid, wd) == 1


def test_committed_text_turn_is_not_lost(tmp_path, monkeypatch):
    sid, wd = _write(
        tmp_path,
        monkeypatch,
        [_user("привет"), _assistant_text("вот рецепт супа...")],
    )
    assert transcript_lost_prose_turns(sid, wd) == 0


def test_tool_turn_losing_only_wrapper_is_not_flagged(tmp_path, monkeypatch):
    # channel pattern: the tool_use persists and carries the meaning, so the
    # dropped post-tool wrap-up is recoverable and must NOT be flagged.
    sid, wd = _write(
        tmp_path,
        monkeypatch,
        [
            _user("сохрани место"),
            _assistant_tool_use("Write"),
            _tool_result(),
            _synthetic_placeholder(),
        ],
    )
    assert transcript_lost_prose_turns(sid, wd) == 0


def test_counts_only_within_lookback_window(tmp_path, monkeypatch):
    records = []
    # 3 old lost-prose turns, then 5 healthy turns
    for _ in range(3):
        records += [_user("q"), _assistant_thinking_only(), _synthetic_placeholder()]
    for _ in range(5):
        records += [_user("q"), _assistant_text("a")]
    sid, wd = _write(tmp_path, monkeypatch, records)
    # turns = [L,L,L,H,H,H,H,H]; last 5 are all healthy -> 0
    assert transcript_lost_prose_turns(sid, wd, lookback_turns=5) == 0
    # last 6 reaches one of the old losses
    assert transcript_lost_prose_turns(sid, wd, lookback_turns=6) == 1
    # widen the window -> all 3 old losses surface
    assert transcript_lost_prose_turns(sid, wd, lookback_turns=8) == 3


def test_missing_transcript_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sdk_integration, "_project_jsonl_dir", lambda _wd: tmp_path / "nope"
    )
    assert transcript_lost_prose_turns("absent", Path("/x")) == 0
