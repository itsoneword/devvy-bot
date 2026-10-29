"""Which harness, provider and model a model call runs on.

Two tables, one module. Agent turns route per profile (dv-d2f6 step 1, below).
Plain calls route per purpose (dv-781f, :class:`CallRoutes` further down):
built-in rows reproduce what each caller hardcoded before, and the owner's
``config/models.yaml`` overlays them. Background jobs (bgjob, Pump stages, the
server monitor) route per job name from the same file's ``jobs:`` section
(dv-d63d, :meth:`CallRoutes.resolve_job`).

Turn routes:

Minimal slice of the route table in docs/analysis/openrouter-per-agent-config.md.
The harness is the program running the loop (always the Claude Code CLI here);
the provider is who serves and bills the model. A profile with no ``provider``
key resolves to the unchanged subscription route, so nothing moves until a
profile opts in.

Precedence for the model: a user's ``/model`` pick, only if it is in the
profile's ``allowed_models`` > the profile's ``model``. A pick outside the list
is ignored, never an error: the stored pick is per user, the allowed list is
per agent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple

import structlog

from .json_contract import JSON_MODES, JSON_NATIVE, JSON_VALIDATE
from .types import (
    CLAUDE_HARNESS,
    CODEX_HARNESS,
    JOB_ROUTE_NAMES,
    OPENROUTER_HARNESS,
    PLAIN_PURPOSES,
    TURN_PURPOSES,
)

if TYPE_CHECKING:
    from src.profiles.registry import ProfileDefinition

logger = structlog.get_logger()

PROVIDER_ANTHROPIC_SUB = "anthropic_sub"
PROVIDER_OPENAI = "openai"
PROVIDER_OPENROUTER = "openrouter"
VALID_PROVIDERS = frozenset({PROVIDER_OPENROUTER})

# OpenRouter's Anthropic Messages-compatible endpoint (their Claude Code guide).
OPENROUTER_ANTHROPIC_BASE_URL = "https://openrouter.ai/api"
OPENROUTER_KEY_SERVICE = "openrouter"
OPENROUTER_KEY_NAME = "OPENROUTER_API_KEY"

_PRICING_PATH = Path(__file__).resolve().parents[2] / "config" / "llm_pricing.json"


@dataclass(frozen=True)
class Route:
    harness: str
    provider: str
    model: Optional[str]

    @property
    def is_third_party(self) -> bool:
        """True when the CLI's own USD figure cannot be trusted."""
        return self.provider != PROVIDER_ANTHROPIC_SUB


def resolve_turn_route(
    profile: Optional["ProfileDefinition"], user_model: Optional[str]
) -> Route:
    """The route one chat turn on ``profile`` runs on.

    ``user_model`` is what the caller would pass today (a Claude tier or None).
    Subscription profiles get it back untouched.
    """
    provider = getattr(profile, "provider", None) if profile is not None else None
    if provider is None:
        return Route(CLAUDE_HARNESS, PROVIDER_ANTHROPIC_SUB, user_model)
    assert profile is not None
    allowed = profile.allowed_models
    if user_model and user_model in allowed:
        return Route(CLAUDE_HARNESS, provider, user_model)
    return Route(CLAUDE_HARNESS, provider, profile.model)


def user_pick_allowed(
    profile: Optional["ProfileDefinition"], pick: Optional[str]
) -> bool:
    """Whether ``/model`` may store ``pick`` while ``profile`` is active.

    A profile without ``allowed_models`` has no say (the backend policy decides).
    """
    if profile is None or not profile.allowed_models:
        return True
    return pick in profile.allowed_models


def openrouter_key() -> Optional[str]:
    """The OpenRouter key from the owner's key store (``/setkey openrouter``).

    The process env is scrubbed of secrets at startup, so the store is the
    source; the env is a fallback for dev runs.
    """
    from src.security.keystore import default_keystore

    store = default_keystore()
    if store is not None:
        value = store.env(OPENROUTER_KEY_SERVICE).get(OPENROUTER_KEY_NAME)
        if value:
            return value
    return os.environ.get(OPENROUTER_KEY_NAME) or None


