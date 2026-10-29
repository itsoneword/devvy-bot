"""dv-275f phase 2: ``view_image`` inside background jobs (bgjob / pump stages).

Jobs run out of the bot process, so the in-process ``agent`` server and the
Codex agent bridge cannot reach them (analysis, "Phase 2"). The plan: a stdio
MCP server mounted as ``agent`` that serves ONLY the job-safe tools (today:
``view_image``), on both job harnesses. Contract pinned here:

* Claude job: ``job_options(spec, link).mcp_servers`` is a dict whose only key
  is ``"agent"``: a stdio config (``command`` / ``args`` / ``env``). Started
  exactly as configured, with cwd = the job's working directory and only the
  MCP SDK's default env plus the config's ``env`` (codex passes no more than
  that), it lists exactly ``["view_image"]`` and returns an image block.
* Codex job: ``codex_job_argv`` mounts the same server through
  ``-c mcp_servers.agent.command/args/env.<VAR>`` (TOML values, the
  ``codex_cli._stdio_mcp_overrides`` pattern), pre-approves it
  (``mcp_servers.agent.default_tools_approval_mode="approve"``) and switches
  Codex's native tool off (``-c features.view_image=false``), all before
  ``resume``. No other ``mcp_servers.*`` key.
* Roots: the job's ``working_directory`` + ``spec.extra_dirs`` (+ scratch).
  Anything else is refused with an error result and no image.
* The job's policy hook (Claude ``policy_hook``, Codex ``JobPolicyEndpoint``)
  sees ``mcp__agent__view_image`` and applies ``policy.decide``: a workspace
  path runs, a ``paths`` entry in secrets/ is denied without asking.
* ``scripts/backend_smoke.py`` gets an image cell per backend
  (``claude:image``, ``codex:image``) with offline-testable pieces:
  ``write_nonce_png(path, nonce)`` writes a 1170x2532 PNG with the nonce
  drawn on it; ``image_check(reply, nonce, tool_names, notes) -> "PASS" |
  "FAIL"`` (PASS = the nonce is in the reply AND a ``view_image`` tool call
  was made, matched on the name's last ``__`` segment); ``CellResult.image``
  defaults to ``"n/a"`` and a ``"FAIL"`` makes the cell not ``ok``.

The server subprocess needs Pillow in the test venv (importorskip below, as
in test_image_tools.py; the build must show these passing, not skipped).
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import os
import sys
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from src.model_router.job import (  # noqa: E402
    BotLink,
    JobSpec,
    job_options,
    policy_hook,
)
from src.security import denylist, policy  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
MAX_EDGE = 1568
MAX_BYTES = 400_000
SERVER_TIMEOUT = 60.0
# A real protected dir of this install (as in test_policy_parity.py).
_SECRETS_PNG = str(denylist.build_protected_dirs().everyone[0] / "leak.png")


# -- fixtures -----------------------------------------------------------------


class Dirs:
    def __init__(self, root: Path) -> None:
        self.workspace = root / "ws"
        self.extra = root / "extra"
        self.outside = root / "elsewhere"
        for d in (self.workspace, self.extra, self.outside):
            d.mkdir(parents=True)


@pytest.fixture
def dirs(tmp_path: Path) -> Dirs:
    return Dirs(tmp_path)


def _render(path: Path, size: Tuple[int, int] = (1170, 2532)) -> Path:
    """A phone-sized render with some noise, so it does not compress to 0."""
    w, h = size
    img = Image.linear_gradient("L").resize((w, h)).convert("RGB")
    noise = Image.effect_noise((w, h), 12).convert("RGB")
    Image.blend(img, noise, 0.2).save(path, "PNG")
    return path


def _claude_spec(dirs: Dirs, **kw: Any) -> JobSpec:
    base: Dict[str, Any] = dict(
        purpose="background_job",
        name="pump-dv-275f-design",
        prompt="look at the render",
        model="sonnet",
        working_directory=dirs.workspace,
        full_access=False,
        unrestricted=False,
        profile="gymbro",
        extra_dirs=(dirs.extra,),
    )
    base.update(kw)
    return JobSpec(**base)


def _codex_route(tmp_path: Path) -> Any:
    from src.model_router.routes import load_call_routes

    path = tmp_path / "models.yaml"
    path.write_text(
        "jobs:\n  background_job:\n    harness: openai_codex\n", encoding="utf-8"
    )
    return load_call_routes(path).resolve_job("background_job")


def _codex_spec(dirs: Dirs, tmp_path: Path) -> JobSpec:
    route = _codex_route(tmp_path)
    return _claude_spec(dirs, model=route.model, route=route)


def _link() -> BotLink:
    link = BotLink("http://127.0.0.1:1", "s")
    link.ask = AsyncMock(return_value=True)  # type: ignore[method-assign]
    return link


# -- the server as each harness would start it --------------------------------


def _claude_server(spec: JobSpec) -> Dict[str, Any]:
    servers = job_options(spec, _link()).mcp_servers
    assert isinstance(servers, dict), f"no mcp_servers dict on the job: {servers!r}"
    assert "agent" in servers, f"no agent server on the Claude job: {servers!r}"
    cfg = dict(servers["agent"])
    assert cfg.get("type", "stdio") == "stdio", cfg
    return cfg


def _toml_value(raw: str) -> Any:
    return tomllib.loads(f"v = {raw}")["v"]


def _codex_overrides(argv: List[str]) -> Dict[str, Any]:
    """Every ``-c key=value`` before ``resume``, value parsed as TOML."""
    stop = argv.index("resume") if "resume" in argv else len(argv)
    out: Dict[str, Any] = {}
    for i, arg in enumerate(argv[:stop]):
        if arg == "-c" and i + 1 < stop:
            key, _, value = argv[i + 1].partition("=")
            out[key.strip()] = _toml_value(value.strip())
    return out


def _codex_argv(
    spec: JobSpec, tmp_path: Path, resume: Optional[str] = None
) -> List[str]:
    from src.model_router.job_codex import codex_job_argv

    return codex_job_argv(spec, "codex", tmp_path / "out.txt", resume)


def _codex_server(argv: List[str]) -> Dict[str, Any]:
    over = _codex_overrides(argv)
    prefix = "mcp_servers.agent."
    assert prefix + "command" in over, f"no agent server on the Codex job: {argv}"
    env: Dict[str, str] = {}
    for key, value in over.items():
        if key.startswith(prefix + "env."):
            env[key[len(prefix + "env.") :]] = str(value)
    for name in over.get(prefix + "env_vars", []) or []:
        if name in os.environ:
            env[name] = os.environ[name]
    cfg: Dict[str, Any] = {
        "command": over[prefix + "command"],
        "args": list(over.get(prefix + "args", []) or []),
        "env": env,
    }
    if prefix + "cwd" in over:
        cfg["cwd"] = over[prefix + "cwd"]
    return cfg


@asynccontextmanager
async def _session(cfg: Dict[str, Any], cwd: Path) -> AsyncIterator[ClientSession]:
    """Start the configured server the way a harness does: its own cwd, the
    MCP SDK's default env (PATH, HOME, ...) plus the config's ``env``."""
    params = StdioServerParameters(
        command=str(cfg["command"]),
        args=[str(a) for a in cfg.get("args") or []],
        env={str(k): str(v) for k, v in (cfg.get("env") or {}).items()},
        cwd=str(cfg.get("cwd") or cwd),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), SERVER_TIMEOUT)
            yield session


async def _list(cfg: Dict[str, Any], cwd: Path) -> List[str]:
    async with _session(cfg, cwd) as s:
        tools = await asyncio.wait_for(s.list_tools(), SERVER_TIMEOUT)
    return sorted(t.name for t in tools.tools)


async def _view(cfg: Dict[str, Any], cwd: Path, args: Dict[str, Any]) -> Any:
    async with _session(cfg, cwd) as s:
        return await asyncio.wait_for(s.call_tool("view_image", args), SERVER_TIMEOUT)


def _images(result: Any) -> List[Any]:
    return [b for b in result.content if getattr(b, "type", None) == "image"]


def _assert_one_capped_image(result: Any) -> None:
    assert not result.isError, result
    images = _images(result)
    assert len(images) == 1, result
    raw = base64.b64decode(images[0].data)
    assert len(raw) <= MAX_BYTES
    img = Image.open(io.BytesIO(raw))
    assert max(img.size) <= MAX_EDGE
    assert images[0].mimeType in ("image/jpeg", "image/png")


def _assert_refused(result: Any) -> None:
    assert result.isError, result
    assert not _images(result), "a refused path must not return an image"


# -- scenario 1: a job on claude and on codex can call view_image ------------


class TestJobsSeeImages:
    async def test_claude_job_mounts_an_agent_server_that_returns_the_image(
        self, dirs: Dirs
    ) -> None:
        src = _render(dirs.workspace / "render.png")
        cfg = _claude_server(_claude_spec(dirs))

        result = await _view(cfg, dirs.workspace, {"path": str(src)})

        _assert_one_capped_image(result)

    async def test_codex_job_mounts_the_same_server_and_it_returns_the_image(
        self, dirs: Dirs, tmp_path: Path
    ) -> None:
        src = _render(dirs.workspace / "render.png")
        cfg = _codex_server(_codex_argv(_codex_spec(dirs, tmp_path), tmp_path))

        result = await _view(cfg, dirs.workspace, {"path": str(src)})

        _assert_one_capped_image(result)

    @pytest.mark.parametrize("resume", [None, "th-1"])
    def test_codex_job_preapproves_the_server_and_switches_native_view_image_off(
        self, dirs: Dirs, tmp_path: Path, resume: Optional[str]
    ) -> None:
        argv = _codex_argv(_codex_spec(dirs, tmp_path), tmp_path, resume)
        over = _codex_overrides(argv)

        assert over.get("features.view_image") is False, argv
        assert over.get("mcp_servers.agent.default_tools_approval_mode") == "approve"
        assert "mcp_servers.agent.command" in over
        if resume:
            assert argv[argv.index("resume") + 1] == resume

    async def test_claude_job_sheet_of_two_renders_is_one_image(
        self, dirs: Dirs
    ) -> None:
        a = _render(dirs.workspace / "a.png")
        b = _render(dirs.extra / "b.png")
        cfg = _claude_server(_claude_spec(dirs))

        result = await _view(cfg, dirs.workspace, {"paths": [str(a), str(b)]})

        _assert_one_capped_image(result)


# -- scenario 2: limited to the job's roots, passes the job policy hook -------


class TestJobRootsAndPolicy:
    async def test_job_server_refuses_a_path_outside_the_jobs_roots(
        self, dirs: Dirs, tmp_path: Path
    ) -> None:
        inside = _render(dirs.extra / "ok.png", (300, 600))
        outside = _render(dirs.outside / "no.png", (300, 600))
        for cfg in (
            _claude_server(_claude_spec(dirs)),
            _codex_server(_codex_argv(_codex_spec(dirs, tmp_path), tmp_path)),
        ):
            _assert_one_capped_image(
                await _view(cfg, dirs.workspace, {"path": str(inside)})
            )
            _assert_refused(await _view(cfg, dirs.workspace, {"path": str(outside)}))
            _assert_refused(
                await _view(cfg, dirs.workspace, {"paths": [str(inside), str(outside)]})
            )

    async def test_roots_follow_the_job_not_a_fixed_install_dir(
        self, dirs: Dirs, tmp_path: Path
    ) -> None:
        """Two jobs, two workspaces: each sees its own, not the other's."""
        other = Dirs(tmp_path / "second")
        mine = _render(dirs.workspace / "mine.png", (200, 400))
        theirs = _render(other.workspace / "theirs.png", (200, 400))
        cfg = _claude_server(_claude_spec(dirs))
        cfg_other = _claude_server(_claude_spec(other, extra_dirs=()))

        _assert_one_capped_image(await _view(cfg, dirs.workspace, {"path": str(mine)}))
        _assert_refused(await _view(cfg, dirs.workspace, {"path": str(theirs)}))
        _assert_one_capped_image(
            await _view(cfg_other, other.workspace, {"path": str(theirs)})
        )
        _assert_refused(await _view(cfg_other, other.workspace, {"path": str(mine)}))

    async def test_claude_job_hook_runs_view_image_in_the_workspace(
        self, dirs: Dirs
    ) -> None:
        spec = _claude_spec(dirs)
        link = _link()
        hook = policy_hook(
            policy.TurnPolicy(working_directory=dirs.workspace), link, source=spec.name
        )
        call = {
            "tool_name": "mcp__agent__view_image",
            "tool_input": {"path": str(dirs.workspace / "render.png")},
        }

        assert await hook(call, "t1", None) == {}
        link.ask.assert_not_awaited()

    async def test_claude_job_hook_denies_a_secrets_entry_in_paths(
        self, dirs: Dirs
    ) -> None:
        link = _link()
        hook = policy_hook(
            policy.TurnPolicy(working_directory=dirs.workspace), link, source="job"
        )
        call = {
            "tool_name": "mcp__agent__view_image",
            "tool_input": {"paths": [str(dirs.workspace / "a.png"), _SECRETS_PNG]},
        }

        out = await hook(call, "t1", None)

        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        link.ask.assert_not_awaited()

    async def test_codex_job_policy_endpoint_gates_view_image_the_same_way(
        self, dirs: Dirs, tmp_path: Path
    ) -> None:
        from src.model_router.job_codex import JobPolicyEndpoint

        link = _link()
        endpoint = JobPolicyEndpoint(_codex_spec(dirs, tmp_path), link)
        ok = await endpoint.check(
            {
                "tool_name": "mcp__agent__view_image",
                "tool_input": {"path": str(dirs.workspace / "render.png")},
            }
        )
        bad = await endpoint.check(
            {
                "tool_name": "mcp__agent__view_image",
                "tool_input": {"paths": [str(dirs.workspace / "a.png"), _SECRETS_PNG]},
            }
        )
        native = await endpoint.check(
            {"tool_name": "view_image", "tool_input": {"path": "/tmp/a.png"}}
        )

        assert ok["decision"] == policy.ALLOW
        assert bad["decision"] == policy.DENY
        assert native["decision"] == policy.DENY
        link.ask.assert_not_awaited()


