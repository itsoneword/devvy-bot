"""Exchange tools: the agent's one way to talk to the trading venues (dv-8664).

Every ``exchange_*`` tool is a :class:`ToolSpec` (same specs on the in-process
SDK server and the Codex bridge, dv-ffcb) whose handler runs an external
exchange script from the bot process, one script directory per venue under
the tools root (see ``VENUES``).

The scripts stay the single implementation of signing, sizing, previews and
sanity checks and can still be used by hand. Their typed "yes" prompt
is replaced, for the bot only, by ``--yes`` (the owner already approved the
call through the policy gate) or ``--dry-run`` (print the preview, send
nothing). Both flags live in each repo's ``lib/confirm.js``.

Gating is NOT here: ``src/security/destructive.py::exchange_tool_ask`` asks
the owner for every exchange tool unless it is listed there as read-only, on
both backends, through ``policy.decide``. This module only builds argv.

Keys never leave the scripts' ``.env`` files. The bot reads those files only
for account *names* and to scrub any secret value out of script output.

Each venue's action table maps a tool to the script that implements it;
an action a venue's scripts do not support returns an error naming the gap
rather than improvising one.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import structlog

from .tool_context import AgentServices, ToolSpec, TurnContext

logger = structlog.get_logger()

READ_TIMEOUT_SECONDS = 240.0  # aster index.js full report walks income history
WRITE_TIMEOUT_SECONDS = 90.0
MAX_OUTPUT_CHARS = 24_000


class Unsupported(Exception):
    """The venue's scripts have no implementation for this request."""


class BadArgs(Exception):
    """The model's arguments are invalid; the message says why."""


@dataclass(frozen=True)
class Venue:
    name: str
    directory: str  # under the tools root; must be in EXCHANGE_TOOL_DIRS


VENUES: Dict[str, Venue] = {
    "aster": Venue("aster", "fetchcrypto"),
    "mexc": Venue("mexc", "fetchmexc"),
}


@dataclass(frozen=True)
class Invocation:
    """One script run: ``node <script> <argv...>`` inside the venue dir."""

    script: str
    argv: Tuple[str, ...]
    # Script goes through lib/confirm.js, so --yes / --dry-run apply.
    confirms: bool = True


