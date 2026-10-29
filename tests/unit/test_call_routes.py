"""Per-purpose route table for plain calls (dv-781f phase 1).

Callers pass a purpose (plus, rarely, an explicit harness/model override);
the router resolves harness + model from ``DEFAULT_CALL_ROUTES`` overlaid by
the owner's ``config/models.yaml`` (path: ``settings.model_routes_path``).

Contract pinned here (names from docs/research/dv-781f/analysis.md):
- ``src.model_router.types``: ``PLAIN_PURPOSES``, ``TURN_PURPOSES``;
  ``CallRequest`` takes ``model``/``harness`` optional and a ``backend`` hint.
- ``src.model_router.routes``: ``DEFAULT_CALL_ROUTES`` (one row per plain
  purpose), ``load_call_routes(path) -> CallRoutes`` (absent file = built-ins,
  bad file = ``ValueError``), ``boot_call_routes(path) -> (CallRoutes,
  alert_or_None)`` (bad file = built-ins + an alert text for the owner).
- ``ModelRouter(call_routes=...)``; ``None`` = built-in routes.
"""

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from src.model_router import (
    CallRequest,
    CallResult,
    ModelCallError,
    ModelCallTimeout,
    ModelRouter,
    complete_text,
    set_router,
)
from src.model_router.harness import HarnessVersions
from src.storage.database import DatabaseManager
from src.storage.model_calls import ModelCallRepository

REPO = Path(__file__).resolve().parents[2]

CODEX_MODEL = "openai-codex/gpt-6-sol"

# Today's (harness, model) per plain purpose, from the callers' hardcoded
# values (analysis.md "full default table"); ask_explain is on sonnet (phase 3).
GOLDEN = {
    ("extraction", "claude"): ("claude", "sonnet"),
    ("extraction", "openai_codex"): ("openai_codex", CODEX_MODEL),
    ("screen_extraction", "claude"): ("claude", "sonnet"),
    ("screen_extraction", "openai_codex"): ("openai_codex", CODEX_MODEL),
    ("reconcile", None): ("claude", "haiku"),
    ("consolidation", None): ("claude", "haiku"),
    ("fact_quality", None): ("claude", "haiku"),
    ("onboarding", None): ("claude", "sonnet"),
    ("ask_explain", None): ("claude", "sonnet"),
    ("scorer", None): ("openrouter", None),
    ("screen", None): ("claude", "haiku"),
    # dv-9133: the design critic is a different family than the generator,
    # pinned to Codex with no fallback.
    ("design_critic", None): ("openai_codex", CODEX_MODEL),
}

NINE_PLAIN = {
    "extraction",
    "screen_extraction",
    "reconcile",
    "consolidation",
    "fact_quality",
    "onboarding",
    "ask_explain",
    "scorer",
    "screen",
    "design_critic",
}

_ASK_JSON = json.dumps(
    {
        "what": "Удаляет файл.",
        "changes": "Удаляет x.",
        "sends": "Ничего.",
        "verdict": "caution",
        "injection_suspected": False,
    }
)


class _Versions(HarnessVersions):
    async def get(self, harness: str) -> str:
        return f"{harness}-9.9"


class _Fake:
    """One plain-call adapter: records each request, returns or raises."""

    def __init__(self, harness: str, *, raises: Optional[BaseException] = None):
        self.harness = harness
        self.raises = raises
        self.calls: List[CallRequest] = []
        self.text = "ok"

    async def __call__(self, request: CallRequest) -> CallResult:
        self.calls.append(request)
        if self.raises is not None:
            raise self.raises
        return CallResult(text=self.text)


def _fakes(**raises: BaseException) -> Dict[str, _Fake]:
    return {
        h: _Fake(h, raises=raises.get(h))
        for h in ("claude", "openai_codex", "openrouter")
    }


def _only_call(fakes: Dict[str, _Fake]) -> CallRequest:
    """The single request any adapter saw, with which harness ran it."""
    seen = [(h, r) for h, f in fakes.items() for r in f.calls]
    assert len(seen) == 1, f"expected one adapter call, got {seen}"
    harness, request = seen[0]
    assert request.harness == harness
    return request


