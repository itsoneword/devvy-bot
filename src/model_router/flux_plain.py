"""Image calls to the free FLUX server (dv-75f1): text prompt in, PNG out.

The design loop's mockups (purpose ``design_mockup``) run here. Token and
base URL come only from key-store service ``flux`` (``FLUX_API_TOKEN``,
``FLUX_API_URL``), read at call time: the bot's env is scrubbed of secrets
and the server is private, so its URL is never a literal in the repo.

The server sits behind Cloudflare, which refuses non-curl clients (error
1010), so the request carries a curl User-Agent. It answers raw PNG bytes or
JSON ``{"images": ["data:image/png;base64,..."]}``; both are handled. The
call is free (cost 0); preset
``best`` takes ~150 s per image, so the timeout is the caller's (minutes).

Since 13.11.26 the server queues: ``POST /v1/generate`` answers ``303`` with
``Location: jobs/<id>/result``; the client polls the short status JSON
``jobs/<id>`` and then GETs ``images[0]`` (the long-polled ``/result`` hit
Cloudflare 502s and the paid fallback).

15.11.26 (dv-c237): the server queues jobs while the GPU worker is offline,
so each call first asks ``/health`` whether the generator was seen lately
(:func:`flux_health`), and an error after the server took a job carries its
id (``ModelCallError.job_id``); ``CallRequest.image_job_id`` fetches it later.

17.11.26 (dv-1c6a): reference images (``CallRequest.images``) are sent only
to a server whose ``/openapi.json`` ``GenRequest`` has a ``ref_images``
property (:func:`flux_takes_images`), as base64 data URLs. The server ignores
unknown fields, so a blind send would draw text-only pictures that look like
success; without the field the call raises before any generate request.
Request text for the server's owner:
``docs/research/flux-ref-images-server-request.md``.
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any, Dict, Optional, Tuple

import httpx

from .routes import (
    FLUX_KEY_SERVICE,
    FLUX_TOKEN_NAME,
    FLUX_URL_NAME,
)
from .routes import flux_credentials as _store_credentials
from .types import (
    FLUX_HARNESS,
    CallRequest,
    CallResult,
    ModelCallError,
    ModelCallTimeout,
    read_images,
)

GENERATE_PATH = "/v1/generate"
JOBS_PATH = "/v1/jobs"
HEALTH_PATH = "/health"
OPENAPI_PATH = "/openapi.json"
# dv-1c6a: the GenRequest field a server with image input advertises.
REF_IMAGES_FIELD = "ref_images"
# A "no image input" answer is asked again after this long; a "yes" is kept.
CAPABILITY_RECHECK_SEC = 600.0
NO_REF_INPUT = (
    "the FLUX server has no reference-image input (no "
    f"{REF_IMAGES_FIELD} in its /openapi.json GenRequest)"
)
# dv-c237: the generator counts as offline when /health last saw it longer ago.
GENERATOR_STALE_SEC = 120
HEALTH_TIMEOUT = 15.0
HEALTH_RECHECK_SEC = 60.0
USER_AGENT = "curl/8.5.0"
GATEWAY = (502, 503, 504, 524)
PRESETS = ("best", "quality", "fast")
DEFAULT_PRESET = "best"
DEFAULT_SIZE = (768, 1024)


def flux_credentials() -> Optional[Tuple[str, str]]:
    """(token, base URL) from key-store service ``flux``, else None."""
    return _store_credentials()


class FluxPlainCaller:
    """Runs one ``image_output`` :class:`CallRequest` on the FLUX server."""

    async def __call__(self, request: CallRequest) -> CallResult:
        if not request.image_output:
            raise ModelCallError(
                f"flux draws only: purpose {request.purpose} asked for text"
            )
        creds = flux_credentials()
        if not creds:
            raise ModelCallError(
                f"{FLUX_TOKEN_NAME} / {FLUX_URL_NAME} are not in key-store "
                f"service {FLUX_KEY_SERVICE}: /setkey {FLUX_KEY_SERVICE} ..."
            )
        token, base = creds
        preset = request.image_preset or DEFAULT_PRESET
        if preset not in PRESETS:
            raise ModelCallError(f"flux: unknown preset {preset!r}")
        width = request.image_width or DEFAULT_SIZE[0]
        height = request.image_height or DEFAULT_SIZE[1]
        body: Dict[str, Any] = {
            "prompt": request.prompt,
            "width": int(width),
            "height": int(height),
            "n": 1,
            "format": "png",
            "preset": preset,
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            "Accept": "image/png, application/json",
        }
        job_id: Optional[str] = None
        try:
            async with httpx.AsyncClient(timeout=request.timeout_seconds) as client:
                deadline = time.monotonic() + request.timeout_seconds
                resp: Optional[httpx.Response] = None
                if request.image_job_id:
                    # dv-c237: a job an earlier call left behind; the server
                    # keeps it, so fetch it instead of drawing it again.
                    job_id = request.image_job_id
                    resp = await _await_job(
                        client,
                        _job_url(base, job_id),
                        headers,
                        deadline,
                        base,
                        wait=0.0,
                        late=True,
                    )
                    if resp is None:
                        job_id = None  # unknown or failed: submit anew
                if resp is None:
                    ok, reason = await _health_async(client, base, headers)
                    if not ok:
                        raise ModelCallError(f"flux {reason}")
                    if request.images:
                        if not await _takes_images_async(client, base, headers):
                            raise ModelCallError(
                                f"{NO_REF_INPUT}: {len(request.images)} "
                                f"reference image(s) not sent ({FLUX_HARNESS}, "
                                f"purpose {request.purpose})"
                            )
                        body[REF_IMAGES_FIELD] = [
                            f"data:{media};base64,"
                            + base64.b64encode(data).decode("ascii")
                            for media, data in read_images(request)
                        ]
                    url = base.rstrip("/") + GENERATE_PATH
                    resp = await client.post(url, headers=headers, json=body)
                    # Cloudflare 502/503/504 come and go on the GPU's tunnel:
                    # retry FLUX a few times before the route's paid fallback.
                    for wait in (10, 30, 60):
                        if resp.status_code not in GATEWAY:
                            break
                        if time.monotonic() + wait >= deadline:
                            break
                        await asyncio.sleep(wait)
                        resp = await client.post(url, headers=headers, json=body)
                    if resp.status_code in (202, 302, 303, 307):
                        status_url, job_id = _job_of(resp, base)
                        resp = await _await_job(
                            client,
                            status_url,
                            headers,
                            deadline,
                            base,
                            wait=_retry_after(resp),
                        )
                assert resp is not None
                resp.raise_for_status()
        except ModelCallError as exc:
            if job_id is None or exc.job_id is not None:
                raise
            raise type(exc)(f"{exc} (flux job {job_id})", job_id=job_id) from exc
        except httpx.TimeoutException as exc:
            raise ModelCallTimeout(
                f"timeout after {request.timeout_seconds:g}s (flux)"
                + _job_note(job_id),
                job_id=job_id,
            ) from exc
        except httpx.HTTPError as exc:
            # The message may carry the URL: keep only the status / class.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise ModelCallError(
                f"flux call failed: {status or type(exc).__name__}" + _job_note(job_id),
                job_id=job_id,
            ) from exc
        image = _image_of(resp)
        if image is None:
            raise ModelCallError(
                "flux returned no image" + _job_note(job_id), job_id=job_id
            )
        return CallResult(
            text="",
            model=request.model or f"flux:{preset}",
            cost_usd=0.0,
            image=image,
        )


def _job_note(job_id: Optional[str]) -> str:
    return f" (flux job {job_id})" if job_id else ""


def _job_url(base: str, job_id: str) -> httpx.URL:
    """The short status JSON of job ``job_id``."""
    return httpx.URL(base.rstrip("/") + f"{JOBS_PATH}/{job_id}")


def _job_of(resp: httpx.Response, base: str) -> Tuple[httpx.URL, str]:
    """(status URL, job id) of an accepted job: ``Location: jobs/<id>/result``
    (303), or, 14.11.26, a 202 with the id in the body and no Location
    (polling the POST URL itself gave 405 on every image)."""
    location = resp.headers.get("location") or ""
    if location:
        result_url = resp.url.join(location)
        path = result_url.path.rsplit("/result", 1)[0]
        return result_url.copy_with(path=path), path.rstrip("/").rsplit("/", 1)[-1]
    job_id = str(resp.json()["id"])
    return _job_url(base, job_id), job_id


async def _await_job(
    client: httpx.AsyncClient,
    status_url: httpx.URL,
    headers: Dict[str, str],
    deadline: float,
    base: str,
    *,
    wait: float,
    late: bool = False,
) -> Optional[httpx.Response]:
    """A queued job: poll its short status JSON (``jobs/<id>``) until done,
    then GET its first image. No long-held request, so Cloudflare's gateway
    timeout (502/524 on the long-polled ``/result``) is not hit; a gateway
    error or a dropped socket on a status poll is just polled again. The
    generator's health is rechecked about every minute: an outage mid-call
    fails it then, not at the deadline (dv-c237). ``late`` (a job of an
    earlier call): None when the server does not have it or it failed."""
    last_health = time.monotonic()
    while True:
        if time.monotonic() >= deadline:
            raise httpx.TimeoutException("flux job still queued")
        await asyncio.sleep(wait)
        wait = min(10.0, max(wait, 3.0))
        if time.monotonic() - last_health >= HEALTH_RECHECK_SEC:
            last_health = time.monotonic()
            reason = await _generator_stale(client, base, headers)
            if reason:
                raise ModelCallError(f"flux {reason}")
        try:
            st = await client.get(status_url, headers=headers)
        except (httpx.NetworkError, httpx.RemoteProtocolError):
            continue  # 14.11.26: one ReadError cost the image
        if st.status_code in GATEWAY:
            continue
        if late and st.status_code == 404:
            return None
        st.raise_for_status()
        data = st.json()
        status = str(data.get("status") or "")
        if status in ("failed", "error", "cancelled"):
            if late:
                return None
            raise ModelCallError(f"flux job {status}: {str(data.get('error'))[:200]}")
        images = data.get("images") or []
        if status == "done" and images:
            # images[] are server paths ("/v1/jobs/<id>/image/0"); the base URL
            # has a path prefix, so a plain URL join would drop it.
            image = str(images[0])
            url = base.rstrip("/") + image if image.startswith("/") else image
            return await client.get(url, headers=headers)


def _health_of(data: Any) -> Tuple[bool, str]:
    """The ``/health`` JSON's verdict. Up = the generator was seen within
    :data:`GENERATOR_STALE_SEC`, whatever its state (busy is alive); an older
    server without ``generator_last_seen_sec`` counts as up."""
    if not isinstance(data, dict):
        return False, "flux /health is not a JSON object"
    state = str(data.get("generator") or "?")
    seen = data.get("generator_last_seen_sec")
    if seen is None:
        return True, f"generator {state}"
    try:
        sec = float(seen)
    except (TypeError, ValueError):
        return True, f"generator {state}"
    if sec > GENERATOR_STALE_SEC:
        return False, f"generator offline (last seen {sec:.0f} s ago)"
    return True, f"generator {state} (last seen {sec:.0f} s ago)"


def _health_answer(resp: httpx.Response) -> Tuple[bool, str]:
    if resp.status_code != 200:
        return False, f"flux /health answered {resp.status_code}"
    try:
        data = resp.json()
    except ValueError:
        return False, "flux /health is not JSON"
    return _health_of(data)


def _health_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}


def flux_health() -> Tuple[bool, str]:
    """(up, reason) from the server's ``/health`` (dv-c237). The server
    queues jobs while Roman's GPU worker is offline, so only a generator
    seen lately counts as up; no answer, a non-200 or non-JSON = down."""
    creds = flux_credentials()
    if not creds:
        return False, f"{FLUX_TOKEN_NAME} / {FLUX_URL_NAME} not in key store"
    token, base = creds
    try:
        resp = httpx.get(
            base.rstrip("/") + HEALTH_PATH,
            headers=_health_headers(token),
            timeout=HEALTH_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return False, f"flux /health failed: {type(exc).__name__}"
    return _health_answer(resp)


async def _health_async(
    client: httpx.AsyncClient, base: str, headers: Dict[str, str]
) -> Tuple[bool, str]:
    """:func:`flux_health` on the call's own client (before each submit)."""
    try:
        resp = await client.get(
            base.rstrip("/") + HEALTH_PATH, headers=headers, timeout=HEALTH_TIMEOUT
        )
    except httpx.HTTPError as exc:
        return False, f"flux /health failed: {type(exc).__name__}"
    return _health_answer(resp)