def provider_env(route: Route, key: Optional[str]) -> Dict[str, str]:
    """Per-turn CLI env that points the Claude Code CLI at ``route``'s provider.

    {} for the subscription. For OpenRouter: base URL, the key as the auth
    token, an explicitly empty API key (so an inherited Anthropic key is not
    sent), and every tier alias mapped to the chosen slug so no side request
    falls back to an Anthropic model name the endpoint cannot serve.
    """
    if route.provider != PROVIDER_OPENROUTER:
        return {}
    if not key:
        raise RuntimeError(
            f"{OPENROUTER_KEY_NAME} is not in the key store: "
            f"/setkey {OPENROUTER_KEY_SERVICE} {OPENROUTER_KEY_NAME} <value>"
        )
    env = {
        "ANTHROPIC_BASE_URL": OPENROUTER_ANTHROPIC_BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": key,
        "ANTHROPIC_API_KEY": "",
    }
    if route.model:
        for tier in ("OPUS", "SONNET", "HAIKU", "FABLE"):
            env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"] = route.model
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = route.model
    return env


def pricing_rule_for(
    model: Optional[str], pricing_path: Path = _PRICING_PATH
) -> Optional[str]:
    """The ``llm_pricing.json`` pattern that prices ``model``; None if only
    the Anthropic default would. The profile loader requires one for every
    provider route, so a cap on that route is never enforced on a guess."""
    from src.llm_analytics.pricing import PricingTable

    if not model:
        return None
    _, rule = PricingTable.load(pricing_path).match(model)
    return None if rule == "default" else rule


def cost_for(
    route: Route,
    *,
    model: Optional[str],
    usage: Mapping[str, Any],
    exact: Optional[float] = None,
    pricing_path: Path = _PRICING_PATH,
) -> Optional[float]:
    """USD for ONE model call on ``route``: the one cost function (dv-d2f6).

    ``exact`` (OpenRouter's ``total_cost`` for the call's generation) wins.
    Otherwise tokens x ``config/llm_pricing.json``: the subscription prices
    the call's own ``model`` at Anthropic list price (what the CLI's
    ``total_cost_usd`` does, 5m/1h cache writes split); a third-party route
    prices ``route.model`` at its conservative rule (max over the provider's
    endpoints, ``scripts/gen_openrouter_pricing.py``). None = no rule for a
    third-party model: unknown is not zero.

    ``usage`` is an Anthropic usage block (a stream-json ``assistant``
    event's ``message.usage``, or a ResultMessage's).
    """
    from src.llm_analytics.pricing import PricingTable

    if exact is not None:
        return float(exact)
    price_model = route.model if route.is_third_party else model
    table = PricingTable.load(pricing_path)
    rates, rule = table.match(price_model or "")
    if rule == "default" and route.is_third_party:
        return None
    split = usage.get("cache_creation") or {}
    return table.cost(
        rates,
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
        cache_read_tokens=_int(usage.get("cache_read_input_tokens")),
        cache_5m_tokens=_int(split.get("ephemeral_5m_input_tokens")),
        cache_1h_tokens=_int(split.get("ephemeral_1h_input_tokens")),
        cache_creation_tokens=_int(usage.get("cache_creation_input_tokens")),
    )


def cost_from_tokens(
    model: Optional[str],
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    pricing_path: Path = _PRICING_PATH,
) -> Optional[float]:
    """Turn-level token sums priced on a third-party ``model``'s rule.

    Thin wrapper over :func:`cost_for` for a turn whose per-call events were
    not seen; None when no rule matches the model.
    """
    if not model:
        return None
    return cost_for(
        Route(CLAUDE_HARNESS, PROVIDER_OPENROUTER, model),
        model=model,
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read_tokens,
            "cache_creation_input_tokens": cache_creation_tokens,
        },
        pricing_path=pricing_path,
    )


# --- plain-call routes, per purpose (dv-781f) --------------------------------

# Who serves a plain call is fixed by the harness that makes it: the Claude
# CLI on the subscription, the Codex CLI on the OpenAI account, the HTTP
# adapter on OpenRouter. The file may name ``provider`` for readability only.
CALL_HARNESS_PROVIDERS: Mapping[str, str] = {
    CLAUDE_HARNESS: PROVIDER_ANTHROPIC_SUB,
    CODEX_HARNESS: PROVIDER_OPENAI,
    OPENROUTER_HARNESS: PROVIDER_OPENROUTER,
}
CALL_HARNESSES = frozenset(CALL_HARNESS_PROVIDERS)
# A purpose row's harness: run on the caller's ``backend`` hint (the chat's
# agent backend), Claude when it gives none.
FOLLOW_BACKEND = "backend"

