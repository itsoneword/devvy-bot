"""Tests for the destructive-op classifier (confirmation gate)."""

from pathlib import Path

from src.security.destructive import classify

CWD = Path("/home/testuser/Devvybot/devvy_bot")


def _bash(command: str):
    return classify("Bash", {"command": command}, CWD)


def test_aster_order_scripts_are_gated():
    assert _bash("node /home/testuser/tools/fetchcrypto/buy.js market BTCUSDT 200")
    assert _bash(
        "node /home/testuser/tools/fetchcrypto/perp.js market XAUUSDT LONG 10 14"
    )
    assert _bash("node buy.js limit BTCUSDT 0.001 65000")


def test_aster_report_is_not_gated():
    assert _bash("node /home/testuser/tools/fetchcrypto/index.js wallet1") is None
    assert _bash("node /home/testuser/tools/fetchcrypto/index.js") is None


def test_mexc_order_scripts_are_also_gated():
    # Same filenames, different tool (fetchmexc) -- the rule matches on
    # buy.js/perp.js regardless of which exchange script it is, so the
    # reason text must not name a specific exchange.
    reason = _bash(
        "node /home/testuser/tools/fetchmexc/perp.js limit BTC_USDT LONG 5 10 55000"
    )
    assert reason
    assert "Aster" not in reason


def test_syntax_check_is_not_gated():
    # `node --check <file>` only parses the script, never executes it --
    # cannot possibly place an order.
    assert _bash("node --check perp.js") is None
    assert _bash("node --check buy.js && echo ok") is None
    # But a real invocation later in the same command still gates.
    assert _bash(
        'node --check perp.js && echo ok\necho "no" | node perp.js limit BTC_USDT LONG 5 10 55000'
    )


def test_everyday_commands_pass():
    assert _bash("ls -la") is None
    assert _bash("git status") is None
    assert _bash("cat buy.js") is None  # reading the script, not running it


def test_known_destructive_still_gated():
    assert _bash("rm -rf /tmp/foo")
    assert _bash("git reset --hard HEAD~1")
