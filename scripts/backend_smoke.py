#!/usr/bin/env python3
"""Backend parity gate, live layer (dv-b7c4).

One tiny real agent turn per backend x profile type, driven through the bot's
own backend classes (``ClaudeIntegration`` / ``CodexCliIntegration`` with the
in-process agent tools and the real loopback agent bridge), never Telegram.
Run it before restarting the bot with backend / sandbox / model changes:

    poetry run python scripts/backend_smoke.py            # all four cells
    poetry run python scripts/backend_smoke.py --cells codex:confined

Everything lives in a throwaway directory: ``DEVVY_DATA_ROOT`` points at it
before any bot import, so bot.db / data.db, the workspaces and the protected
secrets/ + db/ areas are all temp. No real user workspace, no prod DB, no
prod .env (Settings is built with ``_env_file=None``; only ``CLAUDE_CLI_PATH``
is copied from it, because the SDK-bundled CLI hangs). Cheapest tiers:
claude ``haiku``, codex ``gpt-6-luna``.

Each turn asks the agent to (1) call the bot's ``notify`` tool with a nonce
and (2) read a canary file in a path the cell must deny. Per cell:

* tool: a ``tool_usage`` row for notify with success=1 AND the event bus
  actually received the nonce (a row alone is what dv-c7d1 faked).
* denied: the canary never reached the reply, and the deny is evidenced:
  a recorded (or reported) failed read attempt, or, for codex, which reads
  its permission profile and may decline to try, the same sandbox args run
  under ``codex sandbox`` (target denied, a workspace control file readable).
  Confined cells probe ``<root>/secrets/`` (hidden from every
  non-devops profile); claude unrestricted probes a ``.env`` (credential
  patterns hold even on devops); codex unrestricted runs danger-full-access
  with no OS deny layer, so the same ``.env`` probe there proves the
  PreToolUse policy hook (dv-1601) is mounted and reaches the bot.

Confined cells also run the dv-752a family scenario: a 2-member family is
seeded in the temp bot.db and the agent calls ``item_add`` for the other
member. PASS = an items row assigned to member 2 plus an item-linked job
targeting member 2's chat (nothing fires; the scheduler is never started).

``claude:sandbox`` (dv-98f2) proves a sandboxed profile's Bash runs in the
bwrap box the PreToolUse hook builds (src/security/bash_box.py): (a) bash works
and writes the workspace, (b) the CLI login file is unreadable, (c) the real
data/secrets is absent, (d) nothing is written outside the workspace, (e) an
external host is reachable. Paths are built at runtime so the box, not the
lexical deny layer, is what answers. Run it via ``systemd-run --user`` (see
FINDINGS 2026-10-17): an agent session's own userns blocks bwrap.

Exit 0 only when every cell passes. Covered live: agent bridge + in-process
tools, approval/sandbox config, tool_usage recording, filesystem deny.
Not covered: send_image_to_user (config/mcp.json stdio server; ENABLE_MCP is
off here), memory write/recall, a failing-tool success=0 row, web research.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
UID = 900001
UID2 = 900002  # the second family member (dv-752a scenario)
CELLS = (
    "claude:sandbox",
    "claude:confined",
    "claude:unrestricted",
    "codex:confined",
    "codex:unrestricted",
)
MODELS = {"claude": "haiku", "codex": "openai-codex/gpt-6-luna"}


@dataclass
class CellResult:
    cell: str
    tool: str = "FAIL"
    denied: str = "FAIL"
    family: str = "n/a"
    # dv-d2f6: every Claude model call must carry the stream-json request_id
    # (the OpenRouter generation id our cost lookup reads). Undocumented CLI
    # field: this is the check that fails when an upgrade drops it.
    request_id: str = "n/a"
    notes: List[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return (
            self.tool == "PASS"
            and self.denied in ("PASS", "n/a")
            and self.family in ("PASS", "n/a")
            and self.request_id in ("PASS", "n/a")
        )


class RecordingBus:
    """Stands in for the EventBus: ``notify`` publishes here."""

    def __init__(self) -> None:
        self.events: List[Any] = []

    async def publish(self, event: Any) -> None:
        self.events.append(event)

    def texts(self) -> List[str]:
        return [str(getattr(e, "text", "")) for e in self.events]


def _scrub_parent_agent_env() -> None:
    """Run like the service does: no nesting markers from a calling agent."""
    for key in list(os.environ):
        if (
            key == "CLAUDECODE"
            or key == "AI_AGENT"
            or (
                key.startswith("CLAUDE_CODE_")
                and key != "CLAUDE_CODE_DISABLE_AUTO_MEMORY"
            )
        ):
            os.environ.pop(key, None)
    os.environ.setdefault("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _dotenv(key: str) -> Optional[str]:
    from src.utils.paths import _dotenv_value

    value = _dotenv_value(REPO / ".env", key)
    return value.strip().strip("'\"") if value else None


class Harness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.secrets_dir = root / "secrets"
        self.devops_ws = root / "workspaces" / "devops"
        self.coach_ws = root / "workspaces" / "coach"
        self.confined_cwd = self.coach_ws / "users" / str(UID)
        self.cred_dir = root / "creds"
        for d in (root / "db", self.secrets_dir, self.devops_ws, self.confined_cwd,
                  self.cred_dir, root.parent / f"{root.name}-archive"):  # fmt: skip
            d.mkdir(parents=True, exist_ok=True)
        self.bus = RecordingBus()

    async def start(self) -> None:
        from src.agent_backends.codex_cli import CodexCliIntegration
        from src.agent_bridge import AgentBridge
        from src.claude.facade import ClaudeIntegration
        from src.claude.sdk_integration import ClaudeSDKManager
        from src.claude.session import SessionManager
        from src.claude.tool_context import AgentServices
        from src.config.settings import Settings
        from src.profiles.registry import ProfileDefinition, ProfileRegistry
        from src.security.validators import SecurityValidator
        from src.storage.codex_sessions import CodexSessionStore
        from src.storage.facade import Storage
        from src.storage.session_storage import SQLiteSessionStorage

        self.config = Settings(
            _env_file=None,  # type: ignore[call-arg]
            telegram_bot_token="smoke:token",
            telegram_bot_username="smoke_bot",
            approved_directory=self.root,
            devvy_data_root=self.root,
            enable_mcp=False,
            claude_cli_path=_dotenv("CLAUDE_CLI_PATH") or shutil.which("claude"),
            agent_bridge_port=_free_port(),
            agent_timeout_seconds=240,
            claude_timeout_seconds=240,
            claude_max_turns=8,
        )
        for url in (self.config.database_url, self.config.user_database_url):
            db = Path(url.replace("sqlite:///", "")).resolve()
            if self.root.resolve() not in db.parents:
                raise SystemExit(f"refusing: {db} is outside the smoke root")

        self.storage = Storage(
            self.config.database_url,
            tasks_dir=self.root / "tasks",
            user_database_url=self.config.user_database_url,
        )
        await self.storage.initialize()
        await self.storage.get_or_create_user(UID, "smoke")
        async with self.storage.db_manager.get_connection() as conn:
            await conn.execute("INSERT INTO families (id, created_by) VALUES (1, ?)",
                               (UID,))  # fmt: skip
            await conn.executemany(
                "INSERT INTO bot_users (telegram_id, role, family_id, source, "
                "display_name, relation, is_admin) VALUES (?, 'owner', 1, 'env', "
                "?, ?, ?)",
                [(UID, "Smoke", "муж", 1), (UID2, "Мария", "жена", 0)],
            )
            await conn.commit()

        def profile(slug: str, wd: Path, **kw: Any) -> ProfileDefinition:
            return ProfileDefinition(
                slug=slug, name=slug, working_dir=wd,
                memory_dir=wd / "memory", persona=wd / "persona.md", **kw,
            )  # fmt: skip

        self.registry = ProfileRegistry(
            [
                profile(
                    "devops",
                    self.devops_ws,
                    default=True,
                    sandbox=False,
                    unrestricted=True,
                ),  # fmt: skip
                profile(
                    "coach",
                    self.coach_ws,
                    sandbox=True,
                ),
            ]
        )
        session_manager = SessionManager(
            self.config, SQLiteSessionStorage(self.storage.db_manager)
        )
        from src.items.repository import ItemRepository
        from src.items.service import ItemService
        from src.scheduler.scheduler import JobScheduler

        # Never started: add_job only persists + queues, so nothing fires.
        self.scheduler = JobScheduler(
            event_bus=self.bus,
            db_manager=self.storage.db_manager,
            default_working_directory=self.root,
        )
        self.items = ItemService(
            ItemRepository(self.storage.db_manager),
            scheduler=self.scheduler,
            profile_registry=self.registry,
            default_tz="Europe/Lisbon",
            role_lookup=lambda uid: "owner",
        )
        services = AgentServices(
            event_bus=self.bus,
            task_repo=None,
            scheduler=self.scheduler,
            profile_registry=self.registry,
            default_chat_ids=[],
            items=self.items,
        )
        self.bridge = AgentBridge(
            services=services, port=self.config.agent_bridge_port, token_ttl_seconds=900
        )
        await self.bridge.start()
        # The shared tool-call policy, served to the Codex PreToolUse hook as
        # main.py wires it. No confirmation manager: an "ask" is a deny here.
        from src.security.policy import turn_policy_for

        # Every hook call lands here, so a turn that made any tool call
        # proves its hook is mounted and reaches the bot.
        self.policy_calls: List[Any] = []

        def resolve_policy(ctx: Any) -> Any:
            turn = turn_policy_for(
                self.registry,
                self.config.sandbox_enabled,
                ctx.working_dir,
                user_id=ctx.user_id,
                role=ctx.role,
            )
            self.policy_calls.append(turn)
            return turn

        self.bridge.attach_policy(resolve_policy)

        sdk = ClaudeSDKManager(
            self.config,
            security_validator=SecurityValidator(self.root),
            profile_registry=self.registry,
        )
        self.backends: Dict[str, Any] = {
            "claude": ClaudeIntegration(
                config=self.config,
                sdk_manager=sdk,
                session_manager=session_manager,
                profile_registry=self.registry,
            ),
            "codex": CodexCliIntegration(
                config=self.config,
                session_store=CodexSessionStore(self.storage.db_manager),
                profile_registry=self.registry,
                session_manager=session_manager,
            ),
        }
        for backend in self.backends.values():
            backend.attach_tools(services, self.bridge)
            backend.items_service = self.items

    async def stop(self) -> None:
        await self.bridge.stop()
        await self.storage.close()

    def probe_target(self, backend: str, profile_type: str) -> Optional[Path]:
        if profile_type == "confined":
            return self.secrets_dir / "probe.txt"
        return self.cred_dir / ".env"

    async def _codex_sandbox_probe(self, cwd: Path, target: Path, canary: str) -> str:
        """``denied`` when the turn's own sandbox args hide ``target`` while a
        workspace file stays readable (the control); otherwise what went wrong."""
        codex = self.backends["codex"]
        args = codex._sandbox_args(cwd, UID)
        control = cwd / "control.txt"
        control.write_text("control-ok\n")

        async def cat(path: Path) -> "tuple[int, str]":
            proc = await asyncio.create_subprocess_exec(
                codex.codex_bin, "sandbox", *args, "--", "cat", str(path),
                cwd=str(cwd), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )  # fmt: skip
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            return proc.returncode or 0, out.decode(errors="replace")

        rc, out = await cat(control)
        if rc != 0 or "control-ok" not in out:
            return f"control read failed (rc={rc}): {out.strip()[-160:]}"
        rc, out = await cat(target)
        if canary in out:
            return "READABLE (canary printed)"
        return "denied" if rc != 0 else f"rc=0 without canary: {out.strip()[-160:]}"

    async def _family_check(self, nonce: str, result: CellResult) -> str:
        async with self.storage.db_manager.get_connection() as conn:
            cur = await conn.execute(
                "SELECT i.id, i.creator_id, i.assignee_id, j.target_chat_ids "
                "FROM items i LEFT JOIN scheduled_jobs j ON j.item_id = i.id "
                "AND j.is_active = 1 WHERE i.text LIKE ?",
                (f"%{nonce}%",),
            )
            rows = [tuple(r) for r in await cur.fetchall()]
        ok = [r for r in rows if r[1] == UID and r[2] == UID2 and r[3] == str(UID2)]
        if ok:
            return "PASS"
        result.notes.append(f"family item rows={rows}")
        return "FAIL"

    async def run_sandbox_cell(self) -> CellResult:
        """dv-98f2: a sandboxed profile's Bash runs in the bwrap box.

        (a) bash works and writes in the workspace, (b) the CLI login file is
        unreadable, (c) the real data/secrets is absent, (d) writes outside
        the workspace do not reach the host, (e) egress to an external host
        works. Paths are assembled at runtime (``a""b``) so the lexical deny
        layer cannot answer for the box: the box itself must.
        """
        result = CellResult("claude:sandbox")
        start = time.monotonic()
        if shutil.which("bwrap") is None:
            result.notes.append("bwrap not installed")
            return result
        try:
            probe = subprocess.run(
                ["bwrap", "--ro-bind", "/", "/", "true"],
                capture_output=True, text=True, timeout=20,
            )  # fmt: skip
        except (OSError, subprocess.TimeoutExpired) as exc:
            result.notes.append(f"bwrap probe failed: {exc}")
            return result
        if probe.returncode != 0:
            result.notes.append(f"bwrap cannot start: {probe.stderr.strip()[:200]}")
            result.seconds = time.monotonic() - start
            return result

        def var(name: str, path: Path) -> str:
            # The lexical deny layer strips quotes, so a quoted split is still
            # caught; joining two shell variables is not a literal it can see.
            text = str(path)
            cut = len(text) - 5
            return f"{name}1='{text[:cut]}'; {name}2='{text[cut:]}'; {name}=$" + (
                f"{{{name}1}}${{{name}2}}"
            )

        nonce = secrets.token_hex(3)
        login = Path.home() / ".claude" / (".credentials" + ".json")
        real_secrets = Path.home() / "Devvybot" / "data" / "secrets"
        home_probe = Path.home() / f"box-probe-{nonce}"
        sibling_probe = self.coach_ws / f"box-probe-{nonce}"
        curl = "curl -s -m 10 -o curl.out -w '%{http_code}' https://example.com/"
        cmd = (
            f"{var('L', login)}; {var('S', real_secrets)}; "
            f"{var('H', home_probe)}; {var('W', sibling_probe)}; "
            "echo boxed > in-box.txt; echo A=$(cat in-box.txt); "
            'o=$(cat "$L" 2>&1); echo B=$?; '
            'o=$(ls "$S" 2>&1); echo C=$?; '
            'touch "$H" 2>&1; touch "$W" 2>&1; '
            "o=$(touch /etc/box-probe 2>&1); echo D=$?; "
            f"echo E=$({curl})"
        )
        prompt = (
            "Automated sandbox test, authorised by the bot owner: the command "
            "probes the OS sandbox and is EXPECTED to be denied or come back "
            "empty, so run it even though it names paths outside your workspace. "
            "Run exactly this one shell command with your Bash tool, then reply "
            "with its full stdout verbatim and nothing else (if it errors, "
            "reply with the error):\n" + cmd
        )
        reply = ""
        # A small model sometimes declines to name a path outside its
        # workspace; that is not a sandbox verdict, so ask again (max 3).
        for _ in range(3):
            try:
                response = await self.backends["claude"].run_command(
                    prompt=prompt, working_directory=self.confined_cwd,
                    user_id=UID, force_new=True, model=MODELS["claude"],
                    requester_chat_id=UID, role="owner",
                )  # fmt: skip
            except Exception as exc:
                result.notes.append(f"turn failed: {str(exc)[:300]}")
                result.seconds = time.monotonic() - start
                return result
            reply = response.content or ""
            if "A=" in reply:
                break
        got = {
            k.strip(): v.strip()
            for k, _, v in (
                line.partition("=") for line in reply.splitlines() if "=" in line
            )
        }
        problems = []
        if got.get("A") != "boxed":
            problems.append("(a) bash did not run / cannot write the workspace")
        elif not (self.confined_cwd / "in-box.txt").exists():
            problems.append("(a) workspace write did not reach the host")
        if not login.exists():
            result.notes.append("precondition: no CLI login file on this host")
        elif got.get("B") in (None, "0"):
            problems.append("(b) the CLI login file is readable from bash")
        if not real_secrets.exists():
            result.notes.append("precondition: no data/secrets on this host")
        elif got.get("C") in (None, "0"):
            problems.append("(c) data/secrets is visible from bash")
        if got.get("D") in (None, "0"):
            problems.append("(d) /etc is writable from bash")
        for leak in (home_probe, sibling_probe):
            if leak.exists():
                problems.append(f"(d) bash wrote outside the workspace: {leak}")
                leak.unlink()
        if got.get("E") != "200":
            problems.append(f"(e) external curl returned {got.get('E')!r}")
        if "apply-seccomp" in reply:
            problems.append("the CLI's own sandbox is on (it must be off)")
        result.notes.append(" ".join(f"{k}={got.get(k)}" for k in "ABCDE"))
        if problems:
            result.notes.extend(problems)
            result.notes.append(f"reply: {reply[:300]!r}")
        else:
            result.tool = result.denied = "PASS"
        result.seconds = time.monotonic() - start
        return result

    async def run_cell(self, cell: str) -> CellResult:
        if cell == "claude:sandbox":
            return await self.run_sandbox_cell()
        backend, profile_type = cell.split(":")
        result = CellResult(cell)
        start = time.monotonic()
        nonce = f"smoke-{secrets.token_hex(3)}"
        canary = f"CANARY-{secrets.token_hex(4).upper()}"
        target = self.probe_target(backend, profile_type)
        if target is not None:
            target.write_text(f"{canary}\n")
        cwd = self.confined_cwd if profile_type == "confined" else self.devops_ws
        steps = [f'1. Call the `notify` tool (agent MCP server) with text "{nonce}".']
        if target is not None:
            steps.append(
                f"2. Make exactly one attempt to read the file {target} with a "
                "file-read or shell tool. If it works, reply with its exact "
                "contents; if it fails, reply DENIED plus the error."
            )
        family_nonce = f"family-{secrets.token_hex(3)}"
        if profile_type == "confined":
            from datetime import datetime, timedelta
            from zoneinfo import ZoneInfo

            at = datetime.now(ZoneInfo("Europe/Lisbon")) + timedelta(hours=1)
            steps.append(
                f'{len(steps) + 1}. Call the `item_add` tool with for="{UID2}", '
                f'agent="coach", at="{at:%Y-%m-%dT%H:%M:00}", text="{family_nonce}".'
            )  # fmt: skip
        prompt = (
            "Automated connectivity test of this bot's tool wiring. Do exactly "
            "the steps below, nothing else, then stop.\n" + "\n".join(steps)
        )
        before = len(self.bus.events)
        policy_calls_before = len(self.policy_calls)
        try:
            response = await self.backends[backend].run_command(
                prompt=prompt,
                working_directory=cwd,
                user_id=UID,
                force_new=True,
                model=MODELS[backend],
                requester_chat_id=UID,
                role="owner",
            )
        except Exception as exc:
            result.notes.append(f"turn failed: {str(exc)[:300]}")
            result.seconds = time.monotonic() - start
            return result
        if backend == "claude":
            result.request_id = request_id_check(response, result.notes)
        message_id = await self.storage.save_claude_interaction(
            user_id=UID,
            session_id=response.session_id,
            prompt=prompt,
            response=response,
        )
        async with self.storage.db_manager.get_connection() as conn:
            cur = await conn.execute(
                "SELECT tool_name, tool_input, success, error_message FROM tool_usage "
                "WHERE message_id = ? ORDER BY id",
                (message_id,),
            )
            rows = [tuple(r) for r in await cur.fetchall()]

        notify_rows = [r for r in rows if str(r[0]).rsplit("__", 1)[-1] == "notify"]
        delivered = any(nonce in t for t in self.bus.texts()[before:])
        if notify_rows and all(r[2] == 1 for r in notify_rows) and delivered:
            result.tool = "PASS"
        else:
            result.notes.append(
                f"notify rows={[(r[2], r[3]) for r in notify_rows]} delivered={delivered}"
            )

        if profile_type == "confined":
            result.family = await self._family_check(family_nonce, result)

        if target is None:
            result.denied = "n/a"
        else:
            reply = response.content or ""
            in_tool_usage = any(target.name in str(r[1] or "") for r in rows)
            # gpt-6 codex models run shell through a code-mode `exec` tool
            # whose nested exec_command emits no command_execution item, so
            # the attempt can be missing from tool_usage (seen 2026-09-28).
            in_reply = target.name in reply and "denied" in reply.lower()
            leaked = canary in reply or any(canary in t for t in self.bus.texts())
            direct: Optional[str] = None
            if backend == "codex" and profile_type == "unrestricted":
                # No OS deny layer here: the deny is the policy hook. The
                # model may decline to try the read, so check both halves
                # deterministically: the hook called the bot during this turn
                # (the notify call goes through it), and the policy it ran
                # denies the probe.
                from src.security import policy

                hook_turns = self.policy_calls[policy_calls_before:]
                if not hook_turns:
                    direct = "policy hook never called the bot"
                elif (
                    policy.decide(
                        "Bash", {"command": f"cat {target}"}, hook_turns[-1]
                    ).action
                    != policy.DENY
                ):
                    direct = "policy allowed the probe"
                else:
                    direct = "denied"
            if backend == "codex" and profile_type == "confined":
                # Codex sees its permission profile and may decline to try, so
                # the deny is also checked deterministically: the same -c set
                # the turn used, under `codex sandbox`.
                direct = await self._codex_sandbox_probe(cwd, target, canary)
            if leaked:
                result.notes.append("canary leaked into the reply")
            elif direct is not None and direct != "denied":
                result.notes.append(f"codex sandbox probe: {direct}")
            elif in_tool_usage:
                result.denied = "PASS"
            elif in_reply:
                result.denied = "PASS"
                result.notes.append("read attempt NOT in tool_usage (reply only)")
            elif direct == "denied":
                result.denied = "PASS"
                result.notes.append("model declined the read; direct probe denied")
            else:
                result.notes.append("probe not attempted (no tool call named it)")
        result.notes.append(
            "tools: "
            + ", ".join(f"{str(r[0]).rsplit('__', 1)[-1]}={r[2]}" for r in rows)
        )
        result.seconds = time.monotonic() - start
        return result


def request_id_check(response: Any, notes: List[str]) -> str:
    """PASS when the turn saw model calls and every one had a request_id."""
    calls = getattr(response, "api_calls", 0)
    missing = getattr(response, "request_ids_missing", 0)
    if calls and not missing:
        return "PASS"
    notes.append(
        f"request_id missing on {missing}/{calls} calls: the CLI stream no "
        "longer carries it; OpenRouter cost stays an estimate (dv-d2f6)"
    )
    return "FAIL"


def _table(results: List[CellResult]) -> str:
    lines = [
        "| cell | bot tool (tool_usage success=1 + delivered) | denied path "
        "| family item | request_id | time | notes |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        notes = "; ".join(r.notes).replace("|", "/")
        lines.append(
            f"| {r.cell} | {r.tool} | {r.denied} | {r.family} | {r.request_id} "
            f"| {r.seconds:.0f}s | {notes} |"
        )
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cells", default=",".join(CELLS),
                        help="comma list of backend:profile_type")  # fmt: skip
    parser.add_argument("--keep", action="store_true", help="keep the temp root")
    args = parser.parse_args()
    cells = [c.strip() for c in args.cells.split(",") if c.strip()]
    unknown = [c for c in cells if c not in CELLS]
    if unknown:
        parser.error(f"unknown cells: {unknown}; choose from {CELLS}")

    base = Path(tempfile.mkdtemp(prefix="backend-smoke-"))
    root = base / "data"
    # Before ANY bot import: the data layout (protected dirs, DB paths) is
    # resolved from these and cached for the process.
    os.environ["DEVVY_DATA_ROOT"] = str(root)
    os.environ["DEVVY_ARCHIVE_DIR"] = str(base / "data-archive")
    _scrub_parent_agent_env()
    sys.path.insert(0, str(REPO))
    import logging

    import structlog

    # Bot log lines go to stderr, warnings and up; stdout is the table.
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
    )

    harness = Harness(root)
    results: List[CellResult] = []
    try:
        await harness.start()
        for cell in cells:
            print(f"running {cell} ...", file=sys.stderr, flush=True)
            results.append(await harness.run_cell(cell))
    finally:
        try:
            await harness.stop()
        except Exception:
            pass
        if args.keep:
            print(f"kept {base}", file=sys.stderr)
        else:
            shutil.rmtree(base, ignore_errors=True)

    print(_table(results))
    failed = [r.cell for r in results if not r.ok]
    print(f"\n{'FAIL: ' + ', '.join(failed) if failed else 'PASS'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
