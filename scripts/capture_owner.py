#!/usr/bin/env python3
"""Capture the bot's first owner during install (dv-5c83).

Run before the service starts. Validates the token, throws away every update
Telegram was already holding, waits for a fresh ``/start``, confirms who it came
from, and writes the result into ``.env``.

Standard library only, on purpose: this runs in ``install.sh`` *before*
``poetry install``, so it cannot import anything from ``src/``.

Why the drain (step 2) matters -- it is the whole security argument:
Telegram keeps undelivered updates for ~24h. A bot registered with BotFather
before the install may already be holding messages: the owner's own testing, or
a stranger who found the username. "First message this bot ever received" can
therefore be a stranger's ``/start`` from yesterday, and no amount of care at
install time races against that -- it was already lost. Draining first is what
makes "the next message" mean "a message sent after I printed this prompt".

Usage:
    capture_owner.py --token <token> --env-file .env
    capture_owner.py --token <token> --env-file .env --owner-id 12345  # headless
"""

import argparse
import importlib.util
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

API = "https://api.telegram.org/bot{token}/{method}"
# Telegram's own cap is 50s; the poll is chunked so Ctrl-C stays responsive.
POLL_TIMEOUT = 25
DEFAULT_DEADLINE = 300


class TelegramError(RuntimeError):
    """A Telegram API call failed, or the token is not usable."""


def call(token: str, method: str, http_timeout: int = 30, **params: Any) -> Any:
    """One Bot API call. Raises TelegramError on anything but ok:true.

    The HTTP timeout is deliberately NOT called ``timeout``: getUpdates takes a
    Telegram-side ``timeout`` of its own in ``params``, and a same-named
    keyword here collides with it -- passing both raises TypeError, and
    ``{"timeout": 0}`` in params would otherwise become a 0-second HTTP
    timeout rather than a non-blocking poll.
    """
    url = API.format(token=token, method=method)
    data = json.dumps(params).encode() if params else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:200]
        raise TelegramError(f"{method} failed: HTTP {exc.code} {body}") from exc
    except urllib.error.URLError as exc:
        raise TelegramError(f"{method} failed: {exc.reason}") from exc
    if not payload.get("ok"):
        raise TelegramError(f"{method} failed: {payload.get('description')}")
    return payload["result"]


def get_me(token: str) -> Dict[str, Any]:
    """Validate the token and return the bot account. Fails fast on a typo."""
    me = call(token, "getMe")
    if not me.get("username"):
        raise TelegramError("getMe returned no username -- is this a bot token?")
    return me


def drain_updates(token: str) -> int:
    """Discard every pending update. Returns how many were thrown away.

    The count is returned so the caller can PRINT it: if this step ever
    silently stops working, a visible "discarded 0" on a bot that had a backlog
    is the only warning anyone gets before ownership goes to a stranger.
    """
    discarded = 0
    offset: Optional[int] = None
    while True:
        params: Dict[str, Any] = {"timeout": 0, "limit": 100}
        if offset is not None:
            params["offset"] = offset
        batch = call(token, "getUpdates", **params)
        if not batch:
            return discarded
        discarded += len(batch)
        offset = batch[-1]["update_id"] + 1