# -- scenario 4: jobs get only view_image ------------------------------------


class TestOnlyViewImage:
    async def test_claude_job_server_lists_only_view_image(self, dirs: Dirs) -> None:
        cfg = _claude_server(_claude_spec(dirs))

        assert await _list(cfg, dirs.workspace) == ["view_image"]

    async def test_codex_job_server_lists_only_view_image(
        self, dirs: Dirs, tmp_path: Path
    ) -> None:
        cfg = _codex_server(_codex_argv(_codex_spec(dirs, tmp_path), tmp_path))

        assert await _list(cfg, dirs.workspace) == ["view_image"]

    def test_the_agent_server_is_the_only_one_a_job_mounts(
        self, dirs: Dirs, tmp_path: Path
    ) -> None:
        servers = job_options(_claude_spec(dirs), _link()).mcp_servers
        assert isinstance(servers, dict) and set(servers) == {"agent"}

        over = _codex_overrides(_codex_argv(_codex_spec(dirs, tmp_path), tmp_path))
        names = {k.split(".")[1] for k in over if k.startswith("mcp_servers.")}
        assert names == {"agent"}

    async def test_a_bot_tool_called_by_name_on_the_job_server_is_unknown(
        self, dirs: Dirs
    ) -> None:
        cfg = _claude_server(_claude_spec(dirs))
        async with _session(cfg, dirs.workspace) as s:
            result = await asyncio.wait_for(
                s.call_tool("notify", {"text": "hi"}), SERVER_TIMEOUT
            )
        assert result.isError