DEFAULT_CODEX_MODEL = "openai-codex/gpt-6-sol"


@dataclass(frozen=True)
class CallRoute:
    """Where one plain call runs. ``fallback`` runs once if this one fails.

    ``json``: how a schema call is made here (``json_contract``): ``native``
    sends the schema to the adapter, ``validate`` keeps prompt-only JSON.
    """

    harness: str
    model: Optional[str]
    fallback: Optional["CallRoute"] = None
    json: str = JSON_NATIVE

    @property
    def provider(self) -> str:
        return CALL_HARNESS_PROVIDERS[self.harness]


@dataclass(frozen=True)
class FallbackSpec:
    """A partial route: harness, and a model or None (= resolved as usual)."""

    harness: str
    model: Optional[str] = None


@dataclass(frozen=True)
class RouteRow:
    """One row of the table: the default row or a purpose's.

    ``harness`` None = the default row's. ``models`` maps harness -> model;
    a harness missing here falls through to the default row (None = unset).
    ``fallback`` None = the default row's; :data:`NO_FALLBACK` = none.
    ``json`` maps harness -> ``native`` | ``validate``, falling through the
    same way (unset = native).
    """

    harness: Optional[str] = None
    models: Mapping[str, Optional[str]] = field(default_factory=dict)
    fallback: Optional[FallbackSpec] = None
    json: Mapping[str, str] = field(default_factory=dict)


NO_FALLBACK = FallbackSpec(harness="")

# The built-in default row: Claude on the subscription; Sonnet is what an
# unconfigured plain call (onboarding) ran on. No fallback (owner 2026-10-25).
DEFAULT_ROUTE = RouteRow(
    harness=CLAUDE_HARNESS,
    models={CLAUDE_HARNESS: "sonnet", CODEX_HARNESS: DEFAULT_CODEX_MODEL},
)

# One row per plain purpose: exactly what each caller hardcoded before
# dv-781f, so a host with no models.yaml behaves as before.
DEFAULT_CALL_ROUTES: Mapping[str, RouteRow] = {
    # evaluator/service.py: extraction runs on the chat's backend; on Claude
    # it stays prompt-only JSON, as before (no output_format).
    "extraction": RouteRow(
        harness=FOLLOW_BACKEND, json={CLAUDE_HARNESS: JSON_VALIDATE}
    ),
    "screen_extraction": RouteRow(
        harness=FOLLOW_BACKEND, json={CLAUDE_HARNESS: JSON_VALIDATE}
    ),
    # Reconciler + the reconcile gate's decide leg: haiku (owner D4).
    "reconcile": RouteRow(models={CLAUDE_HARNESS: "haiku"}),
    "consolidation": RouteRow(models={CLAUDE_HARNESS: "haiku"}),
    "fact_quality": RouteRow(models={CLAUDE_HARNESS: "haiku"}),
    "onboarding": RouteRow(),
    # Sonnet: Haiku judging safety is doubtful (owner, dv-781f).
    "ask_explain": RouteRow(models={CLAUDE_HARNESS: "sonnet"}),
    # The answer scorer: a different family than the bot's. No openrouter
    # model = the judge picks one from the live catalogue (judges.py).
    "scorer": RouteRow(
        harness=OPENROUTER_HARNESS,
        models={OPENROUTER_HARNESS: None, CLAUDE_HARNESS: "opus"},
    ),
    # The screen's decide leg (off today: fallback_model=None).
    "screen": RouteRow(models={CLAUDE_HARNESS: "haiku"}),
    # The design critic (dv-9133): a different family than the Claude
    # generator, so Codex only. A Claude fallback would quietly defeat it.
    "design_critic": RouteRow(harness=CODEX_HARNESS, fallback=NO_FALLBACK),
}

# A route may resolve to no model only where its caller picks one itself.
_MODEL_OPTIONAL = frozenset({("scorer", OPENROUTER_HARNESS)})

_ROW_KEYS = frozenset({"harness", "provider", "models", "fallback", "json"})

# --- job routes (dv-d63d) -----------------------------------------------------
#
# A job is an agent turn with tools, so its harness names the program that runs
# the loop and who serves it: ``claude`` = the Claude CLI on the subscription,
# ``openrouter`` = the same CLI pointed at OpenRouter (``provider_env``),
# ``openai_codex`` = the Codex CLI. Jobs never fall back to another harness
# (owner 2026-10-26): a failed provider is reported, not hidden.

