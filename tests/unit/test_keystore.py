"""Tool key store + /setkey (dv-0202)."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot.orchestration.commands import keys
from src.claude.exchange_tools import ExchangeClient
from src.security.keystore import KeyStore, KeyStoreError

SECRET = "s3cr3t-VALUE-0123456789"


def test_set_list_delete_and_perms(tmp_path: Path) -> None:
    ks = KeyStore(tmp_path / "tools")
    ks.set("bybit", "BYBIT_API_KEY", SECRET)
    ks.set("bybit", "BYBIT_API_SECRET", "other-secret-value")
    assert ks.names() == {"bybit": ["BYBIT_API_KEY", "BYBIT_API_SECRET"]}
    assert (tmp_path / "tools" / "bybit.env").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "tools").stat().st_mode & 0o777 == 0o700
    assert ks.redact(f"a {SECRET} b") == "a <redacted> b"
    assert ks.delete("bybit", "BYBIT_API_KEY")
    assert ks.delete("bybit")
    assert ks.names() == {}


@pytest.mark.parametrize(
    "service,name,value",
    [("../x", "A", "v"), ("Bybit", "A", "v"), ("bybit", "a", "v"), ("b", "A", "x\ny")],
)
def test_rejects_bad_input(tmp_path: Path, service: str, name: str, value: str) -> None:
    with pytest.raises(KeyStoreError):
        KeyStore(tmp_path).set(service, name, value)


def _update(text: str) -> MagicMock:
    update = MagicMock()
    update.message.text = text
    update.message.delete = AsyncMock()
    update.message.reply_text = AsyncMock()
    update.effective_user.id = 1
    update.effective_chat.id = 42
    return update


def _context(store: KeyStore) -> MagicMock:
    ctx = MagicMock()
    ctx.bot_data = {"keystore": store, "audit_logger": None}
    ctx.bot.send_message = AsyncMock()
    return ctx


async def test_setkey_deletes_message_and_never_echoes_value(tmp_path: Path) -> None:
    store = KeyStore(tmp_path)
    update, ctx = _update(f"/setkey bybit BYBIT_API_KEY {SECRET}"), _context(store)
    await keys.setkey_command(update, ctx)
    update.message.delete.assert_awaited_once()
    assert store.env("bybit") == {"BYBIT_API_KEY": SECRET}
    sent = ctx.bot.send_message.await_args.args[1]
    assert SECRET not in sent and "BYBIT_API_KEY" in sent


async def test_keys_lists_names_only(tmp_path: Path) -> None:
    store = KeyStore(tmp_path)
    store.set("bybit", "BYBIT_API_KEY", SECRET)
    update, ctx = _update("/keys"), _context(store)
    await keys.keys_command(update, ctx)
    out = update.message.reply_text.await_args.args[0]
    assert "BYBIT_API_KEY" in out and SECRET not in out


async def test_exchange_client_passes_and_redacts_stored_keys(tmp_path: Path) -> None:
    store = KeyStore(tmp_path / "store")
    store.set("aster", "WALLET1_SIGNER_KEY", SECRET)
    venue = tmp_path / "tools" / "fetchcrypto"
    venue.mkdir(parents=True)
    (venue / "s.js").write_text("")
    fake_node = tmp_path / "node"
    fake_node.write_text("#!/bin/sh\necho got=$WALLET1_SIGNER_KEY\n")
    fake_node.chmod(0o755)
    client = ExchangeClient(tmp_path / "tools", node=str(fake_node), keystore=store)
    from src.claude.exchange_tools import Invocation

    code, out = await client.run("aster", Invocation("s.js", ()), timeout=10)
    assert code == 0 and "got=<redacted>" in out and SECRET not in out


def test_withkeys_injects_and_redacts(tmp_path: Path) -> None:
    store = KeyStore(tmp_path / "secrets" / "tools")
    store.set("demo", "DEMO_KEY", SECRET)
    repo = Path(__file__).resolve().parents[2]
    env = {**os.environ, "DEVVY_DATA_ROOT": str(tmp_path)}
    res = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts" / "withkeys"),
            "demo",
            "--",
            "sh",
            "-c",
            'echo "k=$DEMO_KEY"',
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert res.stdout.strip() == "k=<redacted>"
