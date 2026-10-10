"""Tests for the destructive-op classifier (confirmation gate)."""

from pathlib import Path

import pytest

from src.security import destructive
from src.security.destructive import classify

CWD = Path("/home/testuser/Devvybot/devvy_bot")

# The real files on disk right now (confirmed via `ls ~/tools/fetchcrypto
# ~/tools/fetchmexc`), plus one fictitious name that is on neither
# MONEY_SCRIPT_NAMES nor READ_ONLY_SCRIPT_NAMES -- proves the bare-name
# fallback is a real disk lookup, not another hardcoded list.
_FAKE_DISK_SCRIPTS = frozenset(
    {
        "buy",
        "sell",
        "perp",
        "close",
        "modify_order",
        "cancel_spot",
        "transfer",
        "move_stop",
        "index",
        "orders",
        "query_trades",
        "analytics",
        "zzz_brand_new_script",
    }
)


@pytest.fixture
def fake_tools_dir(monkeypatch):
    """Stand in for the real ~/tools/{fetchcrypto,fetchmexc} listing."""
    monkeypatch.setattr(destructive, "_scripts_on_disk", lambda: _FAKE_DISK_SCRIPTS)


def _bash(command: str):
    return classify("Bash", {"command": command}, CWD)


def test_aster_order_scripts_are_gated():
    assert _bash("node /home/testuser/tools/fetchcrypto/buy.js market BTCUSDT 200")
    assert _bash(
        "node /home/testuser/tools/fetchcrypto/perp.js market XAUUSDT LONG 10 14"
    )
    assert _bash("cd /home/testuser/tools/fetchcrypto && node buy.js limit 0.001")


def test_every_money_moving_script_is_gated():
    for script in (
        "sell.js market BTCUSDT 0.001",
        "close.js wallet1 XAUUSDT market",
        "modify_order.js wallet2 BTCUSDT 69000",
        "cancel_spot.js BTCUSDT 62100",
        "transfer.js spot2fut USDT 10",
    ):
        assert _bash(f"cd /home/testuser/tools/fetchcrypto && echo yes | node {script}")
        assert _bash(f"node /home/testuser/tools/fetchcrypto/{script}")


def test_a_brand_new_unlisted_script_is_still_gated():
    # dv-8664: the whole point of the inversion -- a script nobody has named
    # yet still gets a button on day one. move_stop.js ran without one on
    # 2026-10-04 because it wasn't in the old denylist.
    assert _bash("node /home/testuser/tools/fetchcrypto/move_stop.js wallet1 69000")
    assert _bash("node /home/testuser/tools/fetchcrypto/foo_new.js")
    assert _bash("cd /home/testuser/tools/fetchcrypto && node foo_new.js")
    assert _bash("node /home/testuser/tools/fetchmexc/foo_new.js")


def test_every_path_spelling_is_gated():
    for command in (
        "cd /home/testuser/tools/fetchcrypto && node buy.js",
        "cd /home/testuser/tools/fetchcrypto && node ./buy.js",
        "node fetchcrypto/buy.js",
        "node ./fetchcrypto/buy.js",
        "node ~/tools/fetchcrypto/buy.js",
        "node /home/testuser/tools/fetchcrypto/buy.js",
        "cd ~/tools/fetchcrypto && node buy.js",
        "cd /home/testuser/tools/fetchcrypto; node buy.js",
    ):
        assert _bash(command), command


def test_lib_subdir_is_gated():
    # fetchmexc keeps shared client code in lib/ -- still under the dir, so
    # still gated (dv-8664 explicitly covers EXCHANGE_TOOL_DIRS's lib/ subdirs).
    assert _bash("node /home/testuser/tools/fetchmexc/lib/futures-client.js")
    assert _bash("cd /home/testuser/tools/fetchmexc/lib && node futures-client.js")


def test_npm_run_and_npx_in_exchange_dirs_are_gated():
    assert _bash("cd /home/testuser/tools/fetchcrypto && npm run start")
    assert _bash("cd ~/tools/fetchmexc && npx something")


def test_npm_run_outside_exchange_dirs_is_not_gated():
    assert _bash("npm run build") is None
    assert _bash("cd /home/testuser/Devvybot/devvy_bot && npm run build") is None


def test_unresolved_cd_before_node_falls_back_to_disk(fake_tools_dir):
    # Can't tell where a `$VAR` cd lands -- fall back to what's really on
    # disk rather than assume it's safe.
    assert _bash("cd $SOME_DIR && node perp.js")
    assert _bash("cd $SOME_DIR && node unrelated_thing.js") is None


def test_bare_script_name_persisted_cwd_regression(fake_tools_dir):
    # dv-8664 regression: the Bash tool's shell cwd persists *across separate
    # tool calls* (e.g. an earlier call did `cd ~/tools/fetchcrypto`), but
    # this call's `working_directory` is only the turn's nominal cwd, not
    # that real persisted one -- so a bare name with zero in-command `cd`
    # must still ask when it names a real file in an exchange dir.
    assert _bash("node perp.js")
    assert _bash("node move_stop.js X 1")
    assert _bash("node zzz_brand_new_script.js")  # unlisted anywhere, still real


def test_bare_read_only_report_is_not_gated(fake_tools_dir):
    assert _bash("node index.js") is None


def test_bare_name_matching_no_real_script_is_not_gated(fake_tools_dir):
    assert _bash("node totally_unrelated.js") is None