JOB_HARNESS_ROUTES: Mapping[str, Tuple[str, str]] = {
    CLAUDE_HARNESS: (CLAUDE_HARNESS, PROVIDER_ANTHROPIC_SUB),
    OPENROUTER_HARNESS: (CLAUDE_HARNESS, PROVIDER_OPENROUTER),
    CODEX_HARNESS: (CODEX_HARNESS, PROVIDER_OPENAI),
}

# The jobs' default row: Sonnet, what bgjob / agentjob passed by default.
DEFAULT_JOB_ROUTE = RouteRow(
    harness=CLAUDE_HARNESS,
    models={CLAUDE_HARNESS: "sonnet", CODEX_HARNESS: DEFAULT_CODEX_MODEL},
)

# One row per job: exactly the model each ran on before dv-d63d (taskpump's
# ANALYZE_MODEL / IMPLEMENT_MODEL / ... constants), so a host with no
# models.yaml behaves as before.
DEFAULT_JOB_ROUTES: Mapping[str, RouteRow] = {
    "background_job": RouteRow(),
    "monitor": RouteRow(),
    "pump_analyze": RouteRow(models={CLAUDE_HARNESS: "opus"}),
    "pump_architect": RouteRow(models={CLAUDE_HARNESS: "sonnet"}),
    "pump_testdesign": RouteRow(models={CLAUDE_HARNESS: "opus"}),
    "pump_implement": RouteRow(models={CLAUDE_HARNESS: "opus"}),
    # The review is a reading job (owner answer dv-a438 2.2).
    "pump_review": RouteRow(models={CLAUDE_HARNESS: "sonnet"}),
    "pump_acceptance": RouteRow(models={CLAUDE_HARNESS: "sonnet"}),
    # dv-9133: the design loop's variant generator. Claude, so the Codex
    # critic is another family; not opus (owner 27.10 on 3.2: generation can
    # run on a cheaper model, the brief and the critic carry the quality).
    "design_generate": RouteRow(models={CLAUDE_HARNESS: "sonnet"}),
}