# -- added: a job in a repo with its own ``src`` package ----------------------


async def test_job_in_a_repo_with_its_own_src_package_still_gets_the_server(
    dirs: Dirs,
) -> None:
    """Pump stages run in another repo's worktree (gymbro, pl). The server is
    started with that cwd; a ``src/`` package there must not shadow the
    bot's, and the server must not depend on the caller's PYTHONPATH."""
    shadow = dirs.workspace / "src" / "mcp"
    shadow.mkdir(parents=True)
    (dirs.workspace / "src" / "__init__.py").write_text("")
    (shadow / "__init__.py").write_text("")
    src = _render(dirs.workspace / "render.png", (600, 1200))
    cfg = _claude_server(_claude_spec(dirs))

    assert await _list(cfg, dirs.workspace) == ["view_image"]
    _assert_one_capped_image(await _view(cfg, dirs.workspace, {"path": str(src)}))


# -- scenario 3: backend_smoke image cell (offline pieces) -------------------


def _backend_smoke() -> Any:
    spec = importlib.util.spec_from_file_location(
        "backend_smoke_dv_275f", REPO / "scripts" / "backend_smoke.py"
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[spec.name] = module  # type: ignore[union-attr]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


class TestBackendSmokeImageCell:
    def test_there_is_an_image_cell_per_backend(self) -> None:
        smoke = _backend_smoke()
        assert "claude:image" in smoke.CELLS
        assert "codex:image" in smoke.CELLS

    def test_nonce_png_is_a_1170x2532_phone_render(self, tmp_path: Path) -> None:
        smoke = _backend_smoke()
        a = tmp_path / "a.png"
        b = tmp_path / "b.png"

        smoke.write_nonce_png(a, "QX7K2")
        smoke.write_nonce_png(b, "ZP94M")

        with Image.open(a) as img:
            assert img.format == "PNG"
            assert img.size == (1170, 2532)
        # The nonce is drawn on it: a different nonce is a different picture.
        assert a.read_bytes() != b.read_bytes()

    def test_image_check_needs_the_nonce_and_a_view_image_call(self) -> None:
        smoke = _backend_smoke()

        notes: List[str] = []
        assert (
            smoke.image_check(
                "The word on it is QX7K2.", "QX7K2", ["mcp__agent__view_image"], notes
            )
            == "PASS"
        )
        # codex records MCP calls under its own spelling: the last segment counts.
        assert smoke.image_check("qx7k2", "QX7K2", ["view_image"], []) == "PASS"

        for reply, tools in (
            ("I could not see it.", ["mcp__agent__view_image"]),  # no nonce
            ("QX7K2", []),  # guessed without looking
            ("QX7K2", ["Read"]),  # looked the forbidden way
        ):
            notes = []
            assert smoke.image_check(reply, "QX7K2", tools, notes) == "FAIL", reply
            assert notes, "a FAIL must say why"

    def test_a_failed_image_check_fails_the_cell(self) -> None:
        smoke = _backend_smoke()

        assert smoke.CellResult(cell="claude:image").image == "n/a"
        passing = smoke.CellResult(cell="claude:image", tool="PASS", denied="n/a")
        passing.image = "PASS"
        assert passing.ok
        passing.image = "FAIL"
        assert not passing.ok
