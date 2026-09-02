"""Codex CLI backend: wraps `codex exec --json` for full agent parity."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Deque, Dict, List, Optional

import structlog

from src.claude.prompt_composer import PromptComposer
from src.claude.sdk_integration import ClaudeResponse, StreamUpdate
from src.config.settings import Settings

if TYPE_CHECKING:
    from src.claude.window_bundle import WindowBundleProvider
    from src.storage.memory_repository import MemoryRetriever

logger = structlog.get_logger()

# Per-stream buffer for the codex subprocess. Well above any single line the
# CLI is known to emit (~250KB worst case observed) so readline() on stdout
# cannot raise, while still bounding memory if the CLI ever goes haywire.
STREAM_LIMIT = 8 * 1024 * 1024


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Kill the whole process group, then reap.

    ``proc.kill()`` alone is not enough: `codex` is a Node shim that execs a
    vendored Rust binary as a child, so SIGKILL to the shim orphans the binary
    (observed 2026-08-30, still alive 30 minutes after its turn timed out).
    The child is spawned with ``start_new_session=True`` so its pgid is its own
    pid and this cannot reach the bot itself. Reaping avoids a zombie.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass


@dataclass
class CodexEventOutcome:
    """Result of interpreting one `codex exec --json` event line."""

    stream_update: Optional[StreamUpdate] = None
    thread_id: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    final_message: Optional[str] = None


def map_codex_event(event: Dict[str, Any]) -> CodexEventOutcome:
    """Map one Codex JSONL event to a CodexEventOutcome.

    Tool activity -> StreamUpdate.tool_calls (shown in the progress window).
    Agent messages are NOT streamed (would pollute the thinking window); the
    final answer is captured here as a fallback and authoritatively via -o.
    """
    etype = event.get("type")
    if etype == "thread.started":
        return CodexEventOutcome(thread_id=event.get("thread_id"))
    if etype == "turn.completed":
        return CodexEventOutcome(usage=event.get("usage") or {})
    if etype == "item.started":
        item = event.get("item") or {}
        itype = item.get("type")
        if itype == "command_execution":
            cmd = str(item.get("command", ""))
            return CodexEventOutcome(
                stream_update=StreamUpdate(
                    type="system",
                    tool_calls=[{"name": "Bash", "input": {"command": cmd}}],
                )
            )
        if itype == "file_change":
            changes = item.get("changes") or [{}]
            path = str(changes[0].get("path", ""))
            return CodexEventOutcome(
                stream_update=StreamUpdate(
                    type="system",
                    tool_calls=[{"name": "Edit", "input": {"file_path": path}}],
                )
            )
        if itype == "mcp_tool_call":
            name = str(item.get("tool") or item.get("name") or "mcp_tool")
            args = item.get("arguments") or {}
            return CodexEventOutcome(
                stream_update=StreamUpdate(
                    type="system",
                    tool_calls=[{"name": name, "input": args}],
                )
            )
        return CodexEventOutcome()
    if etype == "item.completed":
        item = event.get("item") or {}
        if item.get("type") == "agent_message":
            return CodexEventOutcome(final_message=item.get("text"))
        return CodexEventOutcome()
    return CodexEventOutcome()


class CodexSessionStore:
    """Persist Codex thread ids per (user, working_dir) as JSON.

    Mirrors the Claude session-manager's resume-by-directory behaviour so Codex
    turns continue a conversation and `/sessions` can report them. Datetimes are
    timezone-aware UTC ISO strings.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    @staticmethod
    def _key(user_id: int, working_dir: Path) -> str:
        return f"{user_id}::{Path(working_dir)}"

    def _load(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text())
                if isinstance(raw, dict):
                    self._data = raw
        except (OSError, ValueError):
            logger.warning("Failed to load Codex session store", path=str(self.path))
            self._data = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self.path)

    def record(
        self,
        user_id: int,
        working_dir: Path,
        thread_id: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        from datetime import datetime, timezone

        key = self._key(user_id, working_dir)
        entry = self._data.get(key, {"cumulative_tokens": 0})
        entry["session_id"] = thread_id
        entry["working_dir"] = str(Path(working_dir))
        entry["user_id"] = user_id
        entry["cumulative_tokens"] = (
            int(entry.get("cumulative_tokens", 0))
            + int(input_tokens)
            + int(output_tokens)
        )
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._data[key] = entry
        self._save()

    def thread_for(self, user_id: int, working_dir: Path) -> Optional[str]:
        entry = self._data.get(self._key(user_id, working_dir))
        return entry.get("session_id") if entry else None

    def info_for(self, user_id: int, working_dir: Path) -> Optional[Dict[str, Any]]:
        return self._data.get(self._key(user_id, working_dir))

    def clear(self, user_id: int, working_dir: Path) -> None:
        self._data.pop(self._key(user_id, working_dir), None)
        self._save()

    def sessions_for_user(self, user_id: int) -> List[Dict[str, Any]]:
        return [e for e in self._data.values() if e.get("user_id") == user_id]


def _strip_provider_prefix(model: str) -> str:
    return model.split("/", 1)[1] if "/" in model else model


class CodexCliIntegration:
    """Full-agent Codex backend that drives the Codex CLI (`codex exec`)."""

    def __init__(
        self,
        config: Settings,
        session_store: CodexSessionStore,
        codex_bin: str = "codex",
        prompt_composer: Optional[PromptComposer] = None,
        profile_registry: Any = None,
        memory_retriever: Optional["MemoryRetriever"] = None,
        bundle_provider: Optional["WindowBundleProvider"] = None,
    ) -> None:
        self.config = config
        self.store = session_store
        self.codex_bin = codex_bin
        self.prompt_composer = prompt_composer
        self.profile_registry = profile_registry
        self.memory_retriever = memory_retriever
        self.bundle_provider = bundle_provider
        self._profile_composers: Dict[str, PromptComposer] = {}
        self._models_by_user: Dict[int, str] = {}

    def set_user_model(self, user_id: int, model: str) -> None:
        self._models_by_user[user_id] = model

    def model_for_user(self, user_id: int) -> str:
        return self._models_by_user.get(
            user_id, self.config.agent_model or "openai-codex/gpt-5.4-mini"
        )

    def _build_argv(
        self,
        *,
        model: str,
        cwd: Path,
        out_file: Path,
        resume_id: Optional[str],
        images: Optional[List[Path]] = None,
    ) -> List[str]:
        # Exec-level options must precede the `resume` subcommand — `codex exec
        # resume` does not accept them as its own args (it rejects e.g. `-C`).
        base = [self.codex_bin, "exec"]
        base += [
            "--json",
            "-C",
            str(cwd),
            "-m",
            _strip_provider_prefix(model),
            "-s",
            "danger-full-access",
            "--skip-git-repo-check",
            "-o",
            str(out_file),
        ]
        for img in images or []:
            base += ["-i", str(img)]
        base += self._mcp_overrides(cwd)
        if resume_id:
            base += ["resume", resume_id]
        return base

    def _mcp_overrides(self, working_directory: Path) -> List[str]:
        """Return `-c` overrides registering the active profile's MCP server.

        Only profiles that declare an `mcp` seam (e.g. channel) get tools;
        devops/general get none. The seam JSON ({type:http, url, auth_env}) is
        translated to Codex `mcp_servers.<name>` config overrides. Codex reads
        the bearer token from the named env var, which the spawned process
        inherits from the bot's environment.
        """
        if not self.profile_registry:
            return []
        profile = self.profile_registry.get_by_working_dir(working_directory)
        seam_path = getattr(profile, "mcp", None) if profile else None
        if not seam_path:
            return []
        try:
            seam = json.loads(Path(seam_path).read_text())
        except (OSError, ValueError):
            logger.warning("Failed to read MCP seam", path=str(seam_path))
            return []
        if seam.get("type") != "http" or not seam.get("url"):
            return []
        name = Path(seam_path).stem.replace("-", "_")
        overrides = ["-c", f'mcp_servers.{name}.url="{seam["url"]}"']
        auth_env = seam.get("auth_env")
        if auth_env:
            overrides += [
                "-c",
                f'mcp_servers.{name}.bearer_token_env_var="{auth_env}"',
            ]
        return overrides

    async def run_command(
        self,
        prompt: str,
        working_directory: Path,
        user_id: int,
        session_id: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], Any]] = None,
        force_new: bool = False,
        images: Optional[List[Path]] = None,
        # Interface parity with ClaudeIntegration; Codex model selection
        # goes through set_user_model() instead.
        model: Optional[str] = None,
        # Interface parity; the Codex path mounts no in-process agent tools,
        # so the requester chat id is unused here.
        requester_chat_id: Optional[int] = None,
        # Requesting user's family (if any): adds the shared family fact
        # scope to persona composition, mirroring the Claude SDK path.
        family_id: Optional[int] = None,
        # Interface parity; Codex has no per-request SDK budget cap to gate.
        role: Optional[str] = None,
    ) -> ClaudeResponse:
        start = time.time()
        prompt = await self._with_persona(working_directory, prompt, family_id, user_id)

        resume_id: Optional[str] = None
        if not force_new:
            resume_id = session_id or self.store.thread_for(user_id, working_directory)

        out_file = working_directory / ".codex_last_message.txt"
        argv = self._build_argv(
            model=self.model_for_user(user_id),
            cwd=working_directory,
            out_file=out_file,
            resume_id=resume_id,
            images=images,
        )
        argv.append(prompt)

        thread_id: Optional[str] = resume_id
        usage: Dict[str, Any] = {}
        final_fallback: Optional[str] = None

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group: `codex` is a Node wrapper around a vendored
            # Rust binary, so killing the direct child leaves the binary
            # running forever. Observed 2026-08-30 -- a timed-out turn left an
            # orphan alive for 30+ minutes.
            start_new_session=True,
            # StreamReader's default line limit is 64KiB, and readline() RAISES
            # on a longer line rather than returning it. codex emits single
            # lines far past that on both streams (a models-parse error carries
            # a ~250KB body; a JSON event can carry a large tool result), so
            # the default turns a big line into a dead reader and a hung turn.
            limit=STREAM_LIMIT,
        )
        assert proc.stdout is not None and proc.stderr is not None
        # stderr MUST be drained concurrently. It is a 64KiB pipe, and the CLI
        # writes far more than that to it: a models-list parse failure dumps
        # the entire ~250KB response body, twice per turn. Reading only stdout
        # fills the stderr buffer, blocks the child mid-write, and the turn
        # hangs until agent_timeout_seconds. See docs/FINDINGS.md 2026-08-30.
        stderr_tail: Deque[str] = deque(maxlen=20)

        async def _drain_stderr() -> None:
            """Read stderr forever so the child can never block writing to it.

            Fixed-size reads, not readline(): line-oriented reading is what
            broke the first attempt at this fix. It must also never raise --
            a drainer that dies silently reintroduces the exact deadlock, and
            an un-awaited task's exception is invisible.
            """
            assert proc.stderr is not None
            pending = ""
            try:
                while True:
                    chunk = await proc.stderr.read(65536)
                    if not chunk:
                        break
                    pending += chunk.decode(errors="replace")
                    *lines, pending = pending.split("\n")
                    for line in lines:
                        if line.strip():
                            stderr_tail.append(line.strip()[:2000])
                    if len(pending) > STREAM_LIMIT:
                        pending = pending[-2000:]  # one absurd unterminated line
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception as exc:  # never let the drainer die quietly
                logger.warning("codex stderr drain stopped", error=str(exc))

        drainer = asyncio.create_task(_drain_stderr())
        try:
            while True:
                line = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=self.config.agent_timeout_seconds,
                )
                if not line:
                    break
                try:
                    event = json.loads(line.decode().strip())
                except (ValueError, UnicodeDecodeError):
                    continue
                outcome = map_codex_event(event)
                if outcome.thread_id:
                    thread_id = outcome.thread_id
                if outcome.usage:
                    usage = outcome.usage
                if outcome.final_message:
                    final_fallback = outcome.final_message
                if outcome.stream_update and on_stream:
                    await on_stream(outcome.stream_update)
            await asyncio.wait_for(
                proc.wait(), timeout=self.config.agent_timeout_seconds
            )
        except asyncio.TimeoutError:
            await _terminate(proc)
            if stderr_tail:
                logger.warning(
                    "Codex CLI timed out; stderr tail",
                    stderr="\n".join(stderr_tail),
                )
            raise RuntimeError("Codex CLI timed out.")
        finally:
            drainer.cancel()

        answer = ""
        try:
            if out_file.exists():
                answer = out_file.read_text().strip()
                out_file.unlink()
        except OSError:
            pass
        answer = answer or (final_fallback or "")

        if proc.returncode != 0 and not answer:
            err = (
                (await proc.stderr.read()).decode(errors="replace").strip()
                if proc.stderr
                else ""
            )
            raise RuntimeError(
                f"Codex CLI failed (exit {proc.returncode}): {err[-500:]}"
            )

        in_tok = int(usage.get("input_tokens", 0) or 0)
        out_tok = int(usage.get("output_tokens", 0) or 0)
        if thread_id:
            self.store.record(user_id, working_directory, thread_id, in_tok, out_tok)

        return ClaudeResponse(
            content=answer or "(No response from Codex.)",
            session_id=thread_id or "",
            cost=0.0,
            duration_ms=int((time.time() - start) * 1000),
            num_turns=1,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )

    # --- persona injection ---
    def _composer_for(self, working_directory: Path) -> Optional[PromptComposer]:
        if not self.profile_registry:
            return self.prompt_composer
        profile = self.profile_registry.get_by_working_dir(working_directory)
        if not profile:
            return self.prompt_composer
        cached = self._profile_composers.get(profile.slug)
        if cached is None:
            cached = PromptComposer(
                memory_path=profile.memory_dir,
                agent_md_path=profile.persona,
            )
            self._profile_composers[profile.slug] = cached
        return cached

    def _agent_id_for(
        self, working_directory: Path, user_id: Optional[int] = None
    ) -> str:
        """Profile slug is the memory scope key; default profile is 'devops'.

        ``user_id`` applies per-user scoping on ``personal_memory`` profiles.
        """
        from src.profiles.registry import agent_id_for_working_dir

        return agent_id_for_working_dir(
            self.profile_registry, working_directory, user_id
        )

    async def _with_persona(
        self,
        working_directory: Path,
        prompt: str,
        family_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> str:
        """Prepend the active profile's composed persona to the user prompt.

        Codex reads a project's own ``AGENTS.md`` from cwd as developer
        instructions, so the bot persona must NOT be written there: the root
        ``AGENTS.md`` is human-authored, dev-only, and no agent may clobber it.
        Injecting through the prompt each turn keeps the persona + DB-backed
        memory facts fresh without touching any project file.

        ``user_id`` resolves per-user memory scope on ``personal_memory``
        profiles, mirroring the Claude SDK path.
        """
        # Frozen per-window bundle (dv-5e2a): a codex turn in the same window
        # must inject the same frozen text as the Claude SDK path, or the
        # bundle un-freezes whenever the backend switches. Live compose below
        # is the fallback only.
        if self.bundle_provider is not None and user_id is not None:
            bundle = await self.bundle_provider.get_or_materialize(
                working_directory=working_directory,
                user_id=user_id,
                family_id=family_id,
            )
            if bundle is not None:
                return f"{bundle.text}\n\n<user_request>\n{prompt}\n</user_request>"
        composer = self._composer_for(working_directory)
        if not composer:
            return prompt
        pinned_facts: List[str] = []
        general_facts: List[str] = []
        family_facts: List[str] = []
        result_pointers: List[str] = []
        if self.memory_retriever:
            (
                pinned_facts,
                general_facts,
                result_pointers,
            ) = await self.memory_retriever.select(
                self._agent_id_for(working_directory, user_id),
                query=prompt,
            )
            if family_id is not None:
                from src.profiles.registry import family_agent_id_for_agent

                family_facts = await self.memory_retriever.select_family(
                    family_agent_id_for_agent(
                        self._agent_id_for(working_directory, user_id), family_id
                    ),
                    query=prompt,
                )
        try:
            persona = composer.compose(
                working_directory,
                pinned_facts=pinned_facts,
                general_facts=general_facts,
                family_facts=family_facts or None,
                result_pointers=result_pointers or None,
            )
        except OSError:
            logger.warning("Failed to compose persona", cwd=str(working_directory))
            return prompt
        if not persona:
            return prompt
        return f"{persona}\n\n<user_request>\n{prompt}\n</user_request>"