@dataclass(frozen=True)
class CallRoutes:
    """The resolved table: built-in rows overlaid by ``models.yaml``."""

    default: RouteRow = DEFAULT_ROUTE
    purposes: Mapping[str, RouteRow] = field(
        default_factory=lambda: dict(DEFAULT_CALL_ROUTES)
    )
    source: str = "built-in"
    jobs_default: RouteRow = DEFAULT_JOB_ROUTE
    jobs: Mapping[str, RouteRow] = field(
        default_factory=lambda: dict(DEFAULT_JOB_ROUTES)
    )
    # The owner's jobs switch (/menu → 🧠 Модели, quota.job_route_override): every job on this
    # harness, above the rows. Set by process_call_routes, never by a file.
    job_switch: Optional[str] = None

    def resolve_job(self, name: str, *, model: Optional[str] = None) -> Route:
        """The route one background job runs on (dv-d63d).

        The harness: the owner's switch (``job_switch``) > the job row > the
        jobs default row. Precedence for the model: the caller's explicit
        ``model`` > the job row > the jobs default row (for the harness in
        force). The explicit model is a Claude pick
        (``bgjob --model``, ``job_start``): it applies only on the Claude
        subscription; on any other route it is ignored with a log line, so
        ``sonnet`` is never sent to OpenRouter or Codex.
        """
        if name not in JOB_ROUTE_NAMES:
            raise ValueError(
                f"unknown job route {name!r}; one of {sorted(JOB_ROUTE_NAMES)}"
            )
        row = self.jobs.get(name, RouteRow())
        harness = (
            self.job_switch
            or row.harness
            or self.jobs_default.harness
            or CLAUDE_HARNESS
        )
        program, provider = JOB_HARNESS_ROUTES[harness]
        if harness in row.models:
            row_model = row.models[harness]
        else:
            row_model = self.jobs_default.models.get(harness)
        if model and provider != PROVIDER_ANTHROPIC_SUB:
            logger.warning(
                "explicit job model ignored: the job route is not Claude",
                job_route=name,
                model=model,
                harness=harness,
                route_model=row_model,
            )
            model = None
        return Route(program, provider, model or row_model)

    def resolve(
        self,
        purpose: str,
        *,
        harness: Optional[str] = None,
        model: Optional[str] = None,
        backend: Optional[str] = None,
    ) -> CallRoute:
        """The route for one call. Precedence: the caller's explicit
        ``harness`` / ``model`` > the purpose row > the default row.

        An explicit harness pins the call: no fallback to another one.
        """
        if purpose not in PLAIN_PURPOSES:
            raise ValueError(f"not a plain-call purpose: {purpose!r}")
        row = self.purposes.get(purpose, RouteRow())
        chosen = harness or row.harness or self.default.harness or CLAUDE_HARNESS
        if chosen == FOLLOW_BACKEND:
            chosen = backend if backend in CALL_HARNESSES else CLAUDE_HARNESS
        if chosen not in CALL_HARNESSES:
            raise ValueError(f"no plain-call harness {chosen!r}")
        fallback = None
        if harness is None:
            spec = row.fallback or self.default.fallback
            if spec is not None and spec is not NO_FALLBACK and spec.harness:
                fallback = CallRoute(
                    spec.harness,
                    spec.model or self._model(row, spec.harness),
                    json=self._json(row, spec.harness),
                )
        return CallRoute(
            chosen,
            model or self._model(row, chosen),
            fallback,
            json=self._json(row, chosen),
        )

    def _model(self, row: RouteRow, harness: str) -> Optional[str]:
        if harness in row.models:
            return row.models[harness]
        return self.default.models.get(harness)

    def _json(self, row: RouteRow, harness: str) -> str:
        return row.json.get(harness) or self.default.json.get(harness) or JSON_NATIVE

    def check(self) -> None:
        """Every purpose resolves to a model its adapter can run (fail loud)."""
        for purpose in sorted(PLAIN_PURPOSES):
            row = self.purposes.get(purpose, RouteRow())
            hints: Tuple[Optional[str], ...] = (None,)
            if (row.harness or self.default.harness) == FOLLOW_BACKEND:
                hints = (CLAUDE_HARNESS, CODEX_HARNESS)
            for hint in hints:
                route = self.resolve(purpose, backend=hint)
                for leg in (route, route.fallback):
                    if leg is None:
                        continue
                    if leg.model is None and (purpose, leg.harness) not in (
                        _MODEL_OPTIONAL
                    ):
                        raise ValueError(
                            f"purpose {purpose!r} resolves to {leg.harness} "
                            "with no model: set default.models."
                            f"{leg.harness} or purposes.{purpose}.models."
                            f"{leg.harness}"
                        )
        for name in sorted(JOB_ROUTE_NAMES):
            job = self.resolve_job(name)
            harness = self.jobs.get(name, RouteRow()).harness or (
                self.jobs_default.harness or CLAUDE_HARNESS
            )
            if not job.model:
                raise ValueError(
                    f"job {name!r} resolves to {harness} with no model: set "
                    f"jobs.default.models.{harness} or jobs.{name}.models."
                    f"{harness}"
                )
            # A paid route must be priceable: unknown is not zero.
            if job.provider == PROVIDER_OPENROUTER and not pricing_rule_for(job.model):
                raise ValueError(
                    f"jobs.{name}: {job.model!r} has no rule in "
                    "config/llm_pricing.json (scripts/gen_openrouter_pricing.py)"
                )


_REPO_ROOT = Path(__file__).resolve().parents[2]


def default_model_routes_path() -> Path:
    """Where ``models.yaml`` lives when no setting says otherwise."""
    return _REPO_ROOT / "config" / "models.yaml"


def _from_repo(path: Optional[Path]) -> Optional[Path]:
    """A relative ``MODEL_ROUTES_PATH`` is from the repo root, not the cwd."""
    if path is None:
        return None
    path = Path(path).expanduser()
    return path if path.is_absolute() else _REPO_ROOT / path


def process_call_routes() -> CallRoutes:
    """The table for a process without the bot's settings (scripts, the
    default router): ``MODEL_ROUTES_PATH`` or the repo's file. A bad file
    logs and gives the built-in rows. The owner's job switch (/menu → 🧠 Модели,
    ``quota.job_route_override``) is read here, so every job process sees the
    switch as it is at its launch."""
    env_path = os.environ.get("MODEL_ROUTES_PATH")
    path = Path(env_path) if env_path else default_model_routes_path()
    routes = boot_call_routes(path, legacy_jobs=legacy_job_models(os.environ))[0]
    return with_job_switch(routes)


