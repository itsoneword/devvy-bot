"""Edit one ``jobs:`` row of ``models.yaml`` in place (dv-6104 phase 4).

The Pump's stage models live in one table since dv-d63d: the ``jobs:`` section
of ``models.yaml``, parsed by :func:`routes.load_call_routes`. Each job process
reads it at launch (:func:`routes.process_call_routes`), so a change applies to
the next stage with no restart. ``/menu → Настройки → Модели``
(``src/bot/orchestration/admin_menu.py``) is this module's only caller; it never
writes the deprecated ``PUMP_*_MODEL`` env or any second store.

* :func:`set_job_model` sets ``jobs.<job>.models.<harness>`` for the row's
  harness as it is *now* (re-read on every call) and refuses a model that is
  not one of :func:`model_choices` for it — a forged button, or a row
  hand-moved to another harness since the screen was drawn;
* :func:`undo_job_model` puts the previous value back (or drops the key the
  change added), refusing when the row was edited since;
* the new file must pass ``load_call_routes`` before it atomically replaces
  the old one; a file that does not parse is never written ("edit by hand").
  No file = one is created with only the changed row (built-ins overlay the
  rest).

PyYAML does not round-trip comments: the leading comment block is kept,
comments further down are not (the data is). Pure and synchronous: the
read-check-write has no ``await`` in it, so two taps cannot interleave.
"""

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .routes import (
    DEFAULT_CODEX_MODEL,
    CallRoutes,
    RouteRow,
    _from_repo,
    _read_yaml,
    default_model_routes_path,
    load_call_routes,
    pricing_rule_for,
)
from .types import CLAUDE_HARNESS, CODEX_HARNESS, JOB_ROUTE_NAMES, OPENROUTER_HARNESS

#: Russian names for the job routes (the owner reads these, not route names).
JOB_LABELS: Dict[str, str] = {
    "pump_analyze": "анализ",
    "pump_architect": "архитектор",
    "pump_testdesign": "тест-дизайн",
    "pump_implement": "сборка",
    "pump_review": "ревью",
    "pump_acceptance": "приёмка",
    "background_job": "фоновые задачи",
    "monitor": "монитор сервера",
    "design_generate": "дизайн-варианты",
}


class RowError(Exception):
    """The change is refused; the file is untouched. ``by_hand`` = the file
    itself cannot be edited safely (does not parse)."""

    def __init__(self, message: str, by_hand: bool = False) -> None:
        super().__init__(message)
        self.by_hand = by_hand


@dataclass(frozen=True)
class Change:
    job: str
    harness: str
    old: Optional[str]
    new: Optional[str]
    #: The file had no ``jobs.<job>.models.<harness>`` key before the change.
    unset: bool
    #: The file did not exist before the change.
    created: bool


def routes_path(configured: Optional[Path] = None) -> Path:
    """The file the job processes read (``process_call_routes``): the bot's
    ``model_routes_path`` setting, else ``MODEL_ROUTES_PATH``, else the
    repo's ``config/models.yaml``."""
    env = os.environ.get("MODEL_ROUTES_PATH")
    return (
        _from_repo(configured)
        or _from_repo(Path(env) if env else None)
        or default_model_routes_path()
    )


def job_label(job: str) -> str:
    return JOB_LABELS.get(job, job)


def load(path: Path) -> CallRoutes:
    """The table as the file is now, without the env or the owner's jobs switch:
    what the row says, which is what the menu edits."""
    try:
        return load_call_routes(path)
    except ValueError as exc:
        raise RowError(str(exc), by_hand=True) from exc


def row_harness(routes: CallRoutes, job: str) -> str:
    """The harness the row runs on (ignoring the owner's jobs switch)."""
    row = routes.jobs.get(job, RouteRow())
    return row.harness or routes.jobs_default.harness or CLAUDE_HARNESS


def row_model(routes: CallRoutes, job: str) -> Optional[str]:
    return routes.resolve_job(job).model