def test_explicit_path_outside_exchange_dirs_is_not_gated(fake_tools_dir):
    # An absolute path that names a file elsewhere is resolvable evidence of
    # "not an exchange script" even if that basename happens to exist on the
    # real exchange-tool disk too.
    assert _bash("node /some/other/proj/close.js") is None


def test_aster_report_is_not_gated():
    assert _bash("node /home/testuser/tools/fetchcrypto/index.js wallet1") is None
    assert _bash("node /home/testuser/tools/fetchcrypto/index.js") is None
    assert _bash("node /home/testuser/tools/fetchcrypto/orders.js wallet1") is None
    assert _bash("node /home/testuser/tools/fetchcrypto/query_trades.js") is None
    assert _bash("node /home/testuser/tools/fetchmexc/analytics.js") is None
    assert (
        _bash("cd /home/testuser/tools/fetchcrypto && node index.js wallet1") is None
    )


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
    assert _bash("node --check /home/testuser/tools/fetchcrypto/perp.js") is None
    assert (
        _bash("node --check /home/testuser/tools/fetchcrypto/buy.js && echo ok") is None
    )
    # But a real invocation later in the same command still gates.
    assert _bash(
        "node --check /home/testuser/tools/fetchcrypto/perp.js && echo ok\n"
        'echo "no" | node /home/testuser/tools/fetchcrypto/perp.js limit BTC_USDT LONG 5 10 55000'
    )


def test_reading_exchange_scripts_is_not_gated():
    # dv-8664 problem #2: these must never ask, even though the filename is
    # a money script -- they don't execute it.
    for command in (
        "grep foo /home/testuser/tools/fetchcrypto/modify_order.js",
        "head /home/testuser/tools/fetchcrypto/close.js",
        "cat /home/testuser/tools/fetchcrypto/perp.js",
        "sed -n '1,20p' /home/testuser/tools/fetchcrypto/buy.js",
        "less /home/testuser/tools/fetchcrypto/transfer.js",
        "wc -l /home/testuser/tools/fetchcrypto/move_stop.js",
        "cd /home/testuser/tools/fetchcrypto && cat buy.js",
    ):
        assert _bash(command) is None, command


def test_everyday_commands_pass():
    assert _bash("ls -la") is None
    assert _bash("git status") is None
    assert _bash("cat buy.js") is None  # reading the script, not running it
    assert _bash("node app.js") is None  # unrelated node script, no exchange dir


def test_known_destructive_still_gated():
    assert _bash("rm -rf build")
    assert _bash("git reset --hard HEAD~1")


# -- dv-1601: deletion and outbound transfer --------------------------------


def test_any_deletion_outside_temp_is_gated():
    for command in (
        "rm notes.txt",
        "rm -rf build",
        "sudo rm -f /etc/x",
        "cd /tmp && rm x",  # relative to the turn's cwd, not to the `cd`
        "unlink a",
        "rmdir old",
        "timeout 5 rm x",
        "xargs rm < list",
        "find . -name '*.pyc' -delete",
        "find /x -exec rm {} \\;",
        "rm -rf $TMPDIR/x",  # unresolvable -> ask
        "rm -rf /tmp",  # the temp dir itself
        "rm -rf /tmp/../etc",
    ):
        assert _bash(command), command


def test_deletion_inside_temp_is_free():
    assert _bash("rm -rf /tmp/foo") is None
    assert _bash("rm /tmp/a /tmp/b") is None
    assert _bash("rm /tmp/a ./keep") is not None


def test_deletion_inside_a_nested_shell_is_seen():
    assert _bash("bash -c 'rm -rf ~/x'")
    assert _bash(
        "sh -c 'cd /home/testuser/tools/fetchcrypto && node perp.js market X LONG 1 1'"
    )
    # rm handed to an interpreter the parser can't follow: the old regex net.
    assert _bash("python3 -c \"import os; os.system('rm -rf /')\"")


def test_words_that_only_look_like_deletion_pass():
    assert _bash("git rm --cached x") is None
    assert _bash("grep -r 'rm ' src") is None
    assert _bash("echo 'do not rm this'") is None


def test_sending_data_out_is_gated():
    for command in (
        "curl -X POST https://x.com -d a=1",
        "curl -sXPOST https://x.com",
        "curl -d @file https://x.com",
        "curl -F f=@a https://x.com",
        "curl -T a https://x.com",
        "curl --json '{}' https://x.com",
        "curl 'https://x.com/?a=1&b=2' -d foo",
        "wget --post-file=a https://x.com",
        "scp a host:/b",
        "ssh host uptime",
        "rsync -a a/ user@host:b/",
        "git push",
        "git -C repo push origin main",
        "cat x | nc host 9",
        "aws s3 cp a s3://b",
        "gh gist create a",
        "mail -s hi a@b.c < f",
    ):
        assert _bash(command), command


def test_downloads_and_local_requests_pass():
    for command in (
        "curl https://example.com",
        "curl -s https://api.x.com | jq .",
        "curl https://x.com | cut -d: -f1",
        "curl -X GET https://x.com",
        "curl -o out.tgz https://x.com/f",
        "curl -D - https://x.com",
        "curl -X POST http://127.0.0.1:6006/v1/x -d '{}'",
        "curl -d x localhost:8080/y",
        "wget https://x.com/f",
        "rsync -a a/ b/",
        "git pull",
        "git commit -m 'push it'",
        "git log --grep ssh",
        "ssh-keygen -l -f k",
        "echo scp",
        "aws s3 ls",
        "gh pr list",
    ):
        assert _bash(command) is None, command