def with_job_switch(routes: CallRoutes) -> CallRoutes:
    """``routes`` with the owner's job switch (/menu → 🧠 Модели) applied (dv-d63d)."""
    from dataclasses import replace

    from .quota import job_route_override

    switch = job_route_override()
    return replace(routes, job_switch=switch) if switch else routes


# taskpump's per-stage model env vars before dv-d63d. Testdesign followed the
# implement model unless set itself.
_LEGACY_JOB_ENV: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("pump_analyze", ("PUMP_ANALYZE_MODEL",)),
    ("pump_architect", ("PUMP_ARCHITECT_MODEL",)),
    ("pump_testdesign", ("PUMP_TESTDESIGN_MODEL", "PUMP_IMPLEMENT_MODEL")),
    ("pump_implement", ("PUMP_IMPLEMENT_MODEL",)),
    ("pump_review", ("PUMP_REVIEW_MODEL",)),
    ("pump_acceptance", ("PUMP_ACCEPTANCE_MODEL",)),
)
LEGACY_JOB_ENV_NAMES = tuple(
    sorted({name for _, names in _LEGACY_JOB_ENV for name in names})
)


def legacy_job_models(env: Mapping[str, str]) -> Dict[str, Tuple[str, str]]:
    """The deprecated ``PUMP_*_MODEL`` env as job route -> (env var, model).

    Kept for installs that set them; ``load_call_routes`` ranks them below
    ``models.yaml`` and warns when one differs from the built-in row.
    """
    found: Dict[str, Tuple[str, str]] = {}
    for job, names in _LEGACY_JOB_ENV:
        for var in names:
            value = (env.get(var) or "").strip()
            if value:
                found[job] = (var, value)
                break
    return found


def load_call_routes(
    path: Optional[Path],
    *,
    legacy: Optional[Mapping[Tuple[str, str], Optional[str]]] = None,
    legacy_jobs: Optional[Mapping[str, Tuple[str, str]]] = None,
) -> CallRoutes:
    """The route table: built-in rows, ``legacy`` settings, then ``path``.

    An absent file (or None) = the built-in rows. A bad file raises
    ``ValueError`` naming the problem. ``legacy`` maps (purpose, harness) to
    the deprecated scalar settings (``EVALUATOR_MODEL``, ...); they rank below
    the file, so ``models.yaml`` is the one place to look. ``legacy_jobs``
    does the same for the Pump's ``PUMP_*_MODEL`` env
    (:func:`legacy_job_models`): Claude models of job rows.
    """
    purposes: Dict[str, RouteRow] = dict(DEFAULT_CALL_ROUTES)
    for (purpose, harness), model in (legacy or {}).items():
        if not model or _row_model(purposes[purpose], harness) == model:
            continue
        logger.warning(
            "deprecated model setting; move it to models.yaml",
            purpose=purpose,
            harness=harness,
            model=model,
        )
        row = purposes[purpose]
        purposes[purpose] = RouteRow(
            row.harness, {**row.models, harness: model}, row.fallback, row.json
        )
    jobs: Dict[str, RouteRow] = dict(DEFAULT_JOB_ROUTES)
    for job, (var, model) in (legacy_jobs or {}).items():
        row = jobs[job]
        if _row_model(row, CLAUDE_HARNESS, DEFAULT_JOB_ROUTE) == model:
            continue
        logger.warning(
            f"deprecated {var}; move it to models.yaml jobs.{job}.models.claude",
            env=var,
            job_route=job,
            model=model,
        )
        jobs[job] = RouteRow(
            row.harness, {**row.models, CLAUDE_HARNESS: model}, row.fallback, row.json
        )
    jobs_default = DEFAULT_JOB_ROUTE
    default = DEFAULT_ROUTE
    source = "built-in"
    path = _from_repo(path)
    if path is not None and path.exists():
        data = _read_yaml(path)
        unknown = set(data) - {"default", "purposes", "jobs"}
        if unknown:
            raise ValueError(f"unknown top-level key(s): {sorted(unknown)}")
        default = _overlay(default, data.get("default"), "default", allow_follow=False)
        rows = data.get("purposes") or {}
        if not isinstance(rows, dict):
            raise ValueError("purposes: must be a mapping")
        for purpose, raw in rows.items():
            if purpose in TURN_PURPOSES:
                raise ValueError(
                    f"purposes.{purpose}: an agent turn; it routes per profile "
                    "(profiles.yaml), not here"
                )
            if purpose not in PLAIN_PURPOSES:
                raise ValueError(
                    f"purposes.{purpose}: unknown purpose; plain purposes are "
                    f"{sorted(PLAIN_PURPOSES)}"
                )
            purposes[purpose] = _overlay(
                purposes[purpose], raw, f"purposes.{purpose}", allow_follow=True
            )
        job_rows = data.get("jobs") or {}
        if not isinstance(job_rows, dict):
            raise ValueError("jobs: must be a mapping")
        for job, raw in job_rows.items():
            where = f"jobs.{job}"
            if isinstance(raw, dict) and "fallback" in raw:
                raise ValueError(
                    f"{where}.fallback: jobs never fall back to another "
                    "harness; a failed job is reported (owner 2026-10-26)"
                )
            if job == "default":
                jobs_default = _overlay(jobs_default, raw, where, allow_follow=False)
                continue
            if job not in JOB_ROUTE_NAMES:
                raise ValueError(
                    f"{where}: unknown job route; job routes are "
                    f"{sorted(JOB_ROUTE_NAMES)}"
                )
            jobs[job] = _overlay(jobs[job], raw, where, allow_follow=False)
        source = str(path)
    routes = CallRoutes(
        default=default,
        purposes=purposes,
        source=source,
        jobs_default=jobs_default,
        jobs=jobs,
    )
    routes.check()
    return routes