def _plain(purpose: str, **kw: Any) -> CallRequest:
    return CallRequest(purpose=purpose, prompt="p", system_prompt="s", **kw)


def _yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _routes(tmp_path: Path, body: str):
    from src.model_router.routes import load_call_routes

    return load_call_routes(_yaml(tmp_path, body))


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'test.db'}")
    await manager.initialize()
    yield manager
    await manager.close()


async def _rows(db: DatabaseManager) -> List[dict]:
    async with db.get_connection() as conn:
        cur = await conn.execute("SELECT * FROM model_calls ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


# --- scenario 1: no models.yaml = today's routes (golden) --------------------


@pytest.mark.parametrize("with_missing_file", [False, True])
async def test_no_models_yaml_every_plain_purpose_runs_on_todays_route(
    tmp_path, with_missing_file
):
    """No file (or a path that does not exist): all 9 plain purposes resolve
    to the harness + model their callers hardcode today."""
    from src.model_router.routes import load_call_routes

    assert {p for p, _ in GOLDEN} == NINE_PLAIN
    for (purpose, backend), (harness, model) in GOLDEN.items():
        fakes = _fakes()
        kwargs: Dict[str, Any] = {"plain_callers": fakes}
        if with_missing_file:
            kwargs["call_routes"] = load_call_routes(tmp_path / "absent.yaml")
        router = ModelRouter(**kwargs)

        await router.complete(_plain(purpose, backend=backend))

        request = _only_call(fakes)
        assert (request.harness, request.model) == (harness, model), purpose


# --- scenario 2: default harness switch moves every unpinned purpose ---------

_TO_CODEX = f"""
default:
  harness: openai_codex
  models:
    claude: sonnet
    openai_codex: {CODEX_MODEL}
"""

# Purposes the built-in table does not pin to a harness of their own
# (extraction/screen_extraction follow the chat backend, scorer is openrouter).
_UNPINNED = sorted(
    NINE_PLAIN - {"extraction", "screen_extraction", "scorer", "design_critic"}
)


async def test_default_harness_codex_moves_every_unpinned_purpose(tmp_path):
    routes = _routes(tmp_path, _TO_CODEX)
    for purpose in _UNPINNED:
        fakes = _fakes()
        router = ModelRouter(plain_callers=fakes, call_routes=routes)

        await router.complete(_plain(purpose))

        request = _only_call(fakes)
        assert (request.harness, request.model) == (
            "openai_codex",
            CODEX_MODEL,
        ), purpose

    # A purpose pinned to its own harness keeps it.
    fakes = _fakes()
    await ModelRouter(plain_callers=fakes, call_routes=routes).complete(
        _plain("scorer")
    )
    assert _only_call(fakes).harness == "openrouter"


async def test_default_harness_codex_reaches_real_callers_without_code_edits(
    tmp_path,
):
    """The migrated callers pass a purpose only: ask_explain and
    complete_text (consolidation/fact_quality/onboarding) follow the file."""
    from src.security import ask_explain

    fakes = _fakes()
    fakes["openai_codex"].text = _ASK_JSON
    set_router(
        ModelRouter(plain_callers=fakes, call_routes=_routes(tmp_path, _TO_CODEX))
    )
    try:
        html = await ask_explain.explain("delete: rm", "rm -rf x")
        assert html is not None
        assert [r.purpose for r in fakes["openai_codex"].calls] == ["ask_explain"]
        assert not fakes["claude"].calls

        text = await complete_text("p", purpose="consolidation", system_prompt="s")
        assert text == _ASK_JSON
        assert fakes["openai_codex"].calls[-1].purpose == "consolidation"
        assert fakes["openai_codex"].calls[-1].model == CODEX_MODEL
        assert not fakes["claude"].calls
    finally:
        set_router(None)


# --- scenario 3: precedence + fallback ---------------------------------------

_PRECEDENCE = """
default:
  harness: openrouter
  models:
    claude: sonnet
    openai_codex: openai-codex/gpt-6-sol
    openrouter: z-ai/glm-5.3
purposes:
  consolidation:
    models:
      claude: haiku
      openrouter: z-ai/glm-5.2
"""


@pytest.mark.parametrize(
    "purpose, kwargs, expected",
    [
        # default route: reconcile has no openrouter model of its own
        ("reconcile", {}, ("openrouter", "z-ai/glm-5.3")),
        # purpose override beats the default
        ("consolidation", {}, ("openrouter", "z-ai/glm-5.2")),
        # explicit model beats the purpose override
        ("consolidation", {"model": "qwen/qwen3-x"}, ("openrouter", "qwen/qwen3-x")),
        # explicit harness beats the default harness; model from the purpose row
        ("consolidation", {"harness": "claude"}, ("claude", "haiku")),
        # explicit harness + model beat everything
        (
            "consolidation",
            {"harness": "openai_codex", "model": "m-explicit"},
            ("openai_codex", "m-explicit"),
        ),
    ],
)
async def test_caller_arg_beats_purpose_override_beats_default(
    tmp_path, purpose, kwargs, expected
):
    fakes = _fakes()
    router = ModelRouter(
        plain_callers=fakes, call_routes=_routes(tmp_path, _PRECEDENCE)
    )

    await router.complete(_plain(purpose, **kwargs))

    request = _only_call(fakes)
    assert (request.harness, request.model) == expected


_FALLBACK = """
purposes:
  consolidation:
    fallback:
      harness: openai_codex
"""


@pytest.mark.parametrize(
    "failure, outcome",
    [
        (ModelCallError("claude exited 1"), "error"),
        (ModelCallTimeout("timeout after 120s"), "timeout"),
    ],
)
async def test_fallback_runs_once_and_both_calls_get_a_row(
    tmp_path, db, failure, outcome
):
    fakes = _fakes(claude=failure)
    router = ModelRouter(
        plain_callers=fakes,
        call_routes=_routes(tmp_path, _FALLBACK),
        recorder=ModelCallRepository(db),
        versions=_Versions(),
    )

    result = await router.complete(_plain("consolidation"))

    assert result.text == "ok"
    assert len(fakes["claude"].calls) == 1
    (fb,) = fakes["openai_codex"].calls
    assert fb.model == CODEX_MODEL
    rows = await _rows(db)
    assert [(r["harness"], r["outcome"]) for r in rows] == [
        ("claude", outcome),
        ("openai_codex", "ok"),
    ]
    assert rows[0]["model"] == "haiku" and rows[1]["model"] == CODEX_MODEL
    assert all(r["purpose"] == "consolidation" for r in rows)


async def test_failing_fallback_is_not_retried_again(tmp_path, db):
    fakes = _fakes(
        claude=ModelCallError("primary down"),
        openai_codex=ModelCallError("fallback down"),
    )
    router = ModelRouter(
        plain_callers=fakes,
        call_routes=_routes(tmp_path, _FALLBACK),
        recorder=ModelCallRepository(db),
        versions=_Versions(),
    )

    with pytest.raises(ModelCallError):
        await router.complete(_plain("consolidation"))

    assert len(fakes["claude"].calls) == 1
    assert len(fakes["openai_codex"].calls) == 1
    assert len(await _rows(db)) == 2


async def test_no_fallback_on_programming_errors_or_without_config(tmp_path):
    # ValueError is a bug, not an outage: never retried elsewhere.
    fakes = _fakes(claude=ValueError("bad request shape"))
    router = ModelRouter(plain_callers=fakes, call_routes=_routes(tmp_path, _FALLBACK))
    with pytest.raises(ValueError):
        await router.complete(_plain("consolidation"))
    assert not fakes["openai_codex"].calls

    # Built-in routes have no fallback (owner, 2026-10-25).
    fakes = _fakes(claude=ModelCallError("down"))
    with pytest.raises(ModelCallError):
        await ModelRouter(plain_callers=fakes).complete(_plain("consolidation"))
    assert not fakes["openai_codex"].calls and not fakes["openrouter"].calls


# --- scenario 4: a plain purpose with no route row fails a test --------------

_CALL_NAMES = {"CallRequest", "DecideRequest", "complete_text", "_invoke_cli"}


def _literal_plain_purposes(source: str) -> List[str]:
    """Every literal ``purpose="..."`` passed to a plain-call constructor."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name not in _CALL_NAMES:
            continue
        for kw in node.keywords:
            if (
                kw.arg == "purpose"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
            ):
                found.append(kw.value.value)
    return found


def test_every_plain_purpose_has_exactly_one_route_row():
    from src.model_router.routes import DEFAULT_CALL_ROUTES
    from src.model_router.types import PLAIN_PURPOSES, PURPOSES, TURN_PURPOSES

    assert set(PLAIN_PURPOSES) == NINE_PLAIN
    assert set(DEFAULT_CALL_ROUTES) == set(PLAIN_PURPOSES)
    assert not set(TURN_PURPOSES) & set(PLAIN_PURPOSES)
    assert set(TURN_PURPOSES) | set(PLAIN_PURPOSES) == set(PURPOSES)


def test_a_literal_plain_purpose_in_the_code_must_have_a_route_row():
    from src.model_router.routes import DEFAULT_CALL_ROUTES

    # The scan itself catches a new call with an unrouted purpose.
    probe = 'CallRequest(purpose="brand_new", prompt="p", system_prompt="s")'
    assert _literal_plain_purposes(probe) == ["brand_new"]
    assert "brand_new" not in DEFAULT_CALL_ROUTES

    missing = []
    for root in ("src", "scripts"):
        for path in (REPO / root).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for purpose in _literal_plain_purposes(source):
                if purpose not in DEFAULT_CALL_ROUTES:
                    missing.append(f"{path.relative_to(REPO)}: {purpose}")
    assert not missing, f"plain-call purposes with no route row: {missing}"


async def test_complete_refuses_a_turn_purpose():
    fakes = _fakes()
    with pytest.raises(ValueError):
        await ModelRouter(plain_callers=fakes).complete(_plain("chat"))
    assert not any(f.calls for f in fakes.values())


@pytest.mark.parametrize(
    "body",
    [
        "purposes:\n  brand_new: {models: {claude: haiku}}\n",  # unknown purpose
        "purposes:\n  chat: {models: {claude: haiku}}\n",  # a turn purpose
    ],
)
def test_models_yaml_naming_a_non_plain_purpose_is_rejected(tmp_path, body):
    with pytest.raises(ValueError):
        _routes(tmp_path, body)


# --- scenario 5: bad models.yaml at boot = built-in routes + owner alert -----

_BAD_FILES = {
    "yaml syntax": "default: [harness: claude\n",
    "unknown harness": "default:\n  harness: gemini_cli\n",
    "unknown purpose": "purposes:\n  not_a_purpose: {}\n",
    "codex without a model": (
        "default:\n  harness: openai_codex\n  models: {openai_codex: null}\n"
        "purposes:\n  consolidation: {models: {openai_codex: null}}\n"
    ),
}


@pytest.mark.parametrize("label", sorted(_BAD_FILES))
async def test_bad_models_yaml_at_boot_runs_built_in_routes_and_alerts_owner(
    tmp_path, label
):
    from src.model_router.routes import boot_call_routes, load_call_routes

    path = _yaml(tmp_path, _BAD_FILES[label])
    # The loader itself fails loud (show_config / scripts see the reason) ...
    with pytest.raises(ValueError):
        load_call_routes(path)

    # ... the boot path does not: built-in routes + one alert for the owner.
    routes, alert = boot_call_routes(path)
    assert alert, label
    assert "models.yaml" in alert
    for (purpose, backend), (harness, model) in GOLDEN.items():
        fakes = _fakes()
        await ModelRouter(plain_callers=fakes, call_routes=routes).complete(
            _plain(purpose, backend=backend)
        )
        request = _only_call(fakes)
        assert (request.harness, request.model) == (harness, model), purpose


def test_boot_wires_the_settings_path_and_the_owner_alert():
    """main.py reads ``settings.model_routes_path`` through the boot loader
    and hands the table to the router; the path is a settings field."""
    from src.config.settings import Settings

    field = Settings.model_fields["model_routes_path"]
    default = field.get_default(call_default_factory=True)
    assert default is not None and Path(default).name == "models.yaml"

    main = (REPO / "src" / "main.py").read_text(encoding="utf-8")
    assert "boot_call_routes(" in main
    assert "model_routes_path" in main
    assert "call_routes=" in main


# --- added -------------------------------------------------------------------


def test_absent_models_yaml_at_boot_sends_no_alert(tmp_path):
    from src.model_router.routes import boot_call_routes

    _routes_obj, alert = boot_call_routes(tmp_path / "absent.yaml")
    assert alert is None


async def test_openrouter_route_uses_key_store_key_and_prices_the_row(
    tmp_path, db, monkeypatch
):
    """The bot scrubs secrets from os.environ: the OpenRouter adapter must
    read the /setkey store, and the row is priced from llm_pricing.json."""
    import src.security.keystore as keystore
    from src.model_router import openrouter_plain

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    store = SimpleNamespace(
        env=lambda service: (
            {"OPENROUTER_API_KEY": "sk-or-store"} if service == "openrouter" else {}
        )
    )
    monkeypatch.setattr(keystore, "default_keystore", lambda: store)

    sent: Dict[str, Any] = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> Dict[str, Any]:
            return {
                "model": "z-ai/glm-5.3",
                "choices": [{"message": {"content": "hello"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
            }

    class _Client:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def post(self, url: str, **kw: Any) -> _Resp:
            sent.update(kw)
            return _Resp()

    monkeypatch.setattr(openrouter_plain.httpx, "AsyncClient", _Client)

    router = ModelRouter(
        plain_callers={"openrouter": openrouter_plain.OpenRouterPlainCaller()},
        recorder=ModelCallRepository(db),
        versions=_Versions(),
    )
    result = await router.complete(
        _plain("consolidation", harness="openrouter", model="z-ai/glm-5.3")
    )

    assert result.text == "hello"
    assert sent["headers"]["Authorization"] == "Bearer sk-or-store"
    (row,) = await _rows(db)
    assert row["provider"] == "openrouter" and row["model"] == "z-ai/glm-5.3"
    # glm-5.3: 2.8 in / 8.8 out per 1M tokens.
    assert row["cost_usd"] == pytest.approx(1000 * 2.8e-6 + 100 * 8.8e-6)


# --- dv-9133 phase 2: the design critic runs on Codex only -------------------

_DEFAULT_CLAUDE_WITH_FALLBACK = """
default:
  harness: claude
  models:
    claude: sonnet
    openai_codex: openai-codex/gpt-6-sol
  fallback:
    harness: claude
"""


def test_design_critic_resolves_to_codex_with_no_fallback(tmp_path):
    """Built-in row: Codex, no fallback. An owner's models.yaml whose default
    row is Claude with a Claude fallback moves neither: the critic's point is
    a different model family than the Claude generator."""
    from src.model_router.routes import CallRoutes

    route = CallRoutes().resolve("design_critic")
    assert route.harness == "openai_codex" and route.model == CODEX_MODEL
    assert route.fallback is None

    route = _routes(tmp_path, _DEFAULT_CLAUDE_WITH_FALLBACK).resolve("design_critic")
    assert route.harness == "openai_codex"
    assert route.fallback is None


@pytest.mark.parametrize("with_yaml", [False, True])
async def test_failed_design_critic_call_never_falls_back_to_claude(
    tmp_path, db, with_yaml
):
    fakes = _fakes(openai_codex=ModelCallError("codex down"))
    kwargs: Dict[str, Any] = {}
    if with_yaml:
        kwargs["call_routes"] = _routes(tmp_path, _DEFAULT_CLAUDE_WITH_FALLBACK)
    router = ModelRouter(
        plain_callers=fakes,
        recorder=ModelCallRepository(db),
        versions=_Versions(),
        **kwargs,
    )

    with pytest.raises(ModelCallError, match="codex down"):
        await router.complete(_plain("design_critic"))

    assert len(fakes["openai_codex"].calls) == 1
    assert not fakes["claude"].calls and not fakes["openrouter"].calls
    (row,) = await _rows(db)
    assert row["purpose"] == "design_critic"
    assert row["harness"] == "openai_codex" and row["outcome"] == "error"
