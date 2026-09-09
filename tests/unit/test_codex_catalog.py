"""`/model`'s Codex section is built from `codex debug models` (dv-0c01).

The canned catalog below is the real payload shape captured from
codex-cli 0.152.0 on 2026-09-03 (fields the bot reads only; the live payload
is ~250KB, mostly per-model instruction templates).
"""

import asyncio
import json

import pytest

from src.agent_backends.codex_catalog import (
    FALLBACK_MODELS,
    CodexModelCatalog,
    parse_codex_models,
    supported_refs,
)

CANNED = {
    "models": [
        {
            "slug": "gpt-reserve",
            "display_name": "GPT-Reserve",
            "visibility": "hide",
            "context_window": 272000,
            "priority": 3,
        },
        {
            "slug": "gpt-5.4-mini",
            "display_name": "GPT-5.4-Mini",
            "visibility": "list",
            "context_window": 272000,
            "priority": 23,
        },
        {
            "slug": "gpt-5.6-terra",
            "display_name": "GPT-5.6-Terra",
            "visibility": "list",
            "context_window": 272000,
            "priority": 7,
        },
        {
            "slug": "gpt-5.5",
            "display_name": "GPT-5.5",
            "visibility": "list",
            "context_window": 272000,
            "priority": 12,
        },
        {
            "slug": "gpt-5.6-luna",
            "display_name": "GPT-5.6-Luna",
            "visibility": "list",
            "context_window": 272000,
            "priority": 8,
        },
        {
            "slug": "codex-auto-review",
            "display_name": "Codex Auto Review",
            "visibility": "hide",
            "context_window": 272000,
            "priority": 43,
        },
    ]
}


def test_parse_keeps_listable_models_in_priority_order():
    models = parse_codex_models(json.dumps(CANNED))
    assert [m.slug for m in models] == [
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.4-mini",
    ]
    assert models[0].display_name == "GPT-5.6-Terra"
    assert models[0].context_window == 272000
    assert models[0].ref == "openai-codex/gpt-5.6-terra"


def test_parse_rejects_a_catalog_with_nothing_listable():
    hidden = {"models": [dict(CANNED["models"][0])]}
    with pytest.raises(ValueError):
        parse_codex_models(hidden)


def test_parse_rejects_unrecognisable_payloads():
    with pytest.raises(ValueError):
        parse_codex_models({"nope": []})


def test_supported_refs_always_keeps_the_pinned_constants():
    models = parse_codex_models(CANNED)
    refs = supported_refs(
        models, always=("openai-codex/gpt-5.5", "openai-codex/gpt-5.4-mini")
    )
    assert "openai-codex/gpt-5.6-luna" in refs
    assert "openai-codex/gpt-5.5" in refs
    # ...even when the live catalog has dropped one of them.
    refs = supported_refs([], always=("openai-codex/gpt-5.4-mini",))
    assert refs == frozenset({"openai-codex/gpt-5.4-mini"})


class _Proc:
    def __init__(self, stdout: bytes, returncode: int = 0, stderr: bytes = b""):
        self._out = stdout
        self._err = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._out, self._err


def _patch_exec(monkeypatch, proc, counter=None):
    async def fake_exec(*args, **kwargs):
        if counter is not None:
            counter.append(args)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)


async def test_catalog_fetches_parses_and_caches(monkeypatch):
    calls: list = []
    _patch_exec(monkeypatch, _Proc(json.dumps(CANNED).encode()), calls)

    catalog = CodexModelCatalog(ttl_seconds=999)
    first = await catalog.get()
    second = await catalog.get()

    assert [m.slug for m in first] == [m.slug for m in second]
    assert len(calls) == 1  # cached, not re-shelled per keypress
    assert calls[0][:3] == ("codex", "debug", "models")
    assert [m.slug for m in catalog.cached()] == [m.slug for m in first]


async def test_invalidate_forces_a_refetch(monkeypatch):
    calls: list = []
    _patch_exec(monkeypatch, _Proc(json.dumps(CANNED).encode()), calls)

    catalog = CodexModelCatalog(ttl_seconds=999)
    await catalog.get()
    catalog.invalidate()
    await catalog.get()
    assert len(calls) == 2


async def test_failed_cli_falls_back_to_the_two_constants(monkeypatch):
    _patch_exec(monkeypatch, _Proc(b"", returncode=1, stderr=b"boom" * 200))
    catalog = CodexModelCatalog()
    assert await catalog.get() == list(FALLBACK_MODELS)


async def test_unparsable_output_falls_back(monkeypatch):
    _patch_exec(monkeypatch, _Proc(b"not json at all"))
    catalog = CodexModelCatalog()
    assert [m.slug for m in await catalog.get()] == [m.slug for m in FALLBACK_MODELS]


async def test_missing_binary_falls_back(monkeypatch):
    async def boom(*args, **kwargs):
        raise FileNotFoundError("codex")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    catalog = CodexModelCatalog(codex_bin="definitely-not-codex")
    assert await catalog.get() == list(FALLBACK_MODELS)


async def test_a_stale_catalog_survives_a_failed_refresh(monkeypatch):
    """A transient CLI failure must not demote a good catalog to the fallback."""
    _patch_exec(monkeypatch, _Proc(json.dumps(CANNED).encode()))
    catalog = CodexModelCatalog(ttl_seconds=999)
    good = await catalog.get()

    _patch_exec(monkeypatch, _Proc(b"", returncode=1))
    assert await catalog.get(force=True) == good