def boot_call_routes(
    path: Optional[Path],
    *,
    legacy: Optional[Mapping[Tuple[str, str], Optional[str]]] = None,
    legacy_jobs: Optional[Mapping[str, Tuple[str, str]]] = None,
) -> Tuple[CallRoutes, Optional[str]]:
    """The bot's boot path: a bad file does not stop the bot.

    Returns the table and an alert for the owner (None when all is well).
    On a bad file the bot runs on the built-in rows (plus ``legacy``): a bot
    that refuses to boot after a phone edit can only be fixed over SSH.
    """
    try:
        return load_call_routes(path, legacy=legacy, legacy_jobs=legacy_jobs), None
    except ValueError as exc:
        logger.error(
            "models.yaml invalid; built-in routes", path=str(path), error=str(exc)
        )
        return (
            load_call_routes(None, legacy=legacy, legacy_jobs=legacy_jobs),
            f"⚠️ models.yaml invalid: {str(exc)[:300]}\n"
            "Running on the built-in model routes. Fix the file and /restart "
            "(check it first: scripts/show_config.py routes).",
        )


def legacy_route_models(settings: Any) -> Dict[Tuple[str, str], Optional[str]]:
    """The deprecated scalar model settings as (purpose, harness) -> model.

    Kept for installs that set them; ``load_call_routes`` ranks them below
    ``models.yaml`` and warns when one differs from the built-in row.
    """
    claude = getattr(settings, "evaluator_model", None)
    codex = getattr(settings, "evaluator_codex_model", None)
    return {
        ("extraction", CLAUDE_HARNESS): claude,
        ("screen_extraction", CLAUDE_HARNESS): claude,
        ("onboarding", CLAUDE_HARNESS): claude,
        ("extraction", CODEX_HARNESS): codex,
        ("screen_extraction", CODEX_HARNESS): codex,
        ("fact_quality", CLAUDE_HARNESS): getattr(
            settings, "fact_quality_check_model", None
        ),
    }


def _row_model(
    row: RouteRow, harness: str, default: RouteRow = DEFAULT_ROUTE
) -> Optional[str]:
    if harness in row.models:
        return row.models[harness]
    return default.models.get(harness)


def _read_yaml(path: Path) -> Dict[str, Any]:
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"{path.name}: cannot parse: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: top level must be a mapping")
    return data


def _harness(value: Any, where: str, *, allow_follow: bool) -> str:
    allowed = set(CALL_HARNESSES) | ({FOLLOW_BACKEND} if allow_follow else set())
    if value not in allowed:
        raise ValueError(
            f"{where}: unknown harness {value!r}; one of {sorted(allowed)}"
        )
    return str(value)