async def _generator_stale(
    client: httpx.AsyncClient, base: str, headers: Dict[str, str]
) -> Optional[str]:
    """Mid-poll recheck: the reason only when ``/health`` answers that the
    generator is stale; a /health hiccup does not end a running job."""
    try:
        resp = await client.get(
            base.rstrip("/") + HEALTH_PATH, headers=headers, timeout=HEALTH_TIMEOUT
        )
        data = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    ok, reason = _health_of(data)
    return None if ok else reason


# base URL -> (takes images, monotonic time of the answer)
_CAPABILITY: Dict[str, Tuple[bool, float]] = {}


def _cached_capability(base: str) -> Optional[bool]:
    hit = _CAPABILITY.get(base)
    if hit is None:
        return None
    takes, at = hit
    if takes or time.monotonic() - at < CAPABILITY_RECHECK_SEC:
        return takes
    return None


def _capability_of(resp: Optional[httpx.Response]) -> bool:
    """True when the ``/openapi.json`` answer's ``GenRequest`` has
    :data:`REF_IMAGES_FIELD`; anything unreadable = False (fail closed)."""
    if resp is None or resp.status_code != 200:
        return False
    try:
        doc = resp.json()
        props = doc["components"]["schemas"]["GenRequest"]["properties"]
    except (ValueError, KeyError, TypeError):
        return False
    return isinstance(props, dict) and REF_IMAGES_FIELD in props


