"""view_image on the agent server (dv-275f, phase 1).

The tool is a ToolSpec in ``ALL_TOOL_SPECS``, so the same handler serves the
Claude in-process server and the Codex bridge. These tests drive the handler
through that list (the real entry point both transports use) and, for Codex,
through ``AgentBridgeMcp.call_tool``.

Pillow is not in every venv (the bot venv gets it via ``poetry install``):
run with ``PYTHONPATH=<pip --target dir>`` until then. The build must show
these PASSING, not skipped.
"""

from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw  # noqa: E402

from src.agent_bridge.server import AgentBridgeMcp  # noqa: E402
from src.agent_bridge.tokens import TokenRegistry  # noqa: E402
from src.claude import agent_tools  # noqa: E402
from src.claude.tool_context import AgentServices, ToolSpec, TurnContext  # noqa: E402
from src.security import denylist  # noqa: E402

MAX_BYTES = 400_000
MAX_EDGE = 1568


# -- fixtures ---------------------------------------------------------------


@dataclass
class _FakeProfile:
    working_dir: Path
    extra_dirs: Tuple[Path, ...] = ()
    slug: str = "gymbro"
    sandbox: bool = True


class _FakeRegistry:
    def __init__(self, profiles: List[_FakeProfile]) -> None:
        self._profiles = profiles

    def get_by_working_dir(self, path: Path) -> Optional[_FakeProfile]:
        for profile in self._profiles:
            if Path(profile.working_dir) == Path(path):
                return profile
        return None

    def get_by_slug(self, slug: str) -> Optional[_FakeProfile]:
        for profile in self._profiles:
            if profile.slug == slug:
                return profile
        return None


@dataclass
class Env:
    root: Path
    workspace: Path
    extra: Path
    outside: Path
    secrets: Path
    services: AgentServices
    ctx: TurnContext


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    """A profile with a workspace and one extra_dir; secrets/ is protected.

    secrets/ sits INSIDE the workspace on purpose: being under an allowed root
    must not make it viewable (same rule as send_image_to_user).
    """
    root = tmp_path
    workspace = root / "ws"
    extra = root / "extra"
    outside = root / "elsewhere"
    secrets = workspace / "secrets"
    for d in (workspace, extra, outside, secrets):
        d.mkdir(parents=True)
    monkeypatch.setattr(
        denylist,
        "protected_dirs",
        lambda: denylist.ProtectedDirs(everyone=(secrets,), tenant=()),
    )
    registry = _FakeRegistry([_FakeProfile(working_dir=workspace, extra_dirs=(extra,))])
    services = AgentServices(event_bus=None, task_repo=None, profile_registry=registry)
    ctx = TurnContext(
        profile_slug="gymbro", working_dir=workspace, user_id=111, role="owner"
    )
    return Env(root, workspace, extra, outside, secrets, services, ctx)