def model_choices(harness: str, routes: CallRoutes) -> Tuple[str, ...]:
    """The models a row on ``harness`` may be set to: what the bot already
    accepts for that program (Claude tiers, the Codex catalog) plus every
    model the table names for that harness. OpenRouter only offers models the
    table names that have a price (``CallRoutes.check`` requires one)."""
    named: List[str] = []
    for row in [routes.jobs_default, *routes.jobs.values()]:
        model = row.models.get(harness)
        if model and model not in named:
            named.append(model)
    if harness == CLAUDE_HARNESS:
        from ..agent_backends.protocol import _CLAUDE_TIERS

        base: List[str] = list(_CLAUDE_TIERS)
    elif harness == CODEX_HARNESS:
        from ..agent_backends.protocol import codex_supported_models

        base = sorted(set(codex_supported_models()) | {DEFAULT_CODEX_MODEL})
    elif harness == OPENROUTER_HARNESS:
        base = []
        named = [m for m in named if pricing_rule_for(m)]
    else:
        return ()
    return tuple(dict.fromkeys([*base, *named]))


def set_job_model(path: Path, job: str, model: str) -> Optional[Change]:
    """Set ``job``'s model for the row's current harness. None = already
    that model (nothing written). Raises :class:`RowError` on a refusal."""
    if job not in JOB_ROUTE_NAMES:
        raise RowError(f"нет такой строки jobs.{job}")
    routes = load(path)
    harness = row_harness(routes, job)
    allowed = model_choices(harness, routes)
    if model not in allowed:
        raise RowError(
            f"{model} не допустима для {harness} (строка jobs.{job}); "
            f"можно: {', '.join(allowed) or '—'}"
        )
    old = row_model(routes, job)
    if old == model:
        return None
    created = not path.exists()
    data = _data(path)
    models = _row(data, job, create=True).setdefault("models", {})
    unset = harness not in models
    models[harness] = model
    _write(path, data)
    return Change(job, harness, old, model, unset, created)


def undo_job_model(path: Path, change: Change) -> Change:
    """Put ``change`` back. Refused when the row moved on since (another
    harness, or a model other than ``change.new``)."""
    routes = load(path)
    harness = row_harness(routes, change.job)
    now = row_model(routes, change.job)
    if harness != change.harness or now != change.new:
        raise RowError(
            f"jobs.{change.job} уже {harness} / {now}, не {change.harness} / "
            f"{change.new} (правили после) — отмену не делаю"
        )
    data = _data(path)
    row = _row(data, change.job, create=True)
    models = row.setdefault("models", {})
    if change.unset:
        models.pop(change.harness, None)
        if not models:
            row.pop("models")
        jobs = data["jobs"]
        if not row:
            jobs.pop(change.job)
        if not jobs:
            data.pop("jobs")
    else:
        models[change.harness] = change.old
    if change.created and not data:
        path.unlink()
    else:
        _write(path, data)
    return Change(change.job, change.harness, change.new, change.old, False, False)


def _data(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return _read_yaml(path)
    except ValueError as exc:
        raise RowError(str(exc), by_hand=True) from exc


def _row(data: Dict[str, Any], job: str, *, create: bool) -> Dict[str, Any]:
    jobs = data.get("jobs")
    if jobs is None:
        jobs = data["jobs"] = {}
    if not isinstance(jobs, dict):
        raise RowError("jobs: не словарь", by_hand=True)
    row = jobs.get(job)
    if row is None:
        row = jobs[job] = {}
    if not isinstance(row, dict):
        raise RowError(f"jobs.{job}: не словарь", by_hand=True)
    if "models" in row and row["models"] is None:
        row["models"] = {}
    if not isinstance(row.get("models", {}), dict):
        raise RowError(f"jobs.{job}.models: не словарь", by_hand=True)
    return row


def _header(path: Path) -> str:
    """The file's leading comment block (kept across a rewrite)."""
    if not path.exists():
        return ""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        if line.lstrip().startswith("#") or not line.strip():
            lines.append(line)
            continue
        break
    return "".join(lines)


def _write(path: Path, data: Dict[str, Any]) -> None:
    """Validate, then replace ``path`` atomically (mode kept)."""
    text = _header(path) + yaml.safe_dump(
        data, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".models.", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        try:
            load_call_routes(Path(tmp))
        except ValueError as exc:
            raise RowError(f"после правки файл не проходит проверку: {exc}") from exc
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
