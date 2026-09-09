"""The agent tools, served over loopback HTTP MCP (dv-ffcb).

The Claude path mounts ``src/claude/agent_tools.py`` as an in-process SDK MCP
server — a Python object, no socket. Codex is a separate process and can only
reach MCP servers over stdio or HTTP, so it saw none of them. This module is
the second transport for the *same* :class:`ToolSpec` list: FastMCP over
Starlette, the pattern ``src/channel_intel/server.py`` already proves works
against both backends.

What is different from channel-intel, and why:

* **Bound to loopback, always.** The host is not configurable. Nothing outside
  this machine has any business calling these tools.
* **Per-turn bearer tokens, not a shared secret.** channel-intel's data is not
  per-user; every tool here is. A token is minted at the start of one Codex
  turn, placed only in that child's environment, and revoked when the turn
  returns (:mod:`src.agent_bridge.tokens`).
* **The tool list is filtered per request** by the context the token resolves
  to — exactly the filter ``build_agent_mcp_server`` applies on the SDK side,
  because it is literally the same function (``available_tool_specs``).

Fail-closed rules, in order of how badly each would hurt:

1. An unknown or expired token gets 401 and never a default context.
2. A tool the resolved context may not see is not listed AND refuses when
   called by name anyway.
3. No error path echoes the token — not in a log line, not in a response.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any, Collection, Dict, List, Mapping, Optional

import structlog
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ContentBlock, TextContent
from mcp.types import Tool as MCPTool
from src.claude import agent_tools
from src.claude.agent_tools import available_tool_specs
from src.claude.tool_context import AgentServices, ToolSpec, TurnContext

from .tokens import DEFAULT_TTL_SECONDS, TokenRegistry

logger = structlog.get_logger()

# Which specs the bridge serves: every ``ToolSpec`` the Claude path serves,
# minus the names below. The two transports list the SAME specs by
# construction — a tool added to ``agent_tools.py`` reaches Codex on the next
# turn with nothing to update here — and diverge only by these declared
# exceptions. (Until dv-afda this was a hand-maintained allowlist of roll-out
# waves; every wave is live, and an allowlist's failure mode is a new tool
# silently never reaching Codex.)
#
# One entry per withheld tool, value = the one-line reason it stays off the
# HTTP transport. A withheld tool is not listed AND is refused when called by
# name; ``TestExposure`` fails on an entry that names no real spec or gives
# no reason. Empty today: nothing in ``ALL_TOOL_SPECS`` needs the in-process
# SDK, and the owner-only surface (jobs) is gated by the shared
# ``available`` predicate on both transports, not here.
#
# Not a candidate: ``send_image_to_user``. It is not a ``ToolSpec`` at all but
# a stdio MCP server (``config/mcp.json``) that ``codex_cli`` mounts for Codex
# the way the Claude path mounts it — see DECISIONS 2026-09-05.
NOT_EXPORTED: Mapping[str, str] = {}

# The name set the bridge withholds by default — the denylist's keys.
DENIED_TOOLS: frozenset[str] = frozenset(NOT_EXPORTED)


def exported_tool_names(denied: Collection[str] = DENIED_TOOLS) -> frozenset[str]:
    """Every spec name the bridge can serve, before per-context filtering.

    Reads the live ``agent_tools.ALL_TOOL_SPECS`` rather than a snapshot, so
    the number logged at start is the number actually served.
    """
    return frozenset(
        spec.name for spec in agent_tools.ALL_TOOL_SPECS if spec.name not in denied
    )


# The env var the child process reads its token from. Only ever set on the
# child's environment, never on the bot's own.
TOKEN_ENV_VAR = "AGENT_BRIDGE_TOKEN"

MCP_PATH = "/mcp"


def bearer_token(header: Optional[str]) -> Optional[str]:
    """Extract the token from an ``Authorization: Bearer <token>`` header."""
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def _error(message: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=f"error: {message}")], isError=True
    )


def _to_result(payload: Dict[str, Any]) -> CallToolResult:
    """Convert a tool handler's dict result into an MCP CallToolResult.

    The handlers return the SDK's shape (``{"content": [...]}`` plus an error
    flag). Both spellings of that flag are honoured because the two tool
    modules historically disagreed (``isError`` / ``is_error``) and a write
    tool reporting success for a refused write is the one failure this
    project keeps coming back to.
    """
    blocks: List[ContentBlock] = [
        TextContent(type="text", text=str(item.get("text", "")))
        for item in payload.get("content") or []
        if item.get("type") == "text"
    ]
    is_error = bool(payload.get("is_error") or payload.get("isError"))
    return CallToolResult(content=blocks, isError=is_error)


class AgentBridgeMcp(FastMCP):
    """FastMCP server whose tool list and dispatch are per-request scoped."""

    def __init__(
        self,
        services: AgentServices,
        tokens: TokenRegistry,
        denied: Collection[str] = DENIED_TOOLS,
    ) -> None:
        # Stateless: one MCP session per HTTP request. The turn's identity
        # lives in the request's own Authorization header, so nothing is
        # carried over between requests that could be read as another turn's.
        super().__init__("agent", stateless_http=True)
        self._services = services
        self._tokens = tokens
        self._denied = frozenset(denied)

    # -- context resolution -------------------------------------------------

    def _request(self) -> Optional[Request]:
        """The Starlette request behind the MCP message being handled."""
        try:
            request = self._mcp_server.request_context.request
        except LookupError:
            return None
        return request if isinstance(request, Request) else None

    def resolve_context(self) -> Optional[TurnContext]:
        """Whose turn this request is — or None, which means "nobody"."""
        request = self._request()
        if request is None:
            # The auth middleware already rejected anything unauthenticated,
            # so this is a plumbing failure, not an attack. Fail closed
            # anyway: a default context is the one thing that must never
            # happen here.
            logger.warning("agent bridge: no request context for MCP message")
            return None
        return self._tokens.resolve(bearer_token(request.headers.get("authorization")))

    def specs_for(self, ctx: TurnContext) -> List[ToolSpec]:
        """The tools this context may use, minus the denylist.

        The first factor is the shared filter — literally the list the SDK
        server mounts for the same context. The second is the only place the
        two transports are allowed to differ.
        """
        return [
            spec
            for spec in available_tool_specs(self._services, ctx)
            if spec.name not in self._denied
        ]

    # -- MCP handlers -------------------------------------------------------

    async def list_tools(self) -> List[MCPTool]:  # type: ignore[override]
        ctx = self.resolve_context()
        if ctx is None:
            return []
        specs = self.specs_for(ctx)
        logger.debug(
            "agent bridge list_tools",
            profile=ctx.profile_slug,
            user_id=ctx.user_id,
            tools=len(specs),
        )
        return [
            MCPTool(
                name=spec.name,
                description=spec.description,
                inputSchema=spec.schema,
            )
            for spec in specs
        ]

    async def call_tool(  # type: ignore[override]
        self, name: str, arguments: Dict[str, Any]
    ) -> CallToolResult:
        ctx = self.resolve_context()
        if ctx is None:
            return _error("this session is not authorized to use agent tools")
        spec = next((s for s in self.specs_for(ctx) if s.name == name), None)
        if spec is None:
            # Same answer for "no such tool" and "not yours": a caller must
            # not be able to probe what other sessions can do.
            return _error(f"tool {name!r} is not available in this session")
        try:
            payload = await spec.handler(self._services, ctx, arguments or {})
        except Exception as exc:  # a tool must not take the turn down
            logger.warning(
                "agent bridge tool failed",
                tool=name,
                profile=ctx.profile_slug,
                error=str(exc),
            )
            return _error(f"{name} failed: {exc}")
        logger.info(
            "agent bridge tool call",
            tool=name,
            profile=ctx.profile_slug,
            user_id=ctx.user_id,
        )
        return _to_result(payload)


def build_app(
    services: AgentServices,
    tokens: TokenRegistry,
    denied: Collection[str] = DENIED_TOOLS,
) -> Starlette:
    """Build the Starlette app: FastMCP + per-turn bearer auth."""
    mcp = AgentBridgeMcp(services, tokens, denied)
    app: Starlette = mcp.streamable_http_app()

    class Auth(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            token = bearer_token(request.headers.get("authorization"))
            if tokens.resolve(token) is None:
                # No detail, and above all no echo of what was sent.
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)  # type: ignore[no-any-return]

    app.add_middleware(Auth)
    app.state.agent_bridge_mcp = mcp
    return app


class AgentBridge:
    """The bridge's lifecycle: a loopback uvicorn server plus the token table.

    Started with the bot and stopped with it. Deliberately started regardless
    of the configured ``AGENT_BACKEND``: the backend is a per-user, runtime
    choice (``/model`` writes ``user_data["agent_backend"]``), so gating the
    bridge on the process-wide default would leave tools missing for exactly
    the user who switched to Codex from Telegram — the dv-0c01 scenario.
    """

    def __init__(
        self,
        services: AgentServices,
        port: int,
        token_ttl_seconds: float = DEFAULT_TTL_SECONDS,
        denied: Collection[str] = DENIED_TOOLS,
    ) -> None:
        self.host = "127.0.0.1"  # not configurable, on purpose
        self.port = int(port)
        self.tokens = TokenRegistry(ttl_seconds=token_ttl_seconds)
        self.denied = frozenset(denied)
        self.app = build_app(services, self.tokens, self.denied)
        self._server: Optional[Any] = None
        self._task: Optional[asyncio.Task[Any]] = None
        self._socket: Optional[socket.socket] = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{MCP_PATH}"

    async def start(self) -> None:
        """Bind the loopback socket and serve on it.

        The socket is bound HERE, not inside uvicorn: uvicorn's own bind
        failure path calls ``sys.exit(1)``, and a SystemExit raised inside a
        task can take the whole bot's event loop with it. Binding first turns
        an occupied port into an ordinary OSError the caller can log and
        continue without tools.
        """
        import uvicorn

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, self.port))
            sock.listen(128)
        except OSError:
            sock.close()
            raise
        self._socket = sock
        config = uvicorn.Config(
            self.app,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(
            self._server.serve(sockets=[sock]), name="agent-bridge"
        )
        # Wait for the app to be up: a Codex turn started in the same second
        # as the bot must not race the startup.
        for _ in range(100):
            if self._task.done() or getattr(self._server, "started", False):
                break
            await asyncio.sleep(0.05)
        if not getattr(self._server, "started", False):
            raise RuntimeError(f"agent bridge did not start on {self.host}:{self.port}")
        logger.info(
            "Agent bridge listening",
            url=self.url,
            tools=len(exported_tool_names(self.denied)),
            withheld=sorted(self.denied),
        )

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            except Exception as exc:  # shutdown is best-effort
                logger.warning("Agent bridge shutdown failed", error=str(exc))
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self.tokens.clear()
        logger.info("Agent bridge stopped")

    # -- per-turn credentials ----------------------------------------------

    def mint(self, context: TurnContext) -> str:
        return self.tokens.mint(context)

    def revoke(self, token: Optional[str]) -> None:
        self.tokens.revoke(token)