def _ui_render(path: Path, size: Tuple[int, int] = (1170, 2532)) -> Path:
    """A phone-sized UI render: gradient, cards, text, a bit of noise."""
    w, h = size
    img = Image.linear_gradient("L").resize((w, h)).convert("RGB")
    noise = Image.effect_noise((w, h), 12).convert("RGB")
    img = Image.blend(img, noise, 0.15)
    draw = ImageDraw.Draw(img)
    for i in range(12):
        top = 80 + i * (h // 13)
        draw.rounded_rectangle(
            (40, top, w - 40, top + h // 16), radius=24, fill=(30 + i * 15, 90, 160)
        )
        draw.text((70, top + 20), f"Workout card {i} — 3x10 @ 60kg", fill="white")
    img.save(path, "PNG")
    return path


def _solid(path: Path, size: Tuple[int, int], color: str) -> Path:
    Image.new("RGB", size, color).save(path, "PNG")
    return path


def _spec() -> ToolSpec:
    spec = next((s for s in agent_tools.ALL_TOOL_SPECS if s.name == "view_image"), None)
    assert spec is not None, "view_image is not in ALL_TOOL_SPECS"
    return spec


async def _call(env: Env, args: Dict[str, Any]) -> Dict[str, Any]:
    spec = _spec()
    assert spec.available(env.services, env.ctx)
    return await spec.handler(env.services, env.ctx, args)


def _images(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [b for b in result.get("content") or [] if b.get("type") == "image"]


def _text(result: Dict[str, Any]) -> str:
    return "\n".join(
        str(b.get("text", ""))
        for b in result.get("content") or []
        if b.get("type") == "text"
    )


def _is_error(result: Dict[str, Any]) -> bool:
    return bool(result.get("isError") or result.get("is_error"))


def _decode(block: Dict[str, Any]) -> Tuple[bytes, Image.Image]:
    raw = base64.b64decode(block["data"])
    img = Image.open(io.BytesIO(raw))
    img.load()
    return raw, img


def _assert_refused(result: Dict[str, Any]) -> None:
    assert _is_error(result), result
    assert _images(result) == [], "a refused call must carry no image block"


# -- scenario 1: one render, downscaled under the cap --------------------------


class TestSingleImage:
    async def test_phone_render_is_one_image_under_edge_and_byte_cap(
        self, env: Env
    ) -> None:
        src = _ui_render(env.workspace / "render.png")
        assert src.stat().st_size > 0

        result = await _call(env, {"path": str(src)})

        assert not _is_error(result), _text(result)
        images = _images(result)
        assert len(images) == 1
        block = images[0]
        assert block["mimeType"] in ("image/jpeg", "image/png")
        raw, img = _decode(block)
        assert len(raw) <= MAX_BYTES
        assert max(img.size) <= MAX_EDGE
        # Downscaled, not shredded: the ticket's band is ~1000-1568 px.
        assert max(img.size) >= 1000
        # Aspect ratio kept (1170x2532 -> about 0.46).
        assert img.size[0] / img.size[1] == pytest.approx(1170 / 2532, rel=0.02)
        # The format the block claims is the format that was sent.
        assert Image.MIME[img.format] == block["mimeType"]

    async def test_meta_text_reports_original_and_sent_size(self, env: Env) -> None:
        src = _ui_render(env.workspace / "render.png")
        result = await _call(env, {"path": str(src)})
        text = _text(result)
        assert "1170" in text and "2532" in text

    async def test_image_in_an_extra_dir_is_viewable(self, env: Env) -> None:
        src = _solid(env.extra / "ref.png", (300, 600), "red")
        result = await _call(env, {"path": str(src)})
        assert not _is_error(result), _text(result)
        assert len(_images(result)) == 1


# -- scenario 2: contact sheet, region crop, max_edge -----------------------


class TestSheetCropEdge:
    async def test_three_paths_give_one_numbered_contact_sheet(self, env: Env) -> None:
        paths = [
            _solid(env.workspace / f"variant_{name}.png", (400, 800), name)
            for name in ("red", "green", "blue")
        ]
        result = await _call(env, {"paths": [str(p) for p in paths]})

        assert not _is_error(result), _text(result)
        images = _images(result)
        assert len(images) == 1, "a sheet is ONE image block"
        raw, img = _decode(images[0])
        assert len(raw) <= MAX_BYTES
        assert max(img.size) <= MAX_EDGE
        # Side by side: the sheet is wider than one 1:2 cell would be.
        assert img.size[0] / img.size[1] > 0.5
        # The legend numbers the cells and names the files.
        legend = _text(result)
        for i, p in enumerate(paths, start=1):
            assert p.name in legend
            assert str(i) in legend

    async def test_more_than_six_paths_is_an_error(self, env: Env) -> None:
        paths = [
            str(_solid(env.workspace / f"v{i}.png", (100, 100), "white"))
            for i in range(7)
        ]
        result = await _call(env, {"paths": paths})
        _assert_refused(result)

    async def test_region_crops_to_the_requested_fraction(self, env: Env) -> None:
        """Top quarter red, rest blue; region = top quarter -> a red 2:1 image."""
        src = env.workspace / "split.png"
        img = Image.new("RGB", (1000, 2000), "blue")
        ImageDraw.Draw(img).rectangle((0, 0, 999, 499), fill="red")
        img.save(src, "PNG")

        result = await _call(env, {"path": str(src), "region": [0, 0, 1, 0.25]})

        assert not _is_error(result), _text(result)
        _, out = _decode(_images(result)[0])
        assert out.size[0] / out.size[1] == pytest.approx(2.0, rel=0.05)
        r, g, b = out.convert("RGB").resize((1, 1)).getpixel((0, 0))
        assert r > 200 and b < 60, (r, g, b)

    async def test_max_edge_is_honoured(self, env: Env) -> None:
        src = _ui_render(env.workspace / "render.png")
        result = await _call(env, {"path": str(src), "max_edge": 512})
        assert not _is_error(result), _text(result)
        _, img = _decode(_images(result)[0])
        assert max(img.size) <= 512

    async def test_max_edge_above_the_limit_is_clamped(self, env: Env) -> None:
        src = _ui_render(env.workspace / "render.png")
        result = await _call(env, {"path": str(src), "max_edge": 5000})
        assert not _is_error(result), _text(result)
        raw, img = _decode(_images(result)[0])
        assert max(img.size) <= MAX_EDGE
        assert len(raw) <= MAX_BYTES


# -- scenario 3: refusals ---------------------------------------------------


class TestRefusals:
    async def test_path_outside_the_profiles_roots_is_refused(self, env: Env) -> None:
        src = _solid(env.outside / "x.png", (100, 100), "red")
        _assert_refused(await _call(env, {"path": str(src)}))

    async def test_path_in_secrets_is_refused(self, env: Env) -> None:
        src = _solid(env.secrets / "x.png", (100, 100), "red")
        _assert_refused(await _call(env, {"path": str(src)}))

    async def test_svg_is_refused(self, env: Env) -> None:
        src = env.workspace / "logo.svg"
        src.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
            '<rect width="10" height="10"/></svg>'
        )
        _assert_refused(await _call(env, {"path": str(src)}))

    async def test_relative_and_missing_paths_are_refused(self, env: Env) -> None:
        _solid(env.workspace / "rel.png", (100, 100), "red")
        _assert_refused(await _call(env, {"path": "rel.png"}))
        _assert_refused(await _call(env, {"path": str(env.workspace / "nope.png")}))

    async def test_one_bad_path_in_a_sheet_refuses_the_whole_call(
        self, env: Env
    ) -> None:
        ok = _solid(env.workspace / "ok.png", (100, 100), "red")
        bad = _solid(env.secrets / "bad.png", (100, 100), "red")
        _assert_refused(await _call(env, {"paths": [str(ok), str(bad)]}))

    async def test_symlink_out_of_the_roots_is_refused(self, env: Env) -> None:
        target = _solid(env.outside / "x.png", (100, 100), "red")
        link = env.workspace / "link.png"
        link.symlink_to(target)
        _assert_refused(await _call(env, {"path": str(link)}))


# -- scenario 5 (Codex half): the bridge carries the image -------------------


class TestCodexBridge:
    async def test_view_image_through_the_bridge_returns_image_content(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = _ui_render(env.workspace / "render.png")
        bridge = AgentBridgeMcp(env.services, TokenRegistry())
        monkeypatch.setattr(bridge, "resolve_context", lambda: env.ctx)

        listed = {tool.name for tool in await bridge.list_tools()}
        assert "view_image" in listed

        out = await bridge.call_tool("view_image", {"path": str(src)})

        assert not out.isError, out
        images = [b for b in out.content if getattr(b, "type", None) == "image"]
        assert len(images) == 1
        raw = base64.b64decode(images[0].data)  # type: ignore[union-attr]
        assert len(raw) <= MAX_BYTES
        img = Image.open(io.BytesIO(raw))
        assert max(img.size) <= MAX_EDGE
        assert images[0].mimeType in ("image/jpeg", "image/png")  # type: ignore[union-attr]

    async def test_bridge_refuses_a_secrets_path_in_paths(
        self, env: Env, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ok = _solid(env.workspace / "ok.png", (100, 100), "red")
        bad = _solid(env.secrets / "bad.png", (100, 100), "red")
        bridge = AgentBridgeMcp(env.services, TokenRegistry())
        monkeypatch.setattr(bridge, "resolve_context", lambda: env.ctx)

        out = await bridge.call_tool("view_image", {"paths": [str(ok), str(bad)]})

        assert out.isError
        assert not [b for b in out.content if getattr(b, "type", None) == "image"]


# -- added: no Pillow -> a clear tool error, not a crash ------------------------


async def test_without_pillow_view_image_returns_a_clear_error(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live venv gets Pillow only after ``poetry install`` (owner, 1.1).

    Until then the bot must start and the tool must say why it cannot work.
    Blocking the import here also proves PIL is imported lazily, inside the
    call, not at module load.
    """
    import sys

    src = _solid(env.workspace / "a.png", (200, 200), "green")
    for name in [m for m in sys.modules if m == "PIL" or m.startswith("PIL.")]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "PIL", None)

    result = await _call(env, {"path": str(src)})

    _assert_refused(result)
    assert "pillow" in _text(result).lower()
    json.dumps(result)
