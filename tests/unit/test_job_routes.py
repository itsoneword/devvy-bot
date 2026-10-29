"""Job routes in the one route table (dv-d63d phase 2).

Background jobs (bgjob, Pump stages, the server monitor) resolve harness /
provider / model from a ``jobs:`` section of the same ``models.yaml`` that
routes plain calls. Built-in rows reproduce today's models exactly.

Contract pinned here (names from docs/research/dv-d63d/analysis.md, Phase 2):
- ``load_call_routes(path)`` parses a ``jobs:`` section (``default`` + one row
  per job route name, same row keys as ``purposes:``); absent file = built-ins.
- ``CallRoutes.resolve_job(name, *, model=None)`` returns a job route with
  ``.provider`` and ``.model``. Job route names: ``background_job``,
  ``monitor``, ``pump_analyze``, ``pump_architect``, ``pump_testdesign``,
  ``pump_implement``, ``pump_review``, ``pump_acceptance``.
- For a job, harness ``openrouter`` = the Claude CLI pointed at OpenRouter:
  provider ``openrouter``, model = the row's ``models.openrouter`` slug.
- An explicit Claude tier (``--model sonnet``) applies only when the resolved
  route is Claude on the subscription; otherwise it is ignored with a log line.
- ``process_call_routes()`` (the per-process loader agentjob uses) honours the
  legacy ``PUMP_*_MODEL`` env vars below ``models.yaml``, with a warning.
"""

from pathlib import Path

import pytest
from structlog.testing import capture_logs

REPO = Path(__file__).resolve().parents[2]
SLUG = "z-ai/glm-5.3"

# What every job ran on before dv-d63d: taskpump's ANALYZE/IMPLEMENT/... model
# constants (scripts/taskpump ~202-218), bgjob's and agentjob's --model
# default `sonnet` for bgjob jobs and the server monitor.
TODAY = {
    "background_job": "sonnet",
    "monitor": "sonnet",
    "pump_analyze": "opus",
    "pump_architect": "sonnet",
    "pump_testdesign": "opus",
    "pump_implement": "opus",
    "pump_review": "sonnet",
    "pump_acceptance": "sonnet",
}

PUMP_ENV = (
    "PUMP_ANALYZE_MODEL",
    "PUMP_IMPLEMENT_MODEL",
    "PUMP_TESTDESIGN_MODEL",
    "PUMP_REVIEW_MODEL",
    "PUMP_ARCHITECT_MODEL",
    "PUMP_ACCEPTANCE_MODEL",
)


@pytest.fixture(autouse=True)
def _no_pump_env(monkeypatch):
    for name in PUMP_ENV:
        monkeypatch.delenv(name, raising=False)


def _yaml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _routes(tmp_path: Path, body: str):
    from src.model_router.routes import load_call_routes

    return load_call_routes(_yaml(tmp_path, body))


def _logged(logs, capsys) -> str:
    """Everything a log line could have said: structlog events + stderr."""
    captured = capsys.readouterr()
    return repr(logs) + captured.err + captured.out


OR_REVIEW = f"""
jobs:
  pump_review:
    harness: openrouter
    models:
      openrouter: {SLUG}
"""


# --- scenario 1: no models.yaml = today's models ------------------------------


@pytest.mark.parametrize("name,model", sorted(TODAY.items()))
def test_no_models_yaml_every_job_runs_todays_model(name, model):
    from src.model_router.routes import PROVIDER_ANTHROPIC_SUB, load_call_routes

    route = load_call_routes(None).resolve_job(name)

    assert route.model == model
    assert route.provider == PROVIDER_ANTHROPIC_SUB


def test_models_yaml_without_jobs_section_keeps_todays_job_models(tmp_path):
    """An existing plain-call models.yaml (no jobs:) moves no job."""
    routes = _routes(tmp_path, "default:\n  harness: claude\n")
    assert {n: routes.resolve_job(n).model for n in TODAY} == TODAY


# --- scenario 2: jobs.pump_review on openrouter -------------------------------