def _parse_env(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out


_SECRET_KEY = re.compile(r"(KEY|SECRET)$")
_PRIVATE_KEY_HEX = re.compile(r"0x[0-9a-fA-F]{64}")


class ExchangeClient:
    """Runs the venue scripts. Holds no keys; reads ``.env`` names only."""

    def __init__(self, root: Path, node: Optional[str] = None) -> None:
        self.root = Path(root)
        self.node = node or shutil.which("node") or "node"

    def venue_dir(self, venue: str) -> Path:
        return self.root / VENUES[venue].directory

    def _env(self, venue: str) -> Dict[str, str]:
        return _parse_env(self.venue_dir(venue) / ".env")

    def accounts(self) -> Dict[str, List[str]]:
        """``{venue: [account names]}`` for every configured account."""
        out: Dict[str, List[str]] = {}
        env = self._env("aster")
        # The Aster scripts know wallet1 and wallet2 by name (WALLETS map).
        out["aster"] = [
            f"wallet{n}"
            for n in (1, 2)
            if env.get(f"WALLET{n}_USER") and env.get(f"WALLET{n}_SIGNER_KEY")
        ]
        env = self._env("mexc")
        names: List[str] = []
        n = 1
        # Same walk as fetchmexc lib/client.js loadAccounts().
        while env.get(f"MEXC{n}_API_KEY"):
            names.append(env.get(f"MEXC{n}_LABEL") or f"mexc{n}")
            n += 1
        out["mexc"] = names
        return out

    def _redact(self, venue: str, text: str) -> str:
        for key, value in self._env(venue).items():
            if _SECRET_KEY.search(key) and len(value) >= 8:
                text = text.replace(value, "<redacted>")
        return _PRIVATE_KEY_HEX.sub("<redacted>", text)

    async def run(self, venue: str, inv: Invocation, timeout: float) -> Tuple[int, str]:
        """Run one script; ``(exit code, redacted output)``. -1 = timed out."""
        cwd = self.venue_dir(venue)
        script = cwd / inv.script
        if not script.is_file():
            return 127, f"{inv.script} not found in {cwd}"
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(Path.home())),
            "LANG": "C.UTF-8",
            "DOTENV_CONFIG_QUIET": "true",  # drop dotenv's ad line
        }
        proc = await asyncio.create_subprocess_exec(
            self.node,
            str(script),
            *inv.argv,
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            code = proc.returncode if proc.returncode is not None else -1
        except asyncio.TimeoutError:
            proc.kill()
            raw, _ = await proc.communicate()
            code = -1
        text = self._redact(venue, raw.decode("utf-8", "replace"))
        if len(text) > MAX_OUTPUT_CHARS:
            half = MAX_OUTPUT_CHARS // 2
            text = text[:half] + "\n…[output truncated]…\n" + text[-half:]
        return code, text


# -- argument helpers ---------------------------------------------------------

_SYMBOL = re.compile(r"^[A-Z0-9]{2,20}(_[A-Z0-9]{2,10})?$")
_ASSET = re.compile(r"^[A-Z0-9]{2,12}$")
_ORDER_ID = re.compile(r"^[0-9]{1,30}$")


def _opt(args: Dict[str, Any], key: str) -> Any:
    value = args.get(key)
    return None if value in (None, "") else value


def _symbol(args: Dict[str, Any], required: bool = True) -> Optional[str]:
    value = _opt(args, "symbol")
    if value is None:
        if required:
            raise BadArgs("symbol is required")
        return None
    symbol = str(value).strip().upper()
    if not _SYMBOL.match(symbol):
        raise BadArgs(f"bad symbol {value!r}")
    return symbol


def _mexc_contract(symbol: str) -> str:
    """MEXC futures use BTC_USDT; accept BTCUSDT too."""
    if "_" not in symbol:
        for quote in ("USDT", "USDC", "USD"):
            if symbol.endswith(quote) and len(symbol) > len(quote):
                return f"{symbol[: -len(quote)]}_{quote}"
    return symbol


def _num(args: Dict[str, Any], key: str, required: bool = True) -> Optional[str]:
    """A positive decimal as a plain string (never exponent notation)."""
    value = _opt(args, key)
    if value is None:
        if required:
            raise BadArgs(f"{key} is required")
        return None
    if isinstance(value, bool):
        raise BadArgs(f"{key} must be a number")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise BadArgs(f"{key} must be a number, got {value!r}")
    if not number.is_finite() or number <= 0:
        raise BadArgs(f"{key} must be a positive number")
    return format(number.normalize(), "f")


def _int(args: Dict[str, Any], key: str, low: int, high: int) -> Optional[int]:
    value = _opt(args, key)
    if value is None:
        return None
    try:
        number = int(str(value))
    except ValueError:
        raise BadArgs(f"{key} must be a whole number")
    if not low <= number <= high:
        raise BadArgs(f"{key} must be between {low} and {high}")
    return number


def _choice(args: Dict[str, Any], key: str, options: Tuple[str, ...]) -> str:
    value = str(_opt(args, key) or "").strip().lower()
    if value not in options:
        raise BadArgs(f"{key} must be one of {', '.join(options)}")
    return value


def _side(args: Dict[str, Any], options: Tuple[str, ...]) -> str:
    value = str(_opt(args, "side") or "").strip().upper()
    if value not in options:
        raise BadArgs(f"side must be one of {', '.join(options)} here")
    return value


def _reject(args: Dict[str, Any], keys: Tuple[str, ...], why: str) -> None:
    given = [key for key in keys if _opt(args, key) is not None]
    if given:
        raise BadArgs(f"{', '.join(given)} not used {why}")


def _only_wallet1(account: str, script: str) -> None:
    if account != "wallet1":
        raise Unsupported(f"aster {script} trades wallet1 only (account hardcoded)")


# -- per-venue action tables --------------------------------------------------
# builder(account, args) -> Invocation. ``account`` is already validated.

Builder = Callable[[str, Dict[str, Any]], Invocation]


def _aster_report(account: str, args: Dict[str, Any]) -> Invocation:
    flags = () if args.get("full") is True else ("--quick",)
    return Invocation("index.js", (account, *flags))


def _mexc_report(account: str, args: Dict[str, Any]) -> Invocation:
    return Invocation("index.js", (account,))


def _aster_open_orders(account: str, args: Dict[str, Any]) -> Invocation:
    # orders.js: open + last-7-day perp and spot orders; no symbol filter.
    return Invocation("orders.js", (account,))


def _mexc_open_orders(account: str, args: Dict[str, Any]) -> Invocation:
    symbol = _symbol(args, required=False)
    sym = (_mexc_contract(symbol),) if symbol else ()
    return Invocation("orders.js", (*sym, "--account", account))


def _aster_trades(account: str, args: Dict[str, Any]) -> Invocation:
    symbol = _symbol(args)
    assert symbol
    limit = _int(args, "limit", 1, 1000)
    return Invocation(
        "query_trades.js", (account, symbol, *((str(limit),) if limit else ()))
    )


def _mexc_analytics(account: str, args: Dict[str, Any]) -> Invocation:
    symbol = _symbol(args, required=False)
    argv: List[str] = [symbol] if symbol else []
    days = _int(args, "days", 2, 500)
    window = _int(args, "window", 2, 365)
    if days and days <= (window or 30):
        raise BadArgs(f"days must exceed window ({window or 30}, the script's default)")
    if days:
        argv += ["--days", str(days)]
    if window:
        argv += ["--window", str(window)]
    return Invocation("analytics.js", tuple(argv))


_PERP_ONLY = ("margin", "leverage", "stop_price", "take_profit", "isolated")
_SPOT_ONLY = ("quantity", "quote_amount")


def _aster_place(account: str, args: Dict[str, Any]) -> Invocation:
    market = _choice(args, "market", ("spot", "perp"))
    kind = _choice(args, "type", ("market", "limit"))
    symbol = _symbol(args)
    assert symbol
    if market == "spot":
        _reject(args, _PERP_ONLY, "for spot")
        side = _side(args, ("BUY", "SELL"))
        script = "buy.js" if side == "BUY" else "sell.js"
        _only_wallet1(account, script)
        if kind == "market":
            if side == "BUY":
                _reject(
                    args, ("quantity",), "for an aster market buy (spends quote_amount)"
                )
                return Invocation(
                    script, ("market", symbol, _num(args, "quote_amount") or "")
                )
            _reject(
                args, ("quote_amount",), "for an aster market sell (sells quantity)"
            )
            return Invocation(script, ("market", symbol, _num(args, "quantity") or ""))
        _reject(
            args, ("quote_amount",), "for an aster limit order (no notional sizing)"
        )
        return Invocation(
            script,
            ("limit", symbol, _num(args, "quantity") or "", _num(args, "price") or ""),
        )
    _reject(args, _SPOT_ONLY, "for perp (size is margin x leverage)")
    _reject(args, ("take_profit",), "on aster perp.js (no take-profit support)")
    side = _side(args, ("LONG", "SHORT"))
    _only_wallet1(account, "perp.js")
    leverage = _int(args, "leverage", 1, 200)
    if leverage is None:
        raise BadArgs("leverage is required")
    argv = [kind, symbol, side, _num(args, "margin") or "", str(leverage)]
    if kind == "limit":
        argv.append(_num(args, "price") or "")
    if args.get("isolated") is True:
        argv.append("--isolated")
    stop = _num(args, "stop_price", required=False)
    if stop:
        argv += ["--stop", stop]
    return Invocation("perp.js", tuple(argv))


def _mexc_place(account: str, args: Dict[str, Any]) -> Invocation:
    market = _choice(args, "market", ("spot", "perp"))
    kind = _choice(args, "type", ("market", "limit"))
    symbol = _symbol(args)
    assert symbol
    if market == "spot":
        _reject(args, _PERP_ONLY, "for spot")
        side = _side(args, ("BUY", "SELL"))
        script = "buy.js" if side == "BUY" else "sell.js"
        quote = _num(args, "quote_amount", required=False)
        if kind == "market":
            if side == "BUY":
                _reject(
                    args, ("quantity",), "for a mexc market buy (spends quote_amount)"
                )
                return Invocation(
                    script,
                    ("market", symbol, _num(args, "quote_amount") or "", account),
                )
            _reject(args, ("quote_amount",), "for a mexc market sell (sells quantity)")
            return Invocation(
                script, ("market", symbol, _num(args, "quantity") or "", account)
            )
        price = _num(args, "price") or ""
        if quote:
            _reject(args, ("quantity",), "together with quote_amount")
            return Invocation(script, ("notional", symbol, quote, price, account))
        return Invocation(
            script, ("limit", symbol, _num(args, "quantity") or "", price, account)
        )
    _reject(args, _SPOT_ONLY, "for perp (size is margin x leverage)")
    side = _side(args, ("LONG", "SHORT"))
    leverage = _int(args, "leverage", 1, 500)
    if leverage is None:
        raise BadArgs("leverage is required")
    argv = [
        kind,
        _mexc_contract(symbol),
        side,
        _num(args, "margin") or "",
        str(leverage),
    ]
    if kind == "limit":
        argv.append(_num(args, "price") or "")
    stop = _num(args, "stop_price", required=False)
    take = _num(args, "take_profit", required=False)
    if stop:
        argv += ["--sl", stop]
    if take:
        argv += ["--tp", take]
    # MEXC perp.js always opens isolated (openType 1); `isolated` is moot.
    return Invocation("perp.js", (*argv, "--account", account))


def _aster_close(account: str, args: Dict[str, Any]) -> Invocation:
    symbol = _symbol(args)
    assert symbol
    kind = _choice(args, "type", ("market", "limit"))
    qty = _num(args, "quantity", required=False)
    if kind == "market":
        return Invocation(
            "close.js", (account, symbol, "market", *((qty,) if qty else ()))
        )
    price = _num(args, "price") or ""
    return Invocation(
        "close.js", (account, symbol, "limit", price, *((qty,) if qty else ()))
    )


def _aster_move_stop(account: str, args: Dict[str, Any]) -> Invocation:
    _only_wallet1(account, "move_stop.js")
    symbol = _symbol(args)
    assert symbol
    return Invocation("move_stop.js", (symbol, _num(args, "stop_price") or ""))


def _aster_modify(account: str, args: Dict[str, Any]) -> Invocation:
    symbol = _symbol(args)
    assert symbol
    order_id = _opt(args, "order_id")
    argv = [account, symbol, _num(args, "price") or ""]
    if order_id is not None:
        if not _ORDER_ID.match(str(order_id)):
            raise BadArgs("order_id must be digits")
        argv.append(str(order_id))
    return Invocation("modify_order.js", tuple(argv))


def _aster_cancel(account: str, args: Dict[str, Any]) -> Invocation:
    market = _choice(args, "market", ("spot", "perp"))
    if market == "perp":
        raise Unsupported(
            "aster has no perp cancel script (move_stop.js replaces stops; "
            "modify_order.js reprices limits)"
        )
    _only_wallet1(account, "cancel_spot.js")
    symbol = _symbol(args)
    assert symbol
    if _opt(args, "order_id") is not None:
        raise Unsupported(
            "aster cancel_spot.js selects the order by price, not order_id"
        )
    return Invocation("cancel_spot.js", (symbol, _num(args, "price") or ""))


def _mexc_cancel(account: str, args: Dict[str, Any]) -> Invocation:
    market = _choice(args, "market", ("spot", "perp"))
    if market == "spot":
        raise Unsupported(
            "mexc has no spot cancel script (orders.js cancels futures only)"
        )
    order_id = str(_opt(args, "order_id") or "")
    if not _ORDER_ID.match(order_id):
        raise BadArgs("order_id is required (digits; see exchange_open_orders)")
    # orders.js --cancel has no confirm step of its own: no --yes/--dry-run.
    return Invocation(
        "orders.js", ("--cancel", order_id, "--account", account), confirms=False
    )


def _transfer_legs(args: Dict[str, Any]) -> Tuple[str, str]:
    src = _choice(args, "from", ("spot", "futures"))
    dst = _choice(args, "to", ("spot", "futures"))
    if src == dst:
        raise BadArgs("from and to must differ")
    return src, dst


def _asset(args: Dict[str, Any]) -> str:
    asset = str(_opt(args, "asset") or "").strip().upper()
    if not _ASSET.match(asset):
        raise BadArgs("asset is required, e.g. USDT")
    return asset


def _aster_transfer(account: str, args: Dict[str, Any]) -> Invocation:
    src, _dst = _transfer_legs(args)
    _only_wallet1(account, "transfer.js")
    direction = "spot2fut" if src == "spot" else "fut2spot"
    return Invocation(
        "transfer.js", (direction, _asset(args), _num(args, "amount") or "")
    )


def _mexc_transfer(account: str, args: Dict[str, Any]) -> Invocation:
    src, dst = _transfer_legs(args)
    return Invocation(
        "transfer.js",
        (
            src.upper(),
            dst.upper(),
            _asset(args),
            _num(args, "amount") or "",
            "--account",
            account,
        ),
    )


def _gap(why: str) -> Builder:
    def builder(account: str, args: Dict[str, Any]) -> Invocation:
        raise Unsupported(why)

    return builder


ACTIONS: Dict[str, Dict[str, Builder]] = {
    "report": {"aster": _aster_report, "mexc": _mexc_report},
    "open_orders": {"aster": _aster_open_orders, "mexc": _mexc_open_orders},
    "trades": {
        "aster": _aster_trades,
        "mexc": _gap("mexc has no trade-history script"),
    },
    "analytics": {
        "aster": _gap("analytics exists for mexc only (fetchmexc/analytics.js)"),
        "mexc": _mexc_analytics,
    },
    "place_order": {"aster": _aster_place, "mexc": _mexc_place},
    "close_position": {
        "aster": _aster_close,
        "mexc": _gap("mexc has no close-position script"),
    },
    "move_stop": {
        "aster": _aster_move_stop,
        "mexc": _gap(
            "mexc has no stop script; set stop_price when placing the perp order"
        ),
    },
    "modify_order": {
        "aster": _aster_modify,
        "mexc": _gap("mexc has no modify-order script"),
    },
    "cancel_order": {"aster": _aster_cancel, "mexc": _mexc_cancel},
    "transfer": {"aster": _aster_transfer, "mexc": _mexc_transfer},
}
# Read-only actions: no --yes/--dry-run, longer timeout. The GATE's read-only
# list is separate and authoritative (destructive.EXCHANGE_READ_ONLY_TOOLS).
READ_ACTIONS = ("report", "open_orders", "trades", "analytics")


# -- handlers -----------------------------------------------------------------


def _text(text: str, error: bool = False) -> Dict[str, Any]:
    out: Dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if error:
        out["isError"] = True
    return out


def _client(services: AgentServices) -> Optional[ExchangeClient]:
    client = getattr(services, "exchange", None)
    return client if isinstance(client, ExchangeClient) else None


def exchange_available(services: AgentServices, ctx: TurnContext) -> bool:
    """Owner turns on a profile that opted in (``exchange: true``)."""
    if _client(services) is None or ctx.role != "owner":
        return False
    if not services.profile_registry:
        return False
    base = services.profile_registry.get_by_slug(ctx.profile_slug.split("#", 1)[0])
    return bool(getattr(base, "exchange", False)) if base else False


def _targets(
    client: ExchangeClient, args: Dict[str, Any], required: bool
) -> List[Tuple[str, str]]:
    """``[(venue, account)]`` the call addresses; validated against .env."""
    configured = client.accounts()
    venue = str(_opt(args, "exchange") or "").strip().lower()
    account = str(_opt(args, "account") or "").strip()
    if venue and venue not in VENUES:
        raise BadArgs(f"exchange must be one of {', '.join(VENUES)}")
    if required and not (venue and account):
        raise BadArgs("exchange and account are required (see exchange_accounts)")
    venues = [venue] if venue else list(VENUES)
    out = [
        (v, a)
        for v in venues
        for a in configured.get(v, [])
        if not account or a == account
    ]
    if not out:
        raise BadArgs(
            f"no configured account matches exchange={venue or '*'} "
            f"account={account or '*'} (see exchange_accounts)"
        )
    return out


def _run_action(action: str) -> Any:
    write = action not in READ_ACTIONS

    async def handler(
        services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
    ) -> Dict[str, Any]:
        client = _client(services)
        if client is None or ctx.role != "owner":
            return _text("error: exchange tools are not available here", True)
        args = dict(args or {})
        dry_run = args.get("dry_run") is True
        try:
            if action == "analytics":
                # Public market data: no account involved.
                venue = str(_opt(args, "exchange") or "mexc").lower()
                if venue not in VENUES:
                    raise BadArgs(f"exchange must be one of {', '.join(VENUES)}")
                targets = [(venue, "")]
            else:
                targets = _targets(client, args, required=write)
            plans = [(v, a, ACTIONS[action][v](a, args)) for v, a in targets]
        except (BadArgs, Unsupported) as exc:
            kind = "not supported" if isinstance(exc, Unsupported) else "bad arguments"
            return _text(f"error ({kind}): {exc}", True)

        parts: List[str] = []
        failed = False
        for venue, account, inv in plans:
            argv = inv.argv
            if write and inv.confirms:
                argv = (*argv, "--dry-run" if dry_run else "--yes")
            elif write and dry_run:
                # Script has no confirm step; describe instead of running it.
                parts.append(
                    f"[{venue} {account}] DRY RUN: would run "
                    f"node {inv.script} {' '.join(argv)} (no preview available "
                    "from this script; nothing sent)"
                )
                continue
            timeout = WRITE_TIMEOUT_SECONDS if write else READ_TIMEOUT_SECONDS
            run = Invocation(inv.script, argv, inv.confirms)
            logger.info(
                "exchange tool run",
                action=action,
                venue=venue,
                account=account,
                script=inv.script,
                dry_run=dry_run,
                profile=ctx.profile_slug,
            )
            code, output = await client.run(venue, run, timeout)
            label = f"[{venue}{' ' + account if account else ''}] node {inv.script} {' '.join(argv)}"
            if code == -1:
                note = " (timed out" + (
                    "; the request may still have reached the exchange, check open orders)"
                    if write and not dry_run
                    else ")"
                )
                parts.append(f"{label}\nFAILED{note}\n{output}")
                failed = True
            elif code != 0:
                parts.append(f"{label}\nFAILED (exit {code})\n{output}")
                failed = True
            else:
                parts.append(f"{label}\n{output}")
        return _text("\n\n".join(parts), failed)

    return handler


async def _accounts(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    client = _client(services)
    if client is None or ctx.role != "owner":
        return _text("error: exchange tools are not available here", True)
    accounts = client.accounts()
    lines = [
        f"{venue}: {', '.join(names) or '(none configured)'}"
        for venue, names in accounts.items()
    ]
    return _text("\n".join(lines))


# -- schemas ------------------------------------------------------------------

_EXCHANGE = {"type": "string", "enum": list(VENUES), "description": "aster | mexc"}
_ACCOUNT = {"type": "string", "description": "Account name from exchange_accounts"}
_SYMBOL_S = {
    "type": "string",
    "description": "e.g. BTCUSDT (mexc perp also accepts BTC_USDT)",
}
_NUMBER = {"type": ["number", "string"]}
_DRY_RUN = {
    "type": "boolean",
    "description": "true = run the script's preview and checks, send nothing "
    "(no owner button). Use it first to show the owner what will be sent.",
}


def _schema(props: Dict[str, Any], *required: str) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(required)}


_WRITE_NOTE = (
    " Every call without dry_run=true waits for the owner's Approve/Deny button."
)

_SPECS: List[Tuple[str, str, Dict[str, Any], Any]] = [
    (
        "exchange_accounts",
        "Configured exchange accounts, names only (aster: wallet1/wallet2, mexc: labels).",
        _schema({}),
        _accounts,
    ),
    (
        "exchange_report",
        "Balances and open positions (aster index.js, quick unless full=true; mexc "
        "index.js spot + futures). Omit exchange/account for all accounts.",
        _schema(
            {
                "exchange": _EXCHANGE,
                "account": _ACCOUNT,
                "full": {
                    "type": "boolean",
                    "description": "aster: full report with funding/income/spot P&L (slow)",
                },
            }
        ),
        _run_action("report"),
    ),
    (
        "exchange_open_orders",
        "Open orders. aster orders.js: open + last-7-day perp and spot orders (no symbol "
        "filter). mexc orders.js: open futures orders, optional symbol.",
        _schema({"exchange": _EXCHANGE, "account": _ACCOUNT, "symbol": _SYMBOL_S}),
        _run_action("open_orders"),
    ),
    (
        "exchange_trades",
        "Recent perp fills + open orders for one symbol (aster query_trades.js only).",
        _schema(
            {
                "exchange": _EXCHANGE,
                "account": _ACCOUNT,
                "symbol": _SYMBOL_S,
                "limit": {"type": "integer"},
            },
            "symbol",
        ),
        _run_action("trades"),
    ),
    (
        "exchange_analytics",
        "Rolling volatility and drawdown from daily candles (mexc analytics.js; "
        "public data, no account).",
        _schema(
            {
                "exchange": _EXCHANGE,
                "symbol": _SYMBOL_S,
                "days": {
                    "type": "integer",
                    "description": "daily candles, default 500; must exceed window",
                },
                "window": {"type": "integer", "description": "stddev window, default 30"},
            }
        ),
        _run_action("analytics"),
    ),
    (
        "exchange_place_order",
        "Place an order. spot: side BUY|SELL; market BUY spends quote_amount, market "
        "SELL sells quantity, limit takes quantity+price (mexc also quote_amount+price). "
        "perp: side LONG|SHORT, margin (USDT) x leverage, price for limit, optional "
        "stop_price (aster --stop, mexc --sl), take_profit (mexc only), isolated (aster). "
        "aster trades wallet1 only." + _WRITE_NOTE,
        _schema(
            {
                "exchange": _EXCHANGE,
                "account": _ACCOUNT,
                "market": {"type": "string", "enum": ["spot", "perp"]},
                "symbol": _SYMBOL_S,
                "side": {"type": "string", "enum": ["BUY", "SELL", "LONG", "SHORT"]},
                "type": {"type": "string", "enum": ["market", "limit"]},
                "quantity": _NUMBER,
                "quote_amount": _NUMBER,
                "margin": _NUMBER,
                "leverage": {"type": "integer"},
                "price": _NUMBER,
                "stop_price": _NUMBER,
                "take_profit": _NUMBER,
                "isolated": {"type": "boolean"},
                "dry_run": _DRY_RUN,
            },
            "exchange",
            "account",
            "market",
            "symbol",
            "side",
            "type",
        ),
        _run_action("place_order"),
    ),
    (
        "exchange_close_position",
        "Close an open perp position fully, or partially with quantity, via a "
        "reduce-only market or limit order (aster close.js)." + _WRITE_NOTE,
        _schema(
            {
                "exchange": _EXCHANGE,
                "account": _ACCOUNT,
                "symbol": _SYMBOL_S,
                "type": {"type": "string", "enum": ["market", "limit"]},
                "price": _NUMBER,
                "quantity": _NUMBER,
                "dry_run": _DRY_RUN,
            },
            "exchange",
            "account",
            "symbol",
            "type",
        ),
        _run_action("close_position"),
    ),
    (
        "exchange_move_stop",
        "Set or move the close-position stop (STOP_MARKET, mark price) on an open perp "
        "position: places the new stop, then cancels old ones; refuses a stop on the "
        "wrong side of mark (aster move_stop.js, wallet1)." + _WRITE_NOTE,
        _schema(
            {
                "exchange": _EXCHANGE,
                "account": _ACCOUNT,
                "symbol": _SYMBOL_S,
                "stop_price": _NUMBER,
                "dry_run": _DRY_RUN,
            },
            "exchange",
            "account",
            "symbol",
            "stop_price",
        ),
        _run_action("move_stop"),
    ),
    (
        "exchange_modify_order",
        "Reprice an open perp LIMIT order in place (same id, same quantity); pass "
        "order_id when several are open (aster modify_order.js)." + _WRITE_NOTE,
        _schema(
            {
                "exchange": _EXCHANGE,
                "account": _ACCOUNT,
                "symbol": _SYMBOL_S,
                "price": _NUMBER,
                "order_id": {"type": "string"},
                "dry_run": _DRY_RUN,
            },
            "exchange",
            "account",
            "symbol",
            "price",
        ),
        _run_action("modify_order"),
    ),
    (
        "exchange_cancel_order",
        "Cancel an open order. aster: spot only, the one order at `price` "
        "(cancel_spot.js, wallet1). mexc: futures only, by order_id (orders.js "
        "--cancel)." + _WRITE_NOTE,
        _schema(
            {
                "exchange": _EXCHANGE,
                "account": _ACCOUNT,
                "market": {"type": "string", "enum": ["spot", "perp"]},
                "symbol": _SYMBOL_S,
                "price": _NUMBER,
                "order_id": {"type": "string"},
                "dry_run": _DRY_RUN,
            },
            "exchange",
            "account",
            "market",
        ),
        _run_action("cancel_order"),
    ),
    (
        "exchange_transfer",
        "Move an asset between the account's own spot and futures wallets (not a "
        "withdrawal). aster: wallet1 only." + _WRITE_NOTE,
        _schema(
            {
                "exchange": _EXCHANGE,
                "account": _ACCOUNT,
                "from": {"type": "string", "enum": ["spot", "futures"]},
                "to": {"type": "string", "enum": ["spot", "futures"]},
                "asset": {"type": "string"},
                "amount": _NUMBER,
                "dry_run": _DRY_RUN,
            },
            "exchange",
            "account",
            "from",
            "to",
            "asset",
            "amount",
        ),
        _run_action("transfer"),
    ),
]

EXCHANGE_SPECS: List[ToolSpec] = [
    ToolSpec(
        name=name,
        description=description,
        schema=schema,
        handler=handler,
        available=exchange_available,
    )
    for name, description, schema, handler in _SPECS
]
