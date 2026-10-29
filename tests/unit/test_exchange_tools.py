"""exchange_* tools (dv-8664): specs, mount gating, argv building, the gate sets.

Nothing here touches a real exchange: the client runs a fake ``node`` that
echoes its argv, against a temp tools root.
"""

import stat
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

from src.claude.agent_tools import ALL_TOOL_SPECS, available_tool_specs
from src.claude.exchange_tools import (
    ACTIONS,
    EXCHANGE_SPECS,
    ONCHAIN_SPECS,
    READ_ACTIONS,
    SCRIPTS_DIR,
    VENUES,
    BadArgs,
    ExchangeClient,
    Unsupported,
)
from src.claude.tool_context import AgentServices, TurnContext
from src.profiles.registry import (
    ProfileDefinition,
    ProfileRegistry,
    load_profile_registry,
)
from src.security.destructive import (
    EXCHANGE_DRY_RUN_TOOLS,
    EXCHANGE_READ_ONLY_TOOLS,
    EXCHANGE_TOOL_DIRS,
    exchange_tool_ask,
)
from src.security.keystore import KeyStore

NAMES = {s.name for s in EXCHANGE_SPECS}
SIGNER_KEY = "0x" + "ab" * 32
MEXC_SECRET = "mexc-secret-value-123"


# -- the gate's sets cover every spec, one way or the other --------------------


def test_specs_registered_on_both_transports() -> None:
    assert NAMES <= {s.name for s in ALL_TOOL_SPECS}
    assert all(name.startswith("exchange_") for name in NAMES)


def test_every_spec_is_either_read_only_or_a_dry_run_write() -> None:
    assert EXCHANGE_READ_ONLY_TOOLS | EXCHANGE_DRY_RUN_TOOLS == NAMES
    assert not EXCHANGE_READ_ONLY_TOOLS & EXCHANGE_DRY_RUN_TOOLS


def test_write_specs_offer_dry_run_and_read_specs_do_not() -> None:
    for spec in EXCHANGE_SPECS:
        has = "dry_run" in spec.schema["properties"]
        assert has == (spec.name in EXCHANGE_DRY_RUN_TOOLS), spec.name


def test_read_actions_match_the_gate_read_only_list() -> None:
    gate = {name[len("exchange_") :] for name in EXCHANGE_READ_ONLY_TOOLS}
    assert set(READ_ACTIONS) == gate - {"accounts"}


def test_venue_dirs_are_the_ones_the_bash_gate_watches() -> None:
    # node venues can move money, so the Bash gate must watch their dirs;
    # python venues are read-only (writes are Unsupported, see below).
    node = {v.directory for v in VENUES.values() if v.runner == "node"}
    assert node <= set(EXCHANGE_TOOL_DIRS)


# -- the gate ------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool,args,asks",
    [
        ("mcp__agent__exchange_report", {}, False),
        ("mcp__agent__exchange_accounts", {}, False),
        ("mcp__agent__exchange_place_order", {"exchange": "aster"}, True),
        ("mcp__agent__exchange_place_order", {"dry_run": True}, False),
        ("mcp__agent__exchange_place_order", {"dry_run": "true"}, True),
        ("mcp__agent__exchange_place_order", {"arguments": {"dry_run": True}}, False),
        ("mcp__agent__exchange_withdraw", {"dry_run": True}, True),
        ("mcp__other__exchange_report", {}, True),
        ("exchange_cancel_order", {}, True),
        ("mcp__agent__recall", {}, False),
    ],
)
def test_gate(tool: str, args: Dict[str, Any], asks: bool) -> None:
    assert (exchange_tool_ask(tool, args) is not None) == asks


def test_gate_text_names_action_and_key_params() -> None:
    ask = exchange_tool_ask(
        "mcp__agent__exchange_place_order",
        {
            "exchange": "aster",
            "account": "wallet1",
            "market": "perp",
            "symbol": "BTCUSDT",
            "side": "LONG",
            "type": "limit",
            "margin": 10,
            "leverage": 5,
            "price": 60000,
            "dry_run": False,
        },
    )
    assert ask is not None
    reason, preview = ask
    assert reason == "exchange: place order (aster wallet1 perp BTCUSDT LONG)"
    for line in ("margin: 10", "leverage: 5", "price: 60000", "account: wallet1"):
        assert line in preview