def flux_takes_images() -> bool:
    """The FLUX server takes reference images (dv-1c6a): its
    ``/openapi.json`` advertises ``GenRequest.ref_images``. Asked afresh
    (the element tool's one check before drawing); the adapter keeps its own
    cached answer. No credentials or no answer = False."""
    creds = flux_credentials()
    if not creds:
        return False
    token, base = creds
    try:
        resp: Optional[httpx.Response] = httpx.get(
            base.rstrip("/") + OPENAPI_PATH,
            headers=_health_headers(token),
            timeout=HEALTH_TIMEOUT,
        )
    except httpx.HTTPError:
        resp = None
    return _capability_of(resp)


async def _takes_images_async(
    client: httpx.AsyncClient, base: str, headers: Dict[str, str]
) -> bool:
    """:func:`flux_takes_images` on the call's own client, cached per base
    URL: a yes for the process, a no for :data:`CAPABILITY_RECHECK_SEC`."""
    cached = _cached_capability(base)
    if cached is not None:
        return cached
    try:
        resp: Optional[httpx.Response] = await client.get(
            base.rstrip("/") + OPENAPI_PATH, headers=headers, timeout=HEALTH_TIMEOUT
        )
    except httpx.HTTPError:
        resp = None
    takes = _capability_of(resp)
    _CAPABILITY[base] = (takes, time.monotonic())
    return takes


def _retry_after(resp: httpx.Response) -> float:
    """The server's Retry-After seconds, kept within 1..10."""
    try:
        return min(10.0, max(1.0, float(resp.headers.get("retry-after") or 2)))
    except ValueError:
        return 2.0


def _image_of(resp: httpx.Response) -> Optional[bytes]:
    """Raw PNG bytes, or the first data URL / base64 string of ``images``."""
    content = resp.content or b""
    if content.startswith(b"\x89PNG"):
        return content
    try:
        data = resp.json()
    except ValueError:
        return None
    items = data.get("images") if isinstance(data, dict) else None
    for item in items or []:
        if isinstance(item, dict):
            item = item.get("b64") or item.get("data") or item.get("url") or ""
        text = str(item or "")
        if text.startswith("data:") and "," in text:
            text = text.split(",", 1)[1]
        if not text:
            continue
        try:
            return base64.b64decode(text, validate=False)
        except ValueError:
            continue
    return None