def _overlay(base: RouteRow, raw: Any, where: str, *, allow_follow: bool) -> RouteRow:
    """``base`` with the file's row on top (a key the row omits stays)."""
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: must be a mapping")
    unknown = set(raw) - _ROW_KEYS
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {sorted(unknown)}")
    harness = base.harness
    if raw.get("harness") is not None:
        harness = _harness(
            raw["harness"], f"{where}.harness", allow_follow=allow_follow
        )
    provider = raw.get("provider")
    if provider is not None:
        expected = CALL_HARNESS_PROVIDERS.get(harness or "")
        if provider != expected:
            raise ValueError(
                f"{where}.provider: {provider!r} does not match harness "
                f"{harness!r} (served by {expected!r})"
            )
    models = dict(base.models)
    raw_models = raw.get("models") or {}
    if not isinstance(raw_models, dict):
        raise ValueError(f"{where}.models: must be a mapping of harness: model")
    for key, value in raw_models.items():
        _harness(key, f"{where}.models", allow_follow=False)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{where}.models.{key}: must be a model name")
        models[key] = value or None
    fallback = base.fallback
    if "fallback" in raw:
        fallback = _fallback(raw["fallback"], f"{where}.fallback")
    json_modes = dict(base.json)
    raw_json = raw.get("json") or {}
    if not isinstance(raw_json, dict):
        raise ValueError(f"{where}.json: must be a mapping of harness: mode")
    for key, value in raw_json.items():
        _harness(key, f"{where}.json", allow_follow=False)
        if value not in JSON_MODES:
            raise ValueError(
                f"{where}.json.{key}: {value!r}; one of {sorted(JSON_MODES)}"
            )
        json_modes[key] = str(value)
    return RouteRow(harness, models, fallback, json_modes)


def _fallback(raw: Any, where: str) -> FallbackSpec:
    if raw is None or raw is False:
        return NO_FALLBACK
    if not isinstance(raw, dict) or set(raw) - {"harness", "model"}:
        raise ValueError(f"{where}: must be {{harness: ..., model: ...}}")
    harness = _harness(raw.get("harness"), f"{where}.harness", allow_follow=False)
    model = raw.get("model")
    if model is not None and not isinstance(model, str):
        raise ValueError(f"{where}.model: must be a model name")
    return FallbackSpec(harness, model or None)


def describe_call_routes(routes: CallRoutes) -> List[str]:
    """One line per purpose (and backend hint) for ``show_config.py routes``."""
    lines = [f"source: {routes.source}"]
    for purpose in sorted(PLAIN_PURPOSES):
        row = routes.purposes.get(purpose, RouteRow())
        hints: Tuple[Optional[str], ...] = (None,)
        if (row.harness or routes.default.harness) == FOLLOW_BACKEND:
            hints = (CLAUDE_HARNESS, CODEX_HARNESS)
        for hint in hints:
            route = routes.resolve(purpose, backend=hint)
            label = purpose + (f" [backend={hint}]" if hint else "")
            line = f"{label}: {route.harness} / {route.model or '(caller picks)'}"
            if route.json != JSON_NATIVE:
                line += f"  json={route.json}"
            if route.fallback is not None:
                line += f"  fallback {route.fallback.harness} / {route.fallback.model}"
            lines.append(line)
    if routes.job_switch:
        lines.append(
            f"jobs switch: {routes.job_switch} "
            "(/menu → 🧠 Модели → Фоновые задачи на → По таблице to undo)"
        )
    for name in sorted(JOB_ROUTE_NAMES):
        job = routes.resolve_job(name)
        lines.append(f"job {name}: {job.harness} / {job.provider} / {job.model}")
    return lines


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = [
    "CallRoute",
    "CallRoutes",
    "DEFAULT_CALL_ROUTES",
    "DEFAULT_JOB_ROUTE",
    "DEFAULT_JOB_ROUTES",
    "JOB_HARNESS_ROUTES",
    "LEGACY_JOB_ENV_NAMES",
    "DEFAULT_ROUTE",
    "FOLLOW_BACKEND",
    "RouteRow",
    "boot_call_routes",
    "default_model_routes_path",
    "process_call_routes",
    "describe_call_routes",
    "legacy_job_models",
    "legacy_route_models",
    "load_call_routes",
    "OPENROUTER_ANTHROPIC_BASE_URL",
    "PROVIDER_ANTHROPIC_SUB",
    "PROVIDER_OPENROUTER",
    "Route",
    "VALID_PROVIDERS",
    "cost_for",
    "cost_from_tokens",
    "openrouter_key",
    "pricing_rule_for",
    "provider_env",
    "resolve_turn_route",
    "user_pick_allowed",
    "with_job_switch",
]
