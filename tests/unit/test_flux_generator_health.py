"""dv-c237: FLUX generator health check, job ids kept, late fetch."""

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from src.model_router.types import (
    CallRequest,
    CallResult,
    ModelCallError,
    ModelCallTimeout,
)

PNG = b"\x89PNG\r\n\x1a\n0000"
BASE = "https://flux.invalid/api"


class World:
    """One fake FLUX server behind both httpx.get and httpx.AsyncClient."""

    def __init__(self) -> None:
        self.health = {"generator": "idle", "generator_last_seen_sec": 3}
        self.health_status = 200
        self.health_raw = None
        self.log: list = []
        self.post_answer = None  # httpx.Response
        self.jobs: dict = {}  # id -> list of status answers (last repeats)
        self.poll_errors: list = []

    def _health(self):
        req = httpx.Request("GET", BASE + "/health")
        if self.health_raw is not None:
            return httpx.Response(
                self.health_status, content=self.health_raw, request=req
            )
        return httpx.Response(self.health_status, json=self.health, request=req)

    def handle(self, method, url, **kw):
        url = str(url)
        self.log.append((method, url, kw))
        req = httpx.Request(method, url)
        if url.endswith("/health"):
            return self._health()
        if method == "POST":
            ans = self.post_answer
            return ans
        if "/image/" in url:
            return httpx.Response(200, content=PNG, request=req)
        if "/v1/jobs/" in url:
            if self.poll_errors:
                raise self.poll_errors.pop(0)
            jid = url.rsplit("/v1/jobs/", 1)[1].split("/")[0]
            if jid not in self.jobs:
                return httpx.Response(404, json={}, request=req)
            seq = self.jobs[jid]
            data = seq.pop(0) if len(seq) > 1 else seq[0]
            return httpx.Response(200, json=data, request=req)
        return httpx.Response(404, request=req)

    def posts(self):
        return [e for e in self.log if e[0] == "POST"]


@pytest.fixture
def world(monkeypatch):
    from src.model_router import flux_plain

    w = World()

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            return w.handle("POST", url, **kw)

        async def get(self, url, **kw):
            return w.handle("GET", url, **kw)

    monkeypatch.setattr(flux_plain.httpx, "AsyncClient", Client)
    monkeypatch.setattr(
        flux_plain.httpx, "get", lambda url, **kw: w.handle("GET", url, **kw)
    )
    monkeypatch.setattr(flux_plain, "flux_credentials", lambda: ("tok", BASE))
    real_sleep = asyncio.sleep

    async def fast(_s, *a):
        await real_sleep(0.001)

    monkeypatch.setattr(flux_plain.asyncio, "sleep", fast)
    return w


def _req(**kw):
    kw.setdefault("timeout_seconds", 5.0)
    return CallRequest(
        purpose="design_mockup",
        prompt="icon",
        system_prompt="",
        harness="flux",
        image_output=True,
        **kw,
    )


def _accepted(jid="job1"):
    return httpx.Response(
        202,
        json={"id": jid},
        request=httpx.Request("POST", BASE + "/v1/generate"),
    )


def _done(jid="job1"):
    return {"status": "done", "images": [f"/v1/jobs/{jid}/image/0"]}


# ------------------------------------------------------------------ C1


def test_C1_health_probe_reports_generator_state(world):
    from src.model_router import flux_plain

    ok, reason = flux_plain.flux_health()
    assert ok is True
    world.health = {"generator": "offline", "generator_last_seen_sec": 4210}
    ok, reason = flux_plain.flux_health()
    assert ok is False and "4210" in reason
    world.health = {"generator": "busy", "generator_last_seen_sec": 30}
    assert flux_plain.flux_health()[0] is True
    world.health = {"generator": "idle"}  # older server: counts as up
    assert flux_plain.flux_health()[0] is True
    world.health_raw = b"<html>nope</html>"
    assert flux_plain.flux_health()[0] is False
    world.health_raw = None
    world.health_status = 502
    assert flux_plain.flux_health()[0] is False
    method, url, kw = world.log[0]
    assert url == BASE + "/health"
    assert kw["headers"]["Authorization"] == "Bearer tok"
    assert kw["headers"]["User-Agent"].startswith("curl/")


def test_C1_mockups_flux_up_uses_the_health_probe_not_an_empty_post(world):
    from src.designloop import mockups

    assert mockups._flux_up() is True
    world.health = {"generator": "idle", "generator_last_seen_sec": 99999}
    assert mockups._flux_up() is False
    assert not world.posts()


# ------------------------------------------------------------------ C2


async def test_C2_stale_generator_fails_before_any_post(world):
    from src.model_router import flux_plain

    world.health = {"generator": "idle", "generator_last_seen_sec": 4210}
    world.post_answer = _accepted()
    with pytest.raises(ModelCallError) as ei:
        await flux_plain.FluxPlainCaller()(_req())
    msg = str(ei.value)
    assert "generator" in msg and "4210" in msg
    assert not world.posts()


async def test_C2_health_is_checked_before_each_call(world):
    from src.model_router import flux_plain

    world.post_answer = _accepted()
    world.jobs["job1"] = [_done()]
    caller = flux_plain.FluxPlainCaller()
    assert (await caller(_req())).image == PNG
    world.health = {"generator": "idle", "generator_last_seen_sec": 5000}
    with pytest.raises(ModelCallError):
        await caller(_req())
    assert len(world.posts()) == 1


# ------------------------------------------------------------------ C3


