"""Stdio MCP server giving a background job ONE tool: ``view_image`` (dv-275f).

Jobs (bgjob / pump stages) run out of the bot process, so the in-process
``agent`` server and the Codex bridge cannot reach them. This server is
mounted under the same name, ``agent``, so the job policy hook sees
``mcp__agent__view_image`` exactly as a bot turn does. It serves the shared
``view_image`` ToolSpec handler and nothing else: no bot tool is reachable.

Run as a script (``python /path/to/job_image_server.py``): the harness starts
it with the job's cwd, which may be a repo with its own ``src/`` package, so
the bot's checkout is put first on ``sys.path`` from this file's location
rather than relying on cwd or PYTHONPATH. Roots come from the env the job
mounts it with: ``JOB_IMAGE_WORKDIR`` and ``JOB_IMAGE_EXTRA_DIRS``
(os.pathsep-joined); session scratch roots are added by ``allowed_image_roots``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_BOT_ROOT = str(Path(__file__).resolve().parents[2])
if sys.path[:1] != [_BOT_ROOT]:
    sys.path.insert(0, _BOT_ROOT)

import anyio  # noqa: E402

import mcp.types as types  # noqa: E402
from mcp.server.lowlevel import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402
from src.bot.utils.image_extractor import allowed_image_roots  # noqa: E402
from src.claude.image_tools import IMAGE_SPECS, view_image_in_roots  # noqa: E402

WORKDIR_ENV = "JOB_IMAGE_WORKDIR"
EXTRA_DIRS_ENV = "JOB_IMAGE_EXTRA_DIRS"

_SPEC = IMAGE_SPECS[0]
server: Server = Server("agent")


def _roots() -> list[Path]:
    workdir = os.environ.get(WORKDIR_ENV) or None
    extra = [d for d in os.environ.get(EXTRA_DIRS_ENV, "").split(os.pathsep) if d]
    return allowed_image_roots(workdir, extra)


@server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
async def _list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=_SPEC.name, description=_SPEC.description, inputSchema=_SPEC.schema
        )
    ]


@server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
async def _call_tool(
    name: str, arguments: dict[str, object]
) -> list[types.TextContent | types.ImageContent]:
    if name != _SPEC.name:
        raise ValueError(f"unknown tool: {name}")
    result = await view_image_in_roots(_roots(), dict(arguments or {}), "job")
    blocks: list[types.TextContent | types.ImageContent] = []
    for item in result.get("content") or []:
        if item.get("type") == "image":
            blocks.append(
                types.ImageContent(
                    type="image", data=item["data"], mimeType=item["mimeType"]
                )
            )
        else:
            blocks.append(types.TextContent(type="text", text=str(item["text"])))
    if result.get("isError"):
        # The lowlevel server turns an exception into isError=True.
        raise ValueError(" ".join(b.text for b in blocks if b.type == "text"))
    return blocks


async def _main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(_main)