def _sender(update: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The human behind an update, for the message-ish kinds we accept."""
    for key in ("message", "edited_message", "channel_post", "callback_query"):
        payload = update.get(key)
        if isinstance(payload, dict) and isinstance(payload.get("from"), dict):
            return payload["from"]
    return None


def wait_for_sender(
    token: str,
    deadline_seconds: int = DEFAULT_DEADLINE,
    *,
    now: Any = time.monotonic,
) -> Tuple[Dict[str, Any], int]:
    """Long-poll until someone messages the bot. Returns (from, update_id).

    Only call this AFTER drain_updates, or the first result may be a message
    from before the install started.
    """
    started = now()
    offset: Optional[int] = None
    while now() - started < deadline_seconds:
        params: Dict[str, Any] = {"timeout": POLL_TIMEOUT, "limit": 10}
        if offset is not None:
            params["offset"] = offset
        batch = call(token, "getUpdates", http_timeout=POLL_TIMEOUT + 10, **params)
        for update in batch:
            offset = update["update_id"] + 1
            sender = _sender(update)
            if sender and not sender.get("is_bot"):
                return sender, update["update_id"]
        if batch:
            # Only bots/unsupported kinds in this batch: consume and keep going.
            continue
    raise TimeoutError(
        f"No message received in {deadline_seconds}s."
        " Re-run install.sh, or pass --owner-id <your telegram id>."
    )


def describe(sender: Dict[str, Any]) -> str:
    """Human-readable identity for the confirmation prompt."""
    name = " ".join(
        part for part in (sender.get("first_name"), sender.get("last_name")) if part
    )
    handle = f"@{sender['username']}" if sender.get("username") else None
    label = " ".join(part for part in (name, handle) if part) or "unknown"
    return f"{label} (id {sender['id']})"


# --- .env writing -------------------------------------------------------


def _load_env_file_module() -> Any:
    """``src/config/env_file.py`` by path — the one .env writer (dv-6b6c).

    Loaded by path, not imported as ``src.config.env_file``: the package
    import would run ``src/config/__init__`` (pydantic), which is not
    installed yet when install.sh runs this script. The module itself is
    standard library only.
    """
    path = Path(__file__).resolve().parent.parent / "src" / "config" / "env_file.py"
    spec = importlib.util.spec_from_file_location("_devvy_env_file", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_env_file = _load_env_file_module()
upsert_env = _env_file.upsert_env
write_env = _env_file.write_env


# --- CLI ----------------------------------------------------------------


def _confirm(prompt: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    return input(f"{prompt} [Y/n] ").strip().lower() in ("", "y", "yes")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--token", required=True, help="Telegram bot token")
    p.add_argument("--env-file", type=Path, default=Path(".env"))
    p.add_argument(
        "--owner-id",
        type=int,
        help="Skip the interactive capture; use this Telegram id (headless).",
    )
    p.add_argument("--deadline", type=int, default=DEFAULT_DEADLINE)
    p.add_argument("--yes", action="store_true", help="Assume yes at the confirmation.")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Do everything except write .env; print what would be written.",
    )
    p.add_argument(
        "--validate-only",
        action="store_true",
        help=(
            "Check the token with getMe, print the bot username on stdout and"
            " exit. Nothing else runs and nothing is written -- install.sh uses"
            " this to fail fast on a bad token and to fill TELEGRAM_BOT_USERNAME"
            " without anyone typing it."
        ),
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    try:
        me = get_me(args.token)
    except TelegramError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    username = me["username"]

    if args.validate_only:
        # stdout is the username and nothing else -- it is captured by
        # `USERNAME=$(... --validate-only)` in install.sh. Chatter goes to
        # stderr so it still reaches the person running the install.
        banner = f"Bot: @{username} ({me.get('first_name', '')})".rstrip()
        print(banner, file=sys.stderr)
        print(username)
        return 0

    print(f"Bot: @{username} ({me.get('first_name', '')})".rstrip())

    if args.owner_id is not None:
        owner_id = args.owner_id
        print(f"Owner: {owner_id} (from --owner-id, no capture)")
    else:
        try:
            discarded = drain_updates(args.token)
            # Printed unconditionally -- see drain_updates' docstring.
            print(f"Discarded {discarded} pending update(s) from before install.")
            while True:
                print(f"\nOpen https://t.me/{username} and send /start -- waiting…")
                sender, _ = wait_for_sender(args.token, args.deadline)
                if _confirm(
                    f"Got a message from {describe(sender)} -- is that you?",
                    assume_yes=args.yes,
                ):
                    owner_id = int(sender["id"])
                    break
                print("Ignoring that one. Send /start again from your own account.")
        except TelegramError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        except TimeoutError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 3
        except KeyboardInterrupt:
            print("\nAborted; nothing written.", file=sys.stderr)
            return 130

    values = {
        "TELEGRAM_BOT_TOKEN": args.token,
        "TELEGRAM_BOT_USERNAME": username,
        "ALLOWED_USERS": str(owner_id),
        # The bot refuses to start with no owner rather than opening up; make
        # sure a stale DEVELOPMENT_MODE in a copied .env cannot confuse that.
        "DEVELOPMENT_MODE": "false",
    }
    if args.dry_run:
        print("\n--dry-run, would write to", args.env_file)
        for key in values:
            shown = "<token>" if key == "TELEGRAM_BOT_TOKEN" else values[key]
            print(f"  {key}={shown}")
        return 0

    backup = write_env(args.env_file, values)
    print(f"\nWrote {args.env_file} (mode 600); owner is {owner_id}.")
    if backup != args.env_file:
        print(f"Previous .env backed up to {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