async def test_C3_timeout_keeps_job_id_in_attribute_and_message(world):
    from src.model_router import flux_plain

    world.post_answer = _accepted("abc123")
    world.jobs["abc123"] = [{"status": "queued"}]
    with pytest.raises(ModelCallTimeout) as ei:
        await flux_plain.FluxPlainCaller()(_req(timeout_seconds=0.3))
    assert ei.value.job_id == "abc123"
    assert "abc123" in str(ei.value)


async def test_C3_303_location_job_id_is_kept(world):
    from src.model_router import flux_plain

    world.post_answer = httpx.Response(
        303,
        headers={"location": "jobs/loc77/result"},
        request=httpx.Request("POST", BASE + "/v1/generate"),
    )
    world.jobs["loc77"] = [{"status": "failed", "error": "boom"}]
    with pytest.raises(ModelCallError) as ei:
        await flux_plain.FluxPlainCaller()(_req())
    assert ei.value.job_id == "loc77"


async def test_C3_image_job_id_fetches_without_posting(world):
    from src.model_router import flux_plain

    assert "image_job_id" in CallRequest.__dataclass_fields__
    world.jobs["late1"] = [_done("late1")]
    result = await flux_plain.FluxPlainCaller()(_req(image_job_id="late1"))
    assert result.image == PNG
    assert not world.posts()


async def test_C3_unknown_job_id_submits_a_new_job(world):
    from src.model_router import flux_plain

    world.post_answer = _accepted("new1")
    world.jobs["new1"] = [_done("new1")]
    result = await flux_plain.FluxPlainCaller()(_req(image_job_id="gone"))
    assert result.image == PNG
    assert len(world.posts()) == 1


# ------------------------------------------------------------------ C4


class FakeRouter:
    def __init__(self, exc=None):
        self.calls: list = []
        self.exc = exc

    async def complete(self, request):
        self.calls.append(request)
        if self.exc:
            raise self.exc
        return CallResult(text="", image=PNG)


def _env(monkeypatch, tmp_path, router):
    from src.designloop import elements, mockups
    from src.pump2 import state

    board = SimpleNamespace(
        name="gymbro",
        repo=tmp_path / "repo",
        design={"python": "/usr/bin/python3", "mockups": {}},
    )
    fake_load = lambda *a, **k: {"gymbro": board}  # noqa: E731
    monkeypatch.setattr(state, "load_boards", fake_load)
    monkeypatch.setattr(elements, "load_boards", fake_load, raising=False)
    monkeypatch.setattr(mockups, "get_router", lambda: router)
    monkeypatch.setattr(mockups, "_flux_up", lambda: True, raising=False)
    monkeypatch.setattr(elements, "_flux_up", lambda: True, raising=False)
    monkeypatch.setenv("MODEL_ROUTES_PATH", "")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
    )


def _prompts(path: Path):
    path.write_text(
        yaml.safe_dump([{"group": "g", "name": "a", "prompt": "x"}]), encoding="utf-8"
    )
    return path


def test_C4_failure_with_job_id_is_logged_and_resume_passes_it(monkeypatch, tmp_path):
    from src.designloop import elements

    exc = ModelCallError("timeout after 900s (flux) job zzz9")
    exc.job_id = "zzz9"
    router = FakeRouter(exc)
    _env(monkeypatch, tmp_path, router)
    prompts = _prompts(tmp_path / "p.yaml")
    out = tmp_path / "out"
    assert elements.main(["gymbro", str(prompts), "--out", str(out)]) == 1
    lines = [json.loads(ln) for ln in (out / "log.jsonl").read_text().splitlines()]
    assert lines[-1]["ok"] is False and lines[-1]["job_id"] == "zzz9"

    router2 = FakeRouter()
    _env(monkeypatch, tmp_path, router2)
    assert elements.main(["gymbro", str(prompts), "--out", str(out)]) == 0
    assert router2.calls[0].image_job_id == "zzz9"
    assert (out / "g" / "a.png").is_file()


async def test_C4_draw_image_takes_job_id_kwarg(monkeypatch):
    from src.designloop import mockups

    router = FakeRouter()
    monkeypatch.setattr(mockups, "get_router", lambda: router)
    await mockups.draw_image("p", (64, 64), job_id="j1")
    assert router.calls[0].image_job_id == "j1"


# ------------------------------------------------------------------ C5


def test_C5_generator_down_stops_before_first_prompt(monkeypatch, tmp_path, capsys):
    from src.designloop import elements
    from src.model_router import flux_plain

    router = FakeRouter()
    _env(monkeypatch, tmp_path, router)
    reason = "generator offline (last seen 4210 s ago)"
    monkeypatch.setattr(elements, "_flux_up", lambda: False, raising=False)
    monkeypatch.setattr(flux_plain, "flux_health", lambda: (False, reason))
    monkeypatch.setattr(elements, "flux_health", lambda: (False, reason), raising=False)
    rc = elements.main(
        ["gymbro", str(_prompts(tmp_path / "p.yaml")), "--out", str(tmp_path / "o")]
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "FLUX down" in out and "generator offline" in out and "4210" in out
    assert not router.calls


# ------------------------------------------------------------------ C6


async def test_C6_poll_transport_error_is_retried(world):
    from src.model_router import flux_plain

    world.post_answer = _accepted("p1")
    world.jobs["p1"] = [_done("p1")]
    world.poll_errors = [httpx.ReadError("x"), httpx.ConnectError("y")]
    result = await flux_plain.FluxPlainCaller()(_req())
    assert result.image == PNG