# -- mount gating --------------------------------------------------------------


def _profile(slug: str, exchange: bool) -> ProfileDefinition:
    return ProfileDefinition(
        slug=slug,
        name=slug,
        working_dir=Path(f"/tmp/{slug}"),
        memory_dir=Path(f"/tmp/{slug}/memory"),
        persona=Path(f"/tmp/{slug}/persona.md"),
        default=slug == "on",
        exchange=exchange,
    )


def _services(client: Any) -> AgentServices:
    services = AgentServices(
        event_bus=None,
        task_repo=None,
        profile_registry=ProfileRegistry(
            [_profile("on", True), _profile("off", False)]
        ),
    )
    services.exchange = client
    return services


@pytest.mark.parametrize(
    "has_client,slug,role,expected",
    [
        (True, "on", "owner", True),
        (True, "on#u5", "owner", True),
        (True, "on", "guest", False),
        (True, "on", None, False),
        (True, "off", "owner", False),
        (False, "on", "owner", False),
    ],
)
def test_mounting(has_client: bool, slug: str, role: Any, expected: bool) -> None:
    client = ExchangeClient(Path("/nonexistent")) if has_client else None
    ctx = TurnContext(profile_slug=slug, user_id=5, role=role)
    names = {s.name for s in available_tool_specs(_services(client), ctx)}
    assert (NAMES <= names) == expected
    assert bool(NAMES & names) == expected


