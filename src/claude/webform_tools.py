"""webform_fill / webform_submit: fill a registered web form on the host (dv-5a25).

The browser cannot run in a sandboxed profile's box (no network, no
Chromium), so the bot runs it: a child process (``src/webforms/runner.py``)
drives headless Chromium through the deterministic filler
(``src/webforms/engine.py``) for one form from ``src.webforms.FORMS``.

Owner turns only, on a profile that lists the form in ``web_forms``: the
mount predicate and every handler check both, so a guest on the same profile
never sees or reaches the tools.

Gating: ``webform_submit`` asks the owner through ``policy.decide``
(``src/security/destructive.py::webform_tool_ask``), on both backends. On top
of that the submit handler refuses unless the same user's last successful
``webform_fill`` of that form had byte-identical data (canonical JSON plus
the upload files' hashes) less than 30 minutes ago: the owner approves what
he saw in the screenshots, nothing changed afterwards.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import structlog

from ..webforms import FORMS
from .tool_context import AgentServices, ToolSpec, TurnContext

logger = structlog.get_logger()

FILL_TTL_SECONDS = 30 * 60
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
UPLOAD_SUFFIXES = frozenset({".pdf", ".jpg", ".jpeg", ".png"})
CHILD_TIMEOUT_SECONDS = 180.0
SUBMIT_TIMEOUT_SECONDS = 240.0
REPO_ROOT = Path(__file__).resolve().parents[2]

Runner = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


class BadArgs(Exception):
    """The call is refused; the message says why."""


class ChildRunner:
    """Runs ``python -m src.webforms.runner`` for one job, with a hard timeout."""

    def __init__(self, browsers_path: Optional[str] = None) -> None:
        self.browsers_path = browsers_path

    async def __call__(self, job: Dict[str, Any]) -> Dict[str, Any]:
        out_dir = Path(job["out_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        job_file = out_dir / "job.json"
        job_file.write_text(json.dumps(job, indent=2), encoding="utf-8")
        # A minimal env: the bot's own env holds no secrets by now (dv-2976),
        # but the browser needs none of it either.
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
        if self.browsers_path:
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(Path(self.browsers_path).expanduser())
        timeout = SUBMIT_TIMEOUT_SECONDS if job.get("submit") else CHILD_TIMEOUT_SECONDS
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "src.webforms.runner",
            str(job_file),
            cwd=str(REPO_ROOT),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            status = "unconfirmed" if job.get("submit") else "failed"
            return {
                "status": status,
                "message": f"browser run timed out after {int(timeout)} s"
                + (
                    "; do not resubmit, check the William Russell email first"
                    if job.get("submit")
                    else ""
                ),
                "screenshots": [],
                "warnings": [],
            }
        lines = [
            ln for ln in stdout.decode(errors="replace").splitlines() if ln.strip()
        ]
        try:
            return dict(json.loads(lines[-1]))
        except (IndexError, ValueError):
            tail = stderr.decode(errors="replace").strip().splitlines()[-3:]
            return {
                "status": "unconfirmed" if job.get("submit") else "failed",
                "message": "browser run gave no result: " + " | ".join(tail),
                "screenshots": [],
                "warnings": [],
            }


class WebformClient:
    """Holds the runner and the per-user record of the last good fill."""

    def __init__(
        self,
        runner: Optional[Runner] = None,
        clock: Callable[[], float] = time.time,
        browsers_path: Optional[str] = None,
    ) -> None:
        self.runner: Runner = runner or ChildRunner(browsers_path)
        self.clock = clock
        self._fills: Dict[Tuple[int, str], Tuple[str, float]] = {}

    def remember(self, user_id: int, form: str, fingerprint: str) -> None:
        self._fills[(user_id, form)] = (fingerprint, self.clock())

    def matches(self, user_id: int, form: str, fingerprint: str) -> bool:
        seen = self._fills.get((user_id, form))
        if not seen:
            return False
        return seen[0] == fingerprint and self.clock() - seen[1] <= FILL_TTL_SECONDS

    def forget(self, user_id: int, form: str) -> None:
        self._fills.pop((user_id, form), None)


# -- checks -------------------------------------------------------------------


def _client(services: AgentServices) -> Optional[WebformClient]:
    client = getattr(services, "webforms", None)
    return client if isinstance(client, WebformClient) else None


def _profile_forms(services: AgentServices, ctx: TurnContext) -> Tuple[str, ...]:
    registry = services.profile_registry
    if not registry:
        return ()
    base = registry.get_by_slug(ctx.profile_slug.split("#", 1)[0])
    return tuple(getattr(base, "web_forms", ()) or ()) if base else ()


def webform_available(services: AgentServices, ctx: TurnContext) -> bool:
    """Owner turns on a profile that lists at least one form in ``web_forms``."""
    return (
        _client(services) is not None
        and ctx.role == "owner"
        and bool(_profile_forms(services, ctx))
    )


def _resolve_files(raw: Any, workspace: Path) -> List[str]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise BadArgs("data.files must be a list of paths")
    root = workspace.resolve()
    out: List[str] = []
    total = 0
    for item in raw:
        path = Path(item).expanduser()
        if not path.is_absolute():
            path = root / path
        try:
            real = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise BadArgs(f"file not found: {item}")
        if not real.is_relative_to(root):
            raise BadArgs(
                f"{item} is outside your workspace; only your own files upload"
            )
        if not real.is_file():
            raise BadArgs(f"{item} is not a file")
        if real.suffix.lower() not in UPLOAD_SUFFIXES:
            raise BadArgs(
                f"{item}: only {', '.join(sorted(UPLOAD_SUFFIXES))} files upload"
            )
        total += real.stat().st_size
        out.append(str(real))
    if total > MAX_UPLOAD_BYTES:
        raise BadArgs(f"uploads total {total} bytes; the form takes at most 10 MB")
    return out


def _prepare(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Tuple[WebformClient, str, Dict[str, Any], str]:
    client = _client(services)
    if client is None or ctx.role != "owner":
        raise BadArgs("web form tools are not available here (owner turns only)")
    if ctx.working_dir is None or ctx.user_id is None:
        raise BadArgs("web form tools need a user workspace")
    args = dict(args or {})
    if isinstance(args.get("arguments"), dict):
        args = dict(args["arguments"])
    form = str(args.get("form") or "").strip()
    if form not in FORMS:
        raise BadArgs(f"unknown form {form!r} (known: {', '.join(sorted(FORMS))})")
    if form not in _profile_forms(services, ctx):
        raise BadArgs(f"form {form} is not enabled on this profile (web_forms)")
    data = args.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            raise BadArgs("data must be a JSON object")
    if not isinstance(data, dict):
        raise BadArgs("data must be an object")
    data = json.loads(json.dumps(data))  # plain JSON values, a private copy
    data["files"] = _resolve_files(data.get("files"), Path(ctx.working_dir))
    return client, form, data, _fingerprint(form, data)


def _fingerprint(form: str, data: Dict[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            {"form": form, "data": data}, sort_keys=True, separators=(",", ":")
        ).encode()
    )
    for path in data.get("files") or []:
        digest.update(b"\0" + path.encode() + b"\0")
        digest.update(hashlib.sha256(Path(path).read_bytes()).digest())
    return digest.hexdigest()


def _out_dir(ctx: TurnContext, form: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return (
        Path(ctx.working_dir or ".")
        / "claims"
        / f"{stamp}-{form}-{uuid.uuid4().hex[:6]}"
    )


def _text(payload: Any, error: bool = False) -> Dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    out: Dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if error:
        out["isError"] = True
    return out


def _report(result: Dict[str, Any], submit: bool) -> Dict[str, Any]:
    status = result.get("status")
    payload = {
        k: result.get(k)
        for k in ("status", "message", "url", "warnings", "failure", "screenshots")
        if result.get(k)
    }
    if status == "filled":
        payload["next"] = (
            "Send the screenshots with send_image_to_user and one summary; call "
            "webform_submit with exactly the same form and data only after the "
            "owner says yes."
        )
    elif status == "unconfirmed":
        payload["next"] = (
            "Do not resubmit. Tell the owner to check the insurer's email."
        )
    elif status == "failed":
        payload["next"] = (
            "Read failure first; open its screenshot only if the text is not enough. "
            "Fix the data and fill again, or report the failure."
        )
    bad = status in ("failed", "unsupported") or (submit and status != "submitted")
    return _text(payload, error=bad and status != "unconfirmed")


async def _run(
    client: WebformClient,
    ctx: TurnContext,
    form: str,
    data: Dict[str, Any],
    submit: bool,
) -> Dict[str, Any]:
    job = {
        "form": form,
        "data": data,
        "submit": submit,
        "out_dir": str(_out_dir(ctx, form)),
    }
    logger.info("webform run", form=form, submit=submit, user_id=ctx.user_id)
    result = await client.runner(job)
    logger.info("webform result", form=form, submit=submit, status=result.get("status"))
    return dict(result or {})


async def _fill(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    try:
        client, form, data, fingerprint = _prepare(services, ctx, args)
    except BadArgs as exc:
        return _text(f"error: {exc}", True)
    result = await _run(client, ctx, form, data, submit=False)
    if result.get("status") == "filled":
        client.remember(int(ctx.user_id or 0), form, fingerprint)
    else:
        client.forget(int(ctx.user_id or 0), form)
    return _report(result, submit=False)


async def _submit(
    services: AgentServices, ctx: TurnContext, args: Dict[str, Any]
) -> Dict[str, Any]:
    try:
        client, form, data, fingerprint = _prepare(services, ctx, args)
    except BadArgs as exc:
        return _text(f"error: {exc}", True)
    user = int(ctx.user_id or 0)
    if not client.matches(user, form, fingerprint):
        return _text(
            "error: no matching webform_fill in the last 30 minutes. Run webform_fill "
            "with this exact data (and unchanged files), show the owner the "
            "screenshots, then submit the same data.",
            True,
        )
    # One submit per approved fill, whatever the outcome: no blind retry.
    client.forget(user, form)
    result = await _run(client, ctx, form, data, submit=True)
    return _report(result, submit=True)


_DATA_DESCRIPTION = (
    "The claim. william_russell_claim keys: claim_type 'medical'; name, address, "
    "dob (dd/mm/yyyy), policy, member, email, phone; previous_claim (optional ref); "
    "symptoms_from (dd/mm/yyyy), symptoms, consult_date (optional), diagnosis, "
    "prior_condition (optional: when), accident (optional: details); bills: 1-6 of "
    "{date, invoice, amount (number), currency (ISO), notes?}; payout: "
    "{previous_account: true, last4, currency?}; other_insurance (optional); files: "
    "invoice/receipt paths in your workspace (pdf/jpg/png, 10 MB total). Personal "
    "details come from this user's own memory; ask the user for gaps."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "form": {"type": "string", "enum": sorted(FORMS)},
        "data": {"type": "object", "description": _DATA_DESCRIPTION},
    },
    "required": ["form", "data"],
}

WEBFORM_SPECS: List[ToolSpec] = [
    ToolSpec(
        name="webform_fill",
        description=(
            "Fill a registered web form (e.g. the William Russell medical claim) in a "
            "headless browser on the host and stop before Submit. Every field is "
            "read back; returns status, a structured failure if any, warnings and "
            "one screenshot per page (saved under claims/ in your workspace). "
            "No model looks at the pages: read `failure` when status is failed."
        ),
        schema=_SCHEMA,
        handler=_fill,
        available=webform_available,
    ),
    ToolSpec(
        name="webform_submit",
        description=(
            "Submit a form filled by webform_fill: refills it with the exact same "
            "data and clicks Submit. Only after the user said yes to the fill "
            "screenshots; the owner also approves a button. Refused unless the "
            "same data was filled successfully in the last 30 minutes. status "
            "'unconfirmed' means: do not resubmit, check the email."
        ),
        schema=_SCHEMA,
        handler=_submit,
        available=webform_available,
    ),
]