def test_pump_review_on_openrouter_resolves_to_the_openrouter_slug(tmp_path):
    from src.model_router.routes import PROVIDER_OPENROUTER

    routes = _routes(tmp_path, OR_REVIEW)

    review = routes.resolve_job("pump_review")
    assert review.provider == PROVIDER_OPENROUTER
    assert review.model == SLUG
    # Only that row moved: the build stage stays on Claude Opus.
    build = routes.resolve_job("pump_implement")
    assert (build.provider, build.model) == ("anthropic_sub", "opus")


# --- scenario 4: explicit Claude tier ignored off Claude ----------------------


def test_explicit_sonnet_is_ignored_and_logged_when_the_job_route_is_openrouter(
    tmp_path, capsys
):
    routes = _routes(
        tmp_path,
        f"""
jobs:
  background_job:
    harness: openrouter
    models:
      openrouter: {SLUG}
""",
    )

    with capture_logs() as logs:
        route = routes.resolve_job("background_job", model="sonnet")

    assert route.provider == "openrouter"
    assert route.model == SLUG
    assert "sonnet" in _logged(logs, capsys)


def test_explicit_model_still_wins_on_a_claude_job_route(tmp_path):
    """The ignore rule is off-Claude only: on Claude the caller's pick wins
    over the row (job_start passing `fable`)."""
    routes = _routes(
        tmp_path,
        """
jobs:
  background_job:
    models:
      claude: haiku
""",
    )
    assert routes.resolve_job("background_job").model == "haiku"
    assert routes.resolve_job("background_job", model="fable").model == "fable"


# --- scenario 5: legacy PUMP_*_MODEL env, below models.yaml, with a warning ---


def test_legacy_pump_model_env_still_applies_with_a_warning(
    tmp_path, monkeypatch, capsys
):
    from src.model_router.routes import process_call_routes

    monkeypatch.setenv("MODEL_ROUTES_PATH", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("PUMP_REVIEW_MODEL", "fable")

    with capture_logs() as logs:
        routes = process_call_routes()

    assert routes.resolve_job("pump_review").model == "fable"
    # Only its own stage.
    assert routes.resolve_job("pump_architect").model == "sonnet"
    assert "PUMP_REVIEW_MODEL" in _logged(logs, capsys)


def test_models_yaml_job_row_beats_the_legacy_pump_model_env(tmp_path, monkeypatch):
    from src.model_router.routes import process_call_routes

    path = _yaml(
        tmp_path,
        """
jobs:
  pump_review:
    models:
      claude: haiku
""",
    )
    monkeypatch.setenv("MODEL_ROUTES_PATH", str(path))
    monkeypatch.setenv("PUMP_REVIEW_MODEL", "fable")

    assert process_call_routes().resolve_job("pump_review").model == "haiku"


# --- added ---------------------------------------------------------------------


def _all_job_routes(routes):
    return {
        n: (routes.resolve_job(n).provider, routes.resolve_job(n).model) for n in TODAY
    }


def test_example_file_jobs_section_is_the_built_in_job_table():
    """added: config/models.example.yaml documents the built-in job rows (no
    drift between the readable copy and the code)."""
    from src.model_router.routes import CallRoutes, load_call_routes

    text = (REPO / "config" / "models.example.yaml").read_text(encoding="utf-8")
    assert "\njobs:" in text, "models.example.yaml has no jobs: section"
    example = load_call_routes(REPO / "config" / "models.example.yaml")
    assert _all_job_routes(example) == _all_job_routes(CallRoutes())


def test_openrouter_job_model_without_a_pricing_rule_is_rejected(tmp_path):
    """added: a paid job route must be priceable; unknown is not zero."""
    # The same row with a priced slug loads (so the rejection below is about
    # the price, not about the jobs: section).
    assert _routes(tmp_path, OR_REVIEW).resolve_job("pump_review").model == SLUG
    with pytest.raises(ValueError, match="no-such-model"):
        _routes(
            tmp_path,
            """
jobs:
  pump_review:
    harness: openrouter
    models:
      openrouter: acme/no-such-model-xyz
""",
        )