def test_loader_refuses_exchange_on_a_guest_profile(tmp_path: Path) -> None:
    cfg = tmp_path / "profiles.yaml"
    cfg.write_text(
        """
profiles:
  - slug: general
    name: General
    working_dir: /tmp/gen
    memory_dir: /tmp/gen/m
    persona: /tmp/gen/p.md
    default: true
    exchange: true
    permission_policy:
      allowed_roles: [owner, guest]
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exchange is not allowed"):
        load_profile_registry(cfg, sandbox_default=True, data_root=None)


# -- argv building -------------------------------------------------------------


def _build(action: str, venue: str, account: str, **args: Any) -> tuple:
    inv = ACTIONS[action][venue](account, args)
    return (inv.script, *inv.argv)


@pytest.mark.parametrize(
    "action,venue,account,args,expected",
    [
        ("report", "aster", "wallet2", {}, ("index.js", "wallet2", "--quick")),
        ("report", "aster", "wallet1", {"full": True}, ("index.js", "wallet1")),
        ("report", "mexc", "wallet1", {}, ("index.js", "wallet1")),
        (
            "open_orders",
            "mexc",
            "wallet1",
            {"symbol": "btcusdt"},
            ("orders.js", "BTC_USDT", "--account", "wallet1"),
        ),
        (
            "trades",
            "aster",
            "wallet1",
            {"symbol": "XAUUSDT", "limit": 5},
            ("query_trades.js", "wallet1", "XAUUSDT", "5"),
        ),
        (
            "place_order",
            "aster",
            "wallet1",
            {
                "market": "perp",
                "symbol": "XAUUSDT",
                "side": "long",
                "type": "limit",
                "margin": 10,
                "leverage": 14,
                "price": "4494.5",
                "stop_price": 4400,
                "isolated": True,
            },
            (
                "perp.js",
                "limit",
                "XAUUSDT",
                "LONG",
                "10",
                "14",
                "4494.5",
                "--isolated",
                "--stop",
                "4400",
            ),
        ),
        (
            "place_order",
            "aster",
            "wallet1",
            {
                "market": "spot",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "market",
                "quote_amount": 200,
            },
            ("buy.js", "market", "BTCUSDT", "200"),
        ),
        (
            "place_order",
            "aster",
            "wallet1",
            {
                "market": "spot",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "type": "limit",
                "quantity": 0.001,
                "price": 68000,
            },
            ("sell.js", "limit", "BTCUSDT", "0.001", "68000"),
        ),
        (
            "place_order",
            "mexc",
            "wallet1",
            {
                "market": "spot",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "type": "limit",
                "quote_amount": 10,
                "price": 55000,
            },
            ("buy.js", "notional", "BTCUSDT", "10", "55000", "wallet1"),
        ),
        (
            "place_order",
            "mexc",
            "wallet1",
            {
                "market": "perp",
                "symbol": "BTCUSDT",
                "side": "SHORT",
                "type": "market",
                "margin": 10,
                "leverage": 10,
                "stop_price": 70000,
                "take_profit": 50000,
            },
            (
                "perp.js",
                "market",
                "BTC_USDT",
                "SHORT",
                "10",
                "10",
                "--sl",
                "70000",
                "--tp",
                "50000",
                "--account",
                "wallet1",
            ),
        ),
        (
            "close_position",
            "aster",
            "wallet2",
            {"symbol": "XAUUSDT", "type": "limit", "price": 4420, "quantity": 0.1},
            ("close.js", "wallet2", "XAUUSDT", "limit", "4420", "0.1"),
        ),
        (
            "move_stop",
            "aster",
            "wallet1",
            {"symbol": "BTCUSDT", "stop_price": 60000},
            ("move_stop.js", "BTCUSDT", "60000"),
        ),
        (
            "modify_order",
            "aster",
            "wallet2",
            {"symbol": "BTCUSDT", "price": 69000, "order_id": "123"},
            ("modify_order.js", "wallet2", "BTCUSDT", "69000", "123"),
        ),
        (
            "cancel_order",
            "aster",
            "wallet1",
            {"market": "spot", "symbol": "ASTERUSDT", "price": 1.5},
            ("cancel_spot.js", "ASTERUSDT", "1.5"),
        ),
        (
            "cancel_order",
            "mexc",
            "wallet1",
            {"market": "perp", "order_id": "108886241042563584"},
            ("orders.js", "--cancel", "108886241042563584", "--account", "wallet1"),
        ),
        (
            "transfer",
            "aster",
            "wallet1",
            {"from": "futures", "to": "spot", "asset": "usdt", "amount": 25},
            ("transfer.js", "fut2spot", "USDT", "25"),
        ),
        (
            "transfer",
            "mexc",
            "wallet1",
            {"from": "spot", "to": "futures", "asset": "USDC", "amount": "1e1"},
            ("transfer.js", "SPOT", "FUTURES", "USDC", "10", "--account", "wallet1"),
        ),
    ],
)
def test_argv(
    action: str, venue: str, account: str, args: Dict, expected: tuple
) -> None:
    assert _build(action, venue, account, **args) == expected


@pytest.mark.parametrize(
    "action,venue,account,args",
    [
        ("trades", "mexc", "wallet1", {"symbol": "BTCUSDT"}),
        ("analytics", "aster", "", {}),
        ("close_position", "mexc", "wallet1", {"symbol": "BTCUSDT", "type": "market"}),
        ("move_stop", "mexc", "wallet1", {"symbol": "BTCUSDT", "stop_price": 1}),
        ("move_stop", "aster", "wallet2", {"symbol": "BTCUSDT", "stop_price": 1}),
        ("modify_order", "mexc", "wallet1", {"symbol": "BTCUSDT", "price": 1}),
        ("cancel_order", "aster", "wallet1", {"market": "perp", "symbol": "BTCUSDT"}),
        ("cancel_order", "mexc", "wallet1", {"market": "spot", "order_id": "1"}),
        (
            "transfer",
            "aster",
            "wallet2",
            {"from": "spot", "to": "futures", "asset": "USDT", "amount": 1},
        ),
        (
            "place_order",
            "aster",
            "wallet2",
            {
                "market": "perp",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "type": "market",
                "margin": 1,
                "leverage": 2,
            },
        ),
    ],
)
def test_gaps_are_reported_not_improvised(action, venue, account, args) -> None:
    with pytest.raises(Unsupported):
        ACTIONS[action][venue](account, args)


@pytest.mark.parametrize(
    "args",
    [
        {"symbol": "--yes", "side": "LONG"},
        {"symbol": "BTC;rm", "side": "LONG"},
        {"symbol": "BTCUSDT", "side": "LONG", "margin": -5},
        {"symbol": "BTCUSDT", "side": "LONG", "margin": "nan"},
        {"symbol": "BTCUSDT", "side": "BUY"},  # spot side on perp
        {"symbol": "BTCUSDT", "side": "LONG", "quantity": 1},  # spot size on perp
        {"symbol": "BTCUSDT", "side": "LONG", "leverage": 0},
    ],
)
def test_bad_args_rejected(args: Dict[str, Any]) -> None:
    base = {"market": "perp", "type": "market", "margin": 5, "leverage": 2}
    with pytest.raises(BadArgs):
        ACTIONS["place_order"]["aster"]("wallet1", {**base, **args})


# -- the handler, end to end against a fake node -------------------------------


@pytest.fixture
def tools_root(tmp_path: Path) -> Path:
    aster = tmp_path / "fetchcrypto"
    mexc = tmp_path / "fetchmexc"
    for venue in (aster, mexc):
        venue.mkdir()
        for name in ("index.js", "perp.js", "orders.js", "transfer.js"):
            (venue / name).write_text("// fake\n")
    (aster / ".env").write_text(
        "# comment\n"
        "WALLET1_USER=0xuser1\nWALLET1_SIGNER=0xsigner1\n"
        f"WALLET1_SIGNER_KEY={SIGNER_KEY}\n"
        "WALLET2_USER=0xuser2\nWALLET2_SIGNER=0xsigner2\n"
        f"WALLET2_SIGNER_KEY={SIGNER_KEY}\n"
    )
    (mexc / ".env").write_text(
        f"MEXC1_LABEL=wallet1\nMEXC1_API_KEY=mexc-key-abcdef\nMEXC1_API_SECRET={MEXC_SECRET}\n"
    )
    return tmp_path


@pytest.fixture
def fake_node(tmp_path: Path) -> str:
    """Prints its argv, plus a leaked secret to prove redaction."""
    path = tmp_path / "node"
    path.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "print('ARGV', ' '.join(sys.argv[1:]))\n"
        f"print('leak {SIGNER_KEY} {MEXC_SECRET}')\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _spec(name: str) -> Any:
    return next(s for s in EXCHANGE_SPECS if s.name == name)


OWNER = TurnContext(profile_slug="on", user_id=5, role="owner")


async def test_accounts_lists_names_only(tools_root: Path) -> None:
    services = _services(ExchangeClient(tools_root))
    result = await _spec("exchange_accounts").handler(services, OWNER, {})
    text = result["content"][0]["text"]
    assert text == "aster: wallet1, wallet2\nmexc: wallet1\nbybit: (none configured)"
    assert "0x" not in text and "mexc-key" not in text


@pytest.mark.parametrize("dry_run,flag", [(True, "--dry-run"), (False, "--yes")])
async def test_write_passes_the_confirm_flag(
    tools_root, fake_node, dry_run, flag
) -> None:
    services = _services(ExchangeClient(tools_root, node=fake_node))
    args = {
        "exchange": "mexc",
        "account": "wallet1",
        "from": "spot",
        "to": "futures",
        "asset": "USDT",
        "amount": 3,
        "dry_run": dry_run,
    }
    result = await _spec("exchange_transfer").handler(services, OWNER, args)
    text = result["content"][0]["text"]
    assert not result.get("isError")
    assert f"transfer.js SPOT FUTURES USDT 3 --account wallet1 {flag}" in text
    assert SIGNER_KEY not in text and MEXC_SECRET not in text
    assert text.count("<redacted>") == 2


async def test_read_passes_no_confirm_flag(tools_root, fake_node) -> None:
    services = _services(ExchangeClient(tools_root, node=fake_node))
    result = await _spec("exchange_report").handler(services, OWNER, {})
    text = result["content"][0]["text"]
    for line in (
        "index.js wallet1 --quick",
        "index.js wallet2 --quick",
        "index.js wallet1",
    ):
        assert f"ARGV {Path(tools_root)}" in text and line in text
    assert "--yes" not in text and "--dry-run" not in text


async def test_script_without_confirm_step_is_not_run_on_dry_run(
    tools_root, fake_node
) -> None:
    services = _services(ExchangeClient(tools_root, node=fake_node))
    args = {
        "exchange": "mexc",
        "account": "wallet1",
        "market": "perp",
        "order_id": "42",
        "dry_run": True,
    }
    result = await _spec("exchange_cancel_order").handler(services, OWNER, args)
    text = result["content"][0]["text"]
    assert "DRY RUN" in text and "ARGV" not in text


async def test_unknown_account_is_an_error(tools_root, fake_node) -> None:
    services = _services(ExchangeClient(tools_root, node=fake_node))
    args = {"exchange": "aster", "account": "wallet9", "market": "perp"}
    result = await _spec("exchange_cancel_order").handler(services, OWNER, args)
    assert result["isError"] and "no configured account" in result["content"][0]["text"]


async def test_handler_rechecks_role(tools_root, fake_node) -> None:
    services = _services(ExchangeClient(tools_root, node=fake_node))
    guest = TurnContext(profile_slug="on", user_id=5, role="guest")
    result = await _spec("exchange_report").handler(services, guest, {})
    assert result["isError"]


# -- bybit (python venue, read-only) and onchain_activity ----------------------

BYBIT_KEY = "bybitkey-123456789"
BYBIT_SECRET = "bybitsecret-987654321"
ETHERSCAN_KEY = "etherscankey-abcdef123"
ADDR = "0x0cDF53551f75f2508303B94245B9B708f03a1d83"


@pytest.fixture
def keystore(tmp_path: Path) -> KeyStore:
    store = KeyStore(tmp_path / "keys")
    store.set("bybit", "BYBIT_API_KEY", BYBIT_KEY)
    store.set("bybit", "BYBIT_API_SECRET", BYBIT_SECRET)
    store.set("etherscan", "ETHERSCAN_API_KEY", ETHERSCAN_KEY)
    return store


@pytest.fixture
def fake_python(tmp_path: Path) -> str:
    """Prints runner argv + cwd, and leaks every key to prove redaction."""
    path = tmp_path / "python3"
    path.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "print('ARGV', ' '.join(sys.argv[1:]), 'CWD', os.getcwd())\n"
        "print('have-key', 'BYBIT_API_KEY' in os.environ)\n"
        f"print('leak {BYBIT_KEY} {BYBIT_SECRET} {ETHERSCAN_KEY}')\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


# scripts/venues/ is the owner's and is not shipped (export_public.py).
NEEDS_VENUE_SCRIPTS = pytest.mark.skipif(
    not (SCRIPTS_DIR / "bybit_snapshot.py").exists(),
    reason="scripts/venues is not shipped",
)


@pytest.fixture
def bybit_root(tools_root: Path) -> Path:
    (tools_root / "bybit").mkdir()
    (tools_root / "bybit" / "history.py").write_text("# fake\n")
    (tools_root / "bybit" / "funding.py").write_text("# fake\n")
    return tools_root


def _bybit_client(root, fake_python, keystore) -> ExchangeClient:
    return ExchangeClient(root, python=fake_python, keystore=keystore)


async def test_bybit_account_comes_from_the_key_store(bybit_root, keystore) -> None:
    services = _services(ExchangeClient(bybit_root, keystore=keystore))
    result = await _spec("exchange_accounts").handler(services, OWNER, {})
    assert result["content"][0]["text"].endswith("bybit: main")


@NEEDS_VENUE_SCRIPTS
async def test_bybit_report_runs_python_with_key_and_redacts(
    bybit_root, fake_python, keystore
) -> None:
    services = _services(_bybit_client(bybit_root, fake_python, keystore))
    args = {"exchange": "bybit"}
    result = await _spec("exchange_report").handler(services, OWNER, args)
    text = result["content"][0]["text"]
    assert not result.get("isError"), text
    assert "[bybit main] python bybit_snapshot.py report" in text
    assert f"bybit_snapshot.py report CWD {bybit_root / 'bybit'}" in text
    assert "have-key True" in text
    assert "--yes" not in text and "--dry-run" not in text
    for secret in (BYBIT_KEY, BYBIT_SECRET):
        assert secret not in text
    assert text.count("<redacted>") >= 2


async def test_bybit_analytics_history_and_funding(
    bybit_root, fake_python, keystore
) -> None:
    services = _services(_bybit_client(bybit_root, fake_python, keystore))
    spec = _spec("exchange_analytics")
    out = await spec.handler(services, OWNER, {"exchange": "bybit", "days": 90})
    assert "history.py 90" in out["content"][0]["text"]
    out = await spec.handler(services, OWNER, {"exchange": "bybit", "funding": True})
    assert "funding.py" in out["content"][0]["text"]


async def test_bybit_trades_pass_symbol_and_limit(
    bybit_root, fake_python, keystore
) -> None:
    services = _services(_bybit_client(bybit_root, fake_python, keystore))
    args = {"exchange": "bybit", "symbol": "BTCUSDT", "limit": 5}
    out = await _spec("exchange_trades").handler(services, OWNER, args)
    assert "bybit_snapshot.py trades BTCUSDT 5" in out["content"][0]["text"]


@pytest.mark.parametrize("name", sorted(EXCHANGE_DRY_RUN_TOOLS))
async def test_bybit_writes_are_unsupported_even_with_dry_run(
    bybit_root, fake_python, keystore, name
) -> None:
    services = _services(_bybit_client(bybit_root, fake_python, keystore))
    args = {"exchange": "bybit", "account": "main", "dry_run": True}
    result = await _spec(name).handler(services, OWNER, args)
    text = result["content"][0]["text"]
    assert result["isError"] and "not supported" in text and "ARGV" not in text


def test_bybit_unsupported_reads_are_reported() -> None:
    with pytest.raises(BadArgs):
        ACTIONS["analytics"]["bybit"]("", {"symbol": "BTCUSDT"})
    with pytest.raises(BadArgs):
        ACTIONS["trades"]["bybit"]("main", {})


def test_onchain_spec_is_not_an_exchange_tool() -> None:
    assert [s.name for s in ONCHAIN_SPECS] == ["onchain_activity"]
    assert "onchain_activity" in {s.name for s in ALL_TOOL_SPECS}
    assert "onchain_activity" not in NAMES


@NEEDS_VENUE_SCRIPTS
async def test_onchain_activity_runs_in_workspace_out_and_redacts(
    tools_root, fake_python, keystore, tmp_path
) -> None:
    services = _services(_bybit_client(tools_root, fake_python, keystore))
    ctx = TurnContext(
        profile_slug="on", user_id=5, role="owner", working_dir=tmp_path / "ws"
    )
    args = {"address": ADDR, "chain": "arbitrum", "days": 7, "limit": 10}
    result = await ONCHAIN_SPECS[0].handler(services, ctx, args)
    text = result["content"][0]["text"]
    assert not result.get("isError"), text
    assert (
        f"onchain_activity.py {ADDR} arbitrum 7 10 CWD {tmp_path / 'ws' / 'out'}"
        in text
    )
    assert ETHERSCAN_KEY not in text and "<redacted>" in text


@pytest.mark.parametrize(
    "args",
    [
        {"address": "0x123"},
        {"address": ADDR, "chain": "solana"},
        {"address": ADDR, "days": 0},
    ],
)
async def test_onchain_activity_bad_args(
    tools_root, fake_python, keystore, args
) -> None:
    services = _services(_bybit_client(tools_root, fake_python, keystore))
    result = await ONCHAIN_SPECS[0].handler(services, OWNER, args)
    assert result["isError"] and "bad arguments" in result["content"][0]["text"]


async def test_onchain_activity_needs_owner_and_key(tools_root, fake_python) -> None:
    services = _services(ExchangeClient(tools_root, python=fake_python))
    result = await ONCHAIN_SPECS[0].handler(services, OWNER, {"address": ADDR})
    assert result["isError"] and "no etherscan key" in result["content"][0]["text"]
    guest = TurnContext(profile_slug="on", user_id=5, role="guest")
    result = await ONCHAIN_SPECS[0].handler(services, guest, {"address": ADDR})
    assert result["isError"]
