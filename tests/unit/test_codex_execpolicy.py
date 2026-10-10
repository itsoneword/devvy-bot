"""Codex money-script execpolicy guard (dv-4fc2)."""

import json
import shutil
import stat
import subprocess
from pathlib import Path
from typing import List

import pytest

from src.agent_backends import codex_cli
from src.agent_backends.codex_cli import CodexCliIntegration, InMemoryCodexSessionStore
from src.agent_backends.codex_execpolicy import (
    RULES_FILENAME,
    ensure_rules,
    render_rules,
    script_path_variants,
)
from src.config.settings import Settings
from src.security.destructive import (
    EXCHANGE_TOOL_DIRS,
    MONEY_SCRIPT_NAMES,
    classify,
)

TOOLS = Path("/home/testuser/tools")


def test_every_money_script_is_forbidden_in_every_spelling():
    variants = set(script_path_variants(TOOLS))
    for name in MONEY_SCRIPT_NAMES:
        assert f"{name}.js" in variants
        assert f"./{name}.js" in variants
        for d in EXCHANGE_TOOL_DIRS:
            assert f"{TOOLS}/{d}/{name}.js" in variants
            assert f"~/tools/{d}/{name}.js" in variants


def test_reports_are_not_in_the_list():
    variants = script_path_variants(TOOLS)
    for report in ("index.js", "orders.js", "analytics.js", "query_trades.js"):
        assert not any(v.endswith(report) for v in variants)


def test_codex_list_and_claude_gate_share_one_source():
    """Every script Codex forbids is one the Claude gate confirms."""
    for name in MONEY_SCRIPT_NAMES:
        cmd = f"node {TOOLS}/fetchcrypto/{name}.js x"
        assert classify("Bash", {"command": cmd}, TOOLS) is not None


def test_rules_forbid_with_justification():
    text = render_rules(TOOLS)
    assert 'decision = "forbidden"' in text
    assert "Claude backend" in text
    assert 'pattern = ["node", ' in text
    assert 'pattern = ["/usr/bin/node", ' in text


def test_ensure_rules_writes_and_is_idempotent(tmp_path):
    path = ensure_rules(tmp_path, TOOLS)
    assert path == tmp_path / "rules" / RULES_FILENAME
    assert path.read_text() == render_rules(TOOLS)
    mtime = path.stat().st_mtime_ns
    ensure_rules(tmp_path, TOOLS)
    assert path.stat().st_mtime_ns == mtime  # unchanged content -> no rewrite


def test_ensure_rules_repairs_a_tampered_file(tmp_path):
    path = ensure_rules(tmp_path, TOOLS)
    path.write_text("# emptied\n")
    ensure_rules(tmp_path, TOOLS)
    assert path.read_text() == render_rules(TOOLS)


def _check(rules: Path, argv: List[str]) -> str:
    out = subprocess.run(
        ["codex", "execpolicy", "check", "-r", str(rules), *argv],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    return json.loads(out).get("decision", "allow")


@pytest.mark.skipif(shutil.which("codex") is None, reason="codex CLI not installed")
@pytest.mark.parametrize(
    "argv, expected",
    [
        (["node", "perp.js", "market", "XAUUSDT", "LONG", "10", "14"], "forbidden"),
        (["node", f"{TOOLS}/fetchcrypto/buy.js", "market", "BTC", "1"], "forbidden"),
        (["node", "transfer.js"], "forbidden"),
        (["node", "./fetchmexc/sell.js"], "forbidden"),
        (["/usr/bin/node", "close.js"], "forbidden"),
        (["node", "index.js", "--quick"], "allow"),
        (["node", f"{TOOLS}/fetchcrypto/orders.js"], "allow"),
    ],
)
def test_real_codex_execpolicy_decisions(tmp_path, argv, expected):
    # Loading also runs the rules' own match/not_match self-tests.
    rules = ensure_rules(tmp_path, TOOLS)
    assert _check(rules, argv) == expected


def _fake_codex(tmp_path: Path) -> Path:
    marker = tmp_path / "codex_ran"
    script = tmp_path / "codex"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f"touch {marker}\n"
        'echo \'{"type":"thread.started","thread_id":"th-1"}\'\n'
        'echo \'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\'\n'
        'echo \'{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\'\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return script


def _integ(tmp_path: Path) -> CodexCliIntegration:
    settings = Settings(
        telegram_bot_token="x",
        telegram_bot_username="x",
        enable_mcp=False,
        mcp_config_path=None,
    )
    return CodexCliIntegration(
        config=settings,
        codex_bin=str(_fake_codex(tmp_path)),
        session_store=InMemoryCodexSessionStore(),
    )


async def test_every_codex_turn_installs_the_guard_first(tmp_path, monkeypatch):
    calls: List[Path] = []

    def record(home: Path) -> Path:
        calls.append(home)
        assert not (tmp_path / "codex_ran").exists()  # before the spawn
        return home

    monkeypatch.setattr(codex_cli, "ensure_money_guard_rules", record)
    resp = await _integ(tmp_path).run_command(
        prompt="hi", working_directory=tmp_path, user_id=7, force_new=True
    )
    assert resp.content == "ok"
    assert calls == [codex_cli.codex_home()]


async def test_no_turn_runs_without_the_guard(tmp_path, monkeypatch):
    def broken(home: Path) -> Path:
        raise OSError("read-only CODEX_HOME")

    monkeypatch.setattr(codex_cli, "ensure_money_guard_rules", broken)
    with pytest.raises(Exception):
        await _integ(tmp_path).run_command(
            prompt="hi", working_directory=tmp_path, user_id=7, force_new=True
        )
    assert not (tmp_path / "codex_ran").exists()
