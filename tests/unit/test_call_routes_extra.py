"""Call-route table details beyond the pinned phase-1 scenarios (dv-781f)."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List

import pytest

from src.model_router import CallRequest, CallResult, ModelRouter
from src.model_router.routes import (
    CallRoutes,
    describe_call_routes,
    legacy_route_models,
    load_call_routes,
)
from src.model_router.types import (
    CODEX_HARNESS,
    PLAIN_PURPOSES,
    Choice,
    DecideRequest,
)

REPO = Path(__file__).resolve().parents[2]
CODEX_MODEL = "openai-codex/gpt-6-sol"


class _Fake:
    def __init__(self, text: str = "ok") -> None:
        self.calls: List[CallRequest] = []
        self.text = text

    async def __call__(self, request: CallRequest) -> CallResult:
        self.calls.append(request)
        return CallResult(text=self.text)


def _fakes() -> Dict[str, _Fake]:
    return {h: _Fake() for h in ("claude", "openai_codex", "openrouter")}


def _yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _all_routes(routes: CallRoutes):
    return {
        (p, hint): routes.resolve(p, backend=hint)
        for p in PLAIN_PURPOSES
        for hint in (None, "claude", "openai_codex")
    }


def test_example_file_is_the_built_in_table():
    """config/models.example.yaml documents the built-in rows; loading it
    changes nothing (it is their readable copy)."""
    example = load_call_routes(REPO / "config" / "models.example.yaml")
    assert _all_routes(example) == _all_routes(CallRoutes())


def test_codex_preset_moves_every_purpose_but_the_scorer(tmp_path):
    routes = load_call_routes(
        _yaml(
            tmp_path,
            "default:\n  harness: openai_codex\n"
            f"  models: {{openai_codex: {CODEX_MODEL}}}\n"
            "purposes:\n  extraction: {harness: openai_codex}\n"
            "  screen_extraction: {harness: openai_codex}\n",
        )
    )
    for purpose in PLAIN_PURPOSES - {"scorer"}:
        route = routes.resolve(purpose, backend="claude")
        assert (route.harness, route.model) == (CODEX_HARNESS, CODEX_MODEL), purpose


def test_openrouter_preset_moves_every_purpose(tmp_path):
    routes = load_call_routes(
        _yaml(
            tmp_path,
            "default:\n  harness: openrouter\n  models: {openrouter: z-ai/glm-5.3}\n"
            "purposes:\n  extraction: {harness: openrouter}\n"
            "  screen_extraction: {harness: openrouter}\n",
        )
    )
    # scorer and design_critic (dv-9133) are pinned to a harness of their own.
    for purpose in PLAIN_PURPOSES - {"scorer", "design_critic"}:
        for hint in ("claude", "openai_codex"):
            route = routes.resolve(purpose, backend=hint)
            assert (route.harness, route.model, route.provider) == (
                "openrouter",
                "z-ai/glm-5.3",
                "openrouter",
            ), purpose


def test_openrouter_default_without_a_model_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="no model"):
        load_call_routes(_yaml(tmp_path, "default:\n  harness: openrouter\n"))


def test_legacy_setting_beats_built_in_row_but_not_the_file(tmp_path):
    legacy = legacy_route_models(
        SimpleNamespace(
            evaluator_model="opus",
            evaluator_codex_model=CODEX_MODEL,
            fact_quality_check_model="sonnet",
        )
    )
    built_in = load_call_routes(None, legacy=legacy)
    assert built_in.resolve("extraction", backend="claude").model == "opus"
    assert built_in.resolve("onboarding").model == "opus"
    assert built_in.resolve("fact_quality").model == "sonnet"

    path = _yaml(tmp_path, "purposes:\n  fact_quality: {models: {claude: haiku}}\n")
    assert load_call_routes(path, legacy=legacy).resolve("fact_quality").model == (
        "haiku"
    )


def test_legacy_defaults_change_nothing():
    """The settings' own defaults equal the built-in rows: no pin, so a file
    that moves default.models.claude still moves onboarding."""
    legacy = legacy_route_models(
        SimpleNamespace(
            evaluator_model="sonnet",
            evaluator_codex_model=CODEX_MODEL,
            fact_quality_check_model="haiku",
        )
    )
    assert load_call_routes(None, legacy=legacy).purposes == CallRoutes().purposes


@pytest.mark.parametrize(
    "body, ok",
    [
        ("default: {harness: openai_codex, provider: openai}\n", True),
        ("default: {harness: openai_codex, provider: openrouter}\n", False),
        ("purposes:\n  reconcile: {harness: claude, provider: anthropic_sub}\n", True),
        ("purposes:\n  reconcile: {wat: 1}\n", False),
        ("default: {harness: backend}\n", False),  # backend is per purpose only
        ("bogus: {}\n", False),
    ],
)
def test_row_validation(tmp_path, body, ok):
    path = _yaml(tmp_path, body)
    if ok:
        load_call_routes(path)
    else:
        with pytest.raises(ValueError):
            load_call_routes(path)


def test_purpose_can_opt_out_of_a_default_fallback(tmp_path):
    routes = load_call_routes(
        _yaml(
            tmp_path,
            "default:\n  fallback: {harness: openai_codex}\n"
            "purposes:\n  ask_explain: {fallback: null}\n",
        )
    )
    assert routes.resolve("reconcile").fallback is not None
    assert routes.resolve("reconcile").fallback.model == CODEX_MODEL
    assert routes.resolve("ask_explain").fallback is None
    # An explicit harness pins the call: no fallback either.
    assert routes.resolve("reconcile", harness="claude").fallback is None


def test_relative_path_is_from_the_repo_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    routes = load_call_routes(Path("config/models.example.yaml"))
    assert routes.source.endswith("config/models.example.yaml")


def test_describe_lists_every_purpose():
    lines = describe_call_routes(CallRoutes())
    assert lines[0] == "source: built-in"
    for purpose in PLAIN_PURPOSES:
        assert any(line.startswith(purpose) for line in lines[1:]), purpose


def _show_config():
    spec = importlib.util.spec_from_file_location(
        "show_config", REPO / "scripts" / "show_config.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_show_config_routes_exits_1_on_a_bad_file(tmp_path, capsys):
    show_config = _show_config()
    assert show_config.show_routes(_yaml(tmp_path, "default: {harness: x}\n")) == 1
    assert "models.yaml invalid" in capsys.readouterr().out
    assert show_config.show_routes(tmp_path / "absent.yaml") == 0
    assert "source: built-in" in capsys.readouterr().out


async def test_decide_llm_leg_runs_on_the_purpose_route(tmp_path, monkeypatch):
    """decide's default fallback_model is the purpose's route, so a file that
    moves reconcile to Codex moves its LLM leg too."""
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    fakes = _fakes()
    fakes["openai_codex"].text = json.dumps({"rel": "new"})
    routes = load_call_routes(
        _yaml(tmp_path, "purposes:\n  reconcile: {harness: openai_codex}\n")
    )
    router = ModelRouter(plain_callers=fakes, call_routes=routes)

    result = await router.decide(
        DecideRequest(
            purpose="reconcile",
            state="s",
            questions={"rel": Choice("pick", {"new": None, "duplicate": None})},
        )
    )

    assert result.harness == CODEX_HARNESS and result.answers["rel"].value == "new"
    (call,) = fakes["openai_codex"].calls
    assert call.model == CODEX_MODEL and not fakes["claude"].calls


async def test_run_turn_refuses_a_plain_purpose():
    with pytest.raises(ValueError):
        await ModelRouter(plain_callers=_fakes()).run_turn(
            "claude", purpose="extraction", prompt="x"
        )
