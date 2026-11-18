"""dv-1c6a C4: the FLUX adapter sends refs only to a server that advertises
``ref_images`` in /openapi.json; otherwise ModelCallError, nothing sent."""

import asyncio
import itertools
import json  # noqa: F401

import httpx
import pytest

from src.model_router import flux_plain
from src.model_router.types import CallRequest, ModelCallError

PNG = b"\x89PNG\r\n\x1a\n0000"
_n = itertools.count()


class World:
    def __init__(self, base, props=None, openapi_status=200):
        self.base = base
        self.props = props
        self.openapi_status = openapi_status
        self.log: list = []

    def handle(self, method, url, **kw):
        url = str(url)
        self.log.append((method, url, kw))
        req = httpx.Request(method, url)
        if url.endswith("/health"):
            return httpx.Response(200, json={"generator": "idle"}, request=req)
        if url.endswith("/openapi.json"):
            if self.openapi_status != 200 or self.props is None:
                return httpx.Response(self.openapi_status, text="no", request=req)
            doc = {
                "components": {"schemas": {"GenRequest": {"properties": self.props}}}
            }
            return httpx.Response(200, json=doc, request=req)
        if method == "POST":
            return httpx.Response(202, json={"id": "j1"}, request=req)
        if "/image/" in url:
            return httpx.Response(200, content=PNG, request=req)
        if "/v1/jobs/" in url:
            return httpx.Response(
                200,
                json={"status": "done", "images": ["/v1/jobs/j1/image/0"]},
                request=req,
            )
        return httpx.Response(404, request=req)

    def posts(self):
        return [e for e in self.log if e[0] == "POST"]


def _world(monkeypatch, **kw):
    w = World(f"https://flux{next(_n)}.invalid/api", **kw)  # fresh cache key

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **k):
            return w.handle("POST", url, **k)

        async def get(self, url, **k):
            return w.handle("GET", url, **k)

    monkeypatch.setattr(flux_plain.httpx, "AsyncClient", Client)
    monkeypatch.setattr(
        flux_plain.httpx, "get", lambda url, **k: w.handle("GET", url, **k)
    )
    monkeypatch.setattr(flux_plain, "flux_credentials", lambda: ("tok", w.base))
    real = asyncio.sleep

    async def fast(_s, *a):
        await real(0.001)

    monkeypatch.setattr(flux_plain.asyncio, "sleep", fast)
    return w


def _req(images=()):
    return CallRequest(
        purpose="design_mockup",
        prompt="badge",
        system_prompt="",
        harness="flux",
        image_output=True,
        images=tuple(str(i) for i in images),
        timeout_seconds=5.0,
    )


def _refs(tmp_path, n=2):
    out = []
    for i in range(n):
        p = tmp_path / f"r{i}.png"
        p.write_bytes(PNG + bytes([i]))
        out.append(p)
    return out


def test_C4_refs_sent_as_data_urls_when_server_has_the_field(monkeypatch, tmp_path):
    w = _world(monkeypatch, props={"prompt": {}, "ref_images": {"type": "array"}})
    refs = _refs(tmp_path)
    res = asyncio.run(flux_plain.FluxPlainCaller()(_req(refs)))
    assert res.image == PNG
    body = w.posts()[0][2]["json"]
    assert len(body["ref_images"]) == 2
    assert all(u.startswith("data:image/png;base64,") for u in body["ref_images"])
    assert body["ref_images"][0] != body["ref_images"][1]


@pytest.mark.parametrize(
    "kw", [{"props": {"prompt": {}}}, {"props": None, "openapi_status": 500}]
)
def test_C4_no_field_or_unreadable_openapi_raises_and_sends_nothing(
    monkeypatch, tmp_path, kw
):
    w = _world(monkeypatch, **kw)
    with pytest.raises(ModelCallError, match="reference"):
        asyncio.run(flux_plain.FluxPlainCaller()(_req(_refs(tmp_path))))
    assert not w.posts()


def test_C4_request_without_refs_has_no_probe_and_no_field(monkeypatch):
    w = _world(monkeypatch, props={"prompt": {}})
    asyncio.run(flux_plain.FluxPlainCaller()(_req()))
    assert "ref_images" not in w.posts()[0][2]["json"]
    assert not [e for e in w.log if e[1].endswith("/openapi.json")]


def test_C4_late_fetch_of_a_stored_job_skips_the_gate(monkeypatch, tmp_path):
    w = _world(monkeypatch, props={"prompt": {}})
    req = CallRequest(
        purpose="design_mockup",
        prompt="badge",
        system_prompt="",
        harness="flux",
        image_output=True,
        images=tuple(str(p) for p in _refs(tmp_path)),
        image_job_id="j1",
        timeout_seconds=5.0,
    )
    res = asyncio.run(flux_plain.FluxPlainCaller()(req))
    assert res.image == PNG
    assert not w.posts()


def test_C4_capability_probe_is_cached(monkeypatch, tmp_path):
    w = _world(monkeypatch, props={"ref_images": {}})
    for _ in range(2):
        asyncio.run(flux_plain.FluxPlainCaller()(_req(_refs(tmp_path))))
    assert len([e for e in w.log if e[1].endswith("/openapi.json")]) == 1
