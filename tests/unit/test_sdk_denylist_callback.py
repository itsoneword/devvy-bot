"""Tests for SDK can_use_tool in denylist mode."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.claude.sdk_integration import _make_can_use_tool_callback
from src.security.validators import SecurityValidator


class TestCanUseToolDenylistMode:
    """can_use_tool callback works when approved_directory is None."""

    def setup_method(self) -> None:
        self.validator = SecurityValidator(approved_directory=None)
        self.callback = _make_can_use_tool_callback(
            security_validator=self.validator,
            working_directory=Path("/home/testuser/project"),
            approved_directory=None,
        )

    @pytest.mark.asyncio
    async def test_allows_file_read_in_home(self) -> None:
        ctx = MagicMock()
        result = await self.callback(
            "Read",
            {"file_path": "/home/testuser/project/main.py"},
            ctx,
        )
        assert result.__class__.__name__ == "PermissionResultAllow"

    @pytest.mark.asyncio
    async def test_blocks_file_read_etc_shadow(self) -> None:
        ctx = MagicMock()
        result = await self.callback(
            "Read",
            {"file_path": "/etc/shadow"},
            ctx,
        )
        assert result.__class__.__name__ == "PermissionResultDeny"

    @pytest.mark.asyncio
    async def test_allows_bash_in_home(self) -> None:
        ctx = MagicMock()
        result = await self.callback(
            "Bash",
            {"command": "ls /home/testuser/project"},
            ctx,
        )
        assert result.__class__.__name__ == "PermissionResultAllow"

    @pytest.mark.asyncio
    async def test_blocks_bash_rm_boot(self) -> None:
        ctx = MagicMock()
        result = await self.callback(
            "Bash",
            {"command": "rm -rf /boot/"},
            ctx,
        )
        assert result.__class__.__name__ == "PermissionResultDeny"


class TestBackgroundSubagentGate:
    """Agent/Task calls that would background a subagent are denied: the CLI
    process dies at end of turn and takes the subagent with it silently."""

    def setup_method(self) -> None:
        self.validator = SecurityValidator(approved_directory=None)
        self.callback = _make_can_use_tool_callback(
            security_validator=self.validator,
            working_directory=Path("/home/testuser/project"),
            approved_directory=None,
        )

    @pytest.mark.asyncio
    async def test_explicit_background_is_denied(self) -> None:
        for tool in ("Agent", "Task"):
            result = await self.callback(
                tool,
                {"prompt": "research X", "run_in_background": True},
                MagicMock(),
            )
            assert result.__class__.__name__ == "PermissionResultDeny"
            assert "job_start" in result.message

    @pytest.mark.asyncio
    async def test_omitted_flag_is_denied_too(self) -> None:
        # Since CLI 2.1.198 background is the DEFAULT, so omitting the flag
        # means background.
        result = await self.callback("Task", {"prompt": "research X"}, MagicMock())
        assert result.__class__.__name__ == "PermissionResultDeny"

    @pytest.mark.asyncio
    async def test_foreground_subagent_is_allowed(self) -> None:
        result = await self.callback(
            "Agent",
            {"prompt": "research X", "run_in_background": False},
            MagicMock(),
        )
        assert result.__class__.__name__ == "PermissionResultAllow"

    @pytest.mark.asyncio
    async def test_unrelated_tools_unaffected(self) -> None:
        result = await self.callback(
            "Read",
            {"file_path": "/home/testuser/project/main.py"},
            MagicMock(),
        )
        assert result.__class__.__name__ == "PermissionResultAllow"


class TestCrossUserIsolation:
    """dv-6dbc: the guest-tier leak and the write boundary.

    Reproduces, as unit tests, the probes that ran live against the real DB on
    2026-08-02 (scripts/probe_isolation.py).
    """

    def setup_method(self) -> None:
        self.validator = SecurityValidator(approved_directory=None)
        self.workspace = Path(
            "/home/testuser/Devvybot/workspaces/coach/users/222222222"
        )
        # A sandboxed, guest-reachable profile: full_access=False.
        self.callback = _make_can_use_tool_callback(
            security_validator=self.validator,
            working_directory=self.workspace,
            approved_directory=None,
            full_access=False,
        )

    @pytest.mark.asyncio
    async def test_blocks_bash_sqlite_on_bot_db(self) -> None:
        """The exact command that dumped another user's memory_facts."""
        ctx = MagicMock()
        result = await self.callback(
            "Bash",
            {
                "command": (
                    'python3 -c "import sqlite3; '
                    "c=sqlite3.connect('/home/testuser/Devvybot/devvy_bot/data/bot.db'); "
                    "print(c.execute('select agent_id,count(*) from memory_facts "
                    "group by agent_id').fetchall())\""
                )
            },
            ctx,
        )
        assert result.__class__.__name__ == "PermissionResultDeny"

    @pytest.mark.asyncio
    async def test_blocks_bash_cat_dotenv(self) -> None:
        ctx = MagicMock()
        result = await self.callback(
            "Bash",
            {"command": "cat /home/testuser/Devvybot/devvy_bot/.env"},
            ctx,
        )
        assert result.__class__.__name__ == "PermissionResultDeny"

    @pytest.mark.asyncio
    async def test_blocks_read_tool_on_dotenv(self) -> None:
        ctx = MagicMock()
        result = await self.callback(
            "Read",
            {"file_path": "/home/testuser/Devvybot/devvy_bot/.env"},
            ctx,
        )
        assert result.__class__.__name__ == "PermissionResultDeny"

    @pytest.mark.asyncio
    async def test_blocks_write_outside_workspace(self) -> None:
        """`Write ~/PROBE.txt` succeeded from a sandboxed profile before this."""
        ctx = MagicMock()
        for target in (
            "/home/testuser/PROBE_SDK_WRITE.txt",
            "/home/testuser/Devvybot/devvy_bot/PROBE_SDK_WRITE.txt",
            "/home/testuser/Devvybot/workspaces/channel/PROBE_SDK_WRITE.txt",
        ):
            result = await self.callback("Write", {"file_path": target}, ctx)
            assert result.__class__.__name__ == "PermissionResultDeny", target

    @pytest.mark.asyncio
    async def test_blocks_multiedit_outside_workspace(self) -> None:
        """MultiEdit/NotebookEdit were missing from _FILE_TOOLS entirely."""
        ctx = MagicMock()
        for tool in ("MultiEdit", "NotebookEdit"):
            result = await self.callback(
                tool, {"file_path": "/home/testuser/escape.txt"}, ctx
            )
            assert result.__class__.__name__ == "PermissionResultDeny", tool

    @pytest.mark.asyncio
    async def test_allows_write_inside_workspace(self) -> None:
        ctx = MagicMock()
        result = await self.callback(
            "Write", {"file_path": str(self.workspace / "nutrition_log.md")}, ctx
        )
        assert result.__class__.__name__ == "PermissionResultAllow"

    @pytest.mark.asyncio
    async def test_allows_relative_write_inside_workspace(self) -> None:
        ctx = MagicMock()
        result = await self.callback("Write", {"file_path": "notes/today.md"}, ctx)
        assert result.__class__.__name__ == "PermissionResultAllow"

    @pytest.mark.asyncio
    async def test_allows_cross_agent_read(self) -> None:
        """Explicitly allowed by the owner decision -- do not over-tighten."""
        ctx = MagicMock()
        result = await self.callback(
            "Read",
            {"file_path": "/home/testuser/Devvybot/workspaces/channel/notes.md"},
            ctx,
        )
        assert result.__class__.__name__ == "PermissionResultAllow"

    @pytest.mark.asyncio
    async def test_allows_ordinary_bash(self) -> None:
        """No prompt storm / no false denials on everyday commands."""
        ctx = MagicMock()
        for cmd in ("ls -la", "git status", "python3 analyze.py", "grep -rn foo src/"):
            result = await self.callback("Bash", {"command": cmd}, ctx)
            assert result.__class__.__name__ == "PermissionResultAllow", cmd

    @pytest.mark.asyncio
    async def test_blocks_read_tool_on_data_db(self) -> None:
        """dv-5f3a: the per-user store is denied like bot.db — agents reach
        it only through the MCP lanes with an injected user_id."""
        ctx = MagicMock()
        for target in (
            "/home/testuser/Devvybot/devvy_bot/data/data.db",
            "/home/testuser/Devvybot/devvy_bot/data/data.db-wal",
        ):
            result = await self.callback("Read", {"file_path": target}, ctx)
            assert result.__class__.__name__ == "PermissionResultDeny", target

    @pytest.mark.asyncio
    async def test_blocks_bash_sqlite_on_data_db(self) -> None:
        ctx = MagicMock()
        result = await self.callback(
            "Bash",
            {
                "command": (
                    'python3 -c "import sqlite3; '
                    "sqlite3.connect('/home/testuser/Devvybot/devvy_bot/data/data.db')\""
                )
            },
            ctx,
        )
        assert result.__class__.__name__ == "PermissionResultDeny"


class TestFullAccessProfileStillWorks:
    """devops keeps full-server read/write with no new friction (R6)."""

    def setup_method(self) -> None:
        self.validator = SecurityValidator(approved_directory=None)
        self.callback = _make_can_use_tool_callback(
            security_validator=self.validator,
            working_directory=Path("/home/testuser/Devvybot/devvy_bot"),
            approved_directory=None,
            full_access=True,
        )

    @pytest.mark.asyncio
    async def test_allows_cross_fs_write(self) -> None:
        ctx = MagicMock()
        result = await self.callback(
            "Write", {"file_path": "/home/testuser/notes.md"}, ctx
        )
        assert result.__class__.__name__ == "PermissionResultAllow"

    @pytest.mark.asyncio
    async def test_still_blocks_dotenv(self) -> None:
        """Secrets are denied even for devops (dv-6dbc acceptance criteria)."""
        ctx = MagicMock()
        result = await self.callback(
            "Read", {"file_path": "/home/testuser/Devvybot/devvy_bot/.env"}, ctx
        )
        assert result.__class__.__name__ == "PermissionResultDeny"

    @pytest.mark.asyncio
    async def test_keeps_bot_db_access(self) -> None:
        """devops is the admin profile: DB queries are its job, not an escape.

        Blocking this would be the "new friction" R6 forbids. The cross-user
        leak is closed at the sandboxed profiles, which is where guests reach.
        """
        ctx = MagicMock()
        result = await self.callback(
            "Bash",
            {
                "command": "sqlite3 /home/testuser/Devvybot/devvy_bot/data/bot.db .tables"
            },
            ctx,
        )
        assert result.__class__.__name__ == "PermissionResultAllow"

    @pytest.mark.asyncio
    async def test_sandboxed_profile_still_blocked_from_bot_db(self) -> None:
        """The same command from a sandboxed profile stays denied."""
        callback = _make_can_use_tool_callback(
            security_validator=SecurityValidator(approved_directory=None),
            working_directory=Path("/home/testuser/Devvybot/workspaces/general"),
            approved_directory=None,
            full_access=False,
        )
        ctx = MagicMock()
        result = await callback(
            "Bash",
            {
                "command": "sqlite3 /home/testuser/Devvybot/devvy_bot/data/bot.db .tables"
            },
            ctx,
        )
        assert result.__class__.__name__ == "PermissionResultDeny"

    @pytest.mark.asyncio
    async def test_keeps_data_db_access(self) -> None:
        """dv-5f3a: data.db mirrors the bot.db rule — devops keeps access."""
        ctx = MagicMock()
        result = await self.callback(
            "Read",
            {"file_path": "/home/testuser/Devvybot/devvy_bot/data/data.db"},
            ctx,
        )
        assert result.__class__.__name__ == "PermissionResultAllow"

    @pytest.mark.asyncio
    async def test_sandboxed_profile_blocked_from_bot_db_via_read(self) -> None:
        callback = _make_can_use_tool_callback(
            security_validator=SecurityValidator(approved_directory=None),
            working_directory=Path("/home/testuser/Devvybot/workspaces/general"),
            approved_directory=None,
            full_access=False,
        )
        ctx = MagicMock()
        result = await callback(
            "Read",
            {"file_path": "/home/testuser/Devvybot/devvy_bot/data/bot.db"},
            ctx,
        )
        assert result.__class__.__name__ == "PermissionResultDeny"


class TestSandboxDisableGate:
    """A sandboxed profile must not be able to lift the OS sandbox per-call.

    Live incident 2026-08-04: coach's headless-Chrome chart render failed on
    read-only config dirs, and the model retried with
    dangerouslyDisableSandbox=true — which went through, giving a
    guest-reachable profile full FS + network for that call.
    """

    def setup_method(self) -> None:
        self.validator = SecurityValidator(approved_directory=None)
        self.workspace = Path("/home/testuser/Devvybot/workspaces/coach")

    def _callback(self, full_access: bool) -> object:
        return _make_can_use_tool_callback(
            security_validator=self.validator,
            working_directory=self.workspace,
            approved_directory=None,
            full_access=full_access,
        )

    @pytest.mark.asyncio
    async def test_sandboxed_profile_cannot_disable_sandbox(self) -> None:
        result = await self._callback(full_access=False)(
            "Bash",
            {"command": "chromium --headless ...", "dangerouslyDisableSandbox": True},
            MagicMock(),
        )
        assert result.__class__.__name__ == "PermissionResultDeny"
        assert "matplotlib" in result.message

    @pytest.mark.asyncio
    async def test_flag_false_or_absent_is_unaffected(self) -> None:
        cb = self._callback(full_access=False)
        for tool_input in (
            {"command": "ls"},
            {"command": "ls", "dangerouslyDisableSandbox": False},
        ):
            result = await cb("Bash", tool_input, MagicMock())
            assert result.__class__.__name__ == "PermissionResultAllow"

    @pytest.mark.asyncio
    async def test_full_access_profile_may_disable_sandbox(self) -> None:
        # devops runs unsandboxed anyway; the flag is a no-op there and must
        # not start tripping denials.
        result = await self._callback(full_access=True)(
            "Bash",
            {"command": "ls", "dangerouslyDisableSandbox": True},
            MagicMock(),
        )
        assert result.__class__.__name__ == "PermissionResultAllow"


class TestGuestReadScope:
    """dv-7de7: hook + callback built with a guest ReadScope enforce the
    read allowlist, the cross-user tree denial, and the search-ancestor rule.
    Owner scopes leave every existing behavior untouched."""

    GUEST_ID = 222
    OTHER_ID = 111

    def _setup(self, tmp_path: Path, role: str = "guest"):  # type: ignore[no-untyped-def]
        from src.claude.sdk_integration import _make_pre_tool_use_hook
        from src.profiles.registry import ProfileDefinition, ProfileRegistry
        from src.security.read_scope import build_read_scope

        ws = tmp_path / "workspaces"
        profiles = []
        for slug, roles in [
            ("devops", {"owner"}),
            ("channel", {"owner"}),
            ("chef", {"owner", "guest"}),
            ("coach", {"owner", "guest"}),
        ]:
            root = ws / slug
            root.mkdir(parents=True, exist_ok=True)
            profiles.append(
                ProfileDefinition(
                    slug=slug,
                    name=slug,
                    working_dir=root,
                    memory_dir=root / "memory",
                    persona=root / "persona.md",
                    default=(slug == "devops"),
                    allowed_roles=frozenset(roles),
                )
            )
        registry = ProfileRegistry(profiles)
        coach = registry.get_by_slug("coach")
        assert coach is not None
        if role == "guest":
            cwd = coach.working_dir / "guests" / str(self.GUEST_ID)
            cwd.mkdir(parents=True, exist_ok=True)
        else:
            cwd = registry.get_default().working_dir
        scope = build_read_scope(
            registry=registry,
            working_directory=cwd,
            user_id=self.GUEST_ID,
            role=role,
        )
        callback = _make_can_use_tool_callback(
            security_validator=SecurityValidator(approved_directory=None),
            working_directory=cwd,
            approved_directory=None,
            full_access=(role != "guest"),
            read_scope=scope,
        )
        hook = _make_pre_tool_use_hook(
            working_directory=cwd,
            full_access=(role != "guest"),
            read_scope=scope,
        )
        return registry, cwd, callback, hook

    @staticmethod
    def _hook_denied(decision: dict) -> bool:  # type: ignore[type-arg]
        specific = decision.get("hookSpecificOutput", {}) if decision else {}
        return specific.get("permissionDecision") == "deny"

    @pytest.mark.asyncio
    async def test_guest_read_other_user_tree_denied(self, tmp_path: Path) -> None:
        registry, _cwd, callback, hook = self._setup(tmp_path)
        coach = registry.get_by_slug("coach")
        target = str(coach.working_dir / "users" / str(self.OTHER_ID) / "x.md")
        decision = await hook(
            {"tool_name": "Read", "tool_input": {"file_path": target}}, None, None
        )
        assert self._hook_denied(decision)
        result = await callback("Read", {"file_path": target}, MagicMock())
        assert result.__class__.__name__ == "PermissionResultDeny"

    @pytest.mark.asyncio
    async def test_guest_read_owner_only_profile_denied(self, tmp_path: Path) -> None:
        registry, _cwd, callback, hook = self._setup(tmp_path)
        channel = registry.get_by_slug("channel")
        target = str(channel.working_dir / "persona.md")
        decision = await hook(
            {"tool_name": "Read", "tool_input": {"file_path": target}}, None, None
        )
        assert self._hook_denied(decision)
        result = await callback("Read", {"file_path": target}, MagicMock())
        assert result.__class__.__name__ == "PermissionResultDeny"

    @pytest.mark.asyncio
    async def test_guest_grep_profile_root_denied_via_hook(
        self, tmp_path: Path
    ) -> None:
        registry, _cwd, callback, hook = self._setup(tmp_path)
        chef = registry.get_by_slug("chef")
        tool_input = {"path": str(chef.working_dir), "pattern": "probe"}
        decision = await hook(
            {"tool_name": "Grep", "tool_input": tool_input}, None, None
        )
        assert self._hook_denied(decision)
        result = await callback("Grep", tool_input, MagicMock())
        assert result.__class__.__name__ == "PermissionResultDeny"

    @pytest.mark.asyncio
    async def test_guest_narrow_read_and_grep_allowed(self, tmp_path: Path) -> None:
        registry, cwd, callback, hook = self._setup(tmp_path)
        chef = registry.get_by_slug("chef")
        for tool, tool_input in (
            ("Read", {"file_path": str(chef.working_dir / "recipes" / "a.md")}),
            ("Grep", {"path": str(chef.working_dir / "recipes"), "pattern": "x"}),
            ("Read", {"file_path": str(cwd / "notes.md")}),
        ):
            decision = await hook(
                {"tool_name": tool, "tool_input": tool_input}, None, None
            )
            assert not self._hook_denied(decision), (tool, tool_input)
            result = await callback(tool, tool_input, MagicMock())
            assert result.__class__.__name__ == "PermissionResultAllow", tool_input

    @pytest.mark.asyncio
    async def test_guest_bash_cross_user_denied_via_hook(self, tmp_path: Path) -> None:
        registry, _cwd, _callback, hook = self._setup(tmp_path)
        coach = registry.get_by_slug("coach")
        target = coach.working_dir / "users" / str(self.OTHER_ID) / "x.md"
        decision = await hook(
            {"tool_name": "Bash", "tool_input": {"command": f"cat {target}"}},
            None,
            None,
        )
        assert self._hook_denied(decision)

    @pytest.mark.asyncio
    async def test_owner_scope_keeps_cross_agent_reads(self, tmp_path: Path) -> None:
        """Regression for dv-6dbc acceptance: cross-agent reads still work
        for owners, including reads into another user's tree (amendment
        2026-08-08: cross-user denial is guest-only)."""
        registry, _cwd, callback, hook = self._setup(tmp_path, role="owner")
        chef = registry.get_by_slug("chef")
        coach = registry.get_by_slug("coach")
        for tool, tool_input in (
            ("Read", {"file_path": str(chef.working_dir / "recipes" / "a.md")}),
            ("Grep", {"path": str(chef.working_dir), "pattern": "x"}),
            (
                "Read",
                {"file_path": str(coach.working_dir / "users" / "999" / "food.md")},
            ),
        ):
            decision = await hook(
                {"tool_name": tool, "tool_input": tool_input}, None, None
            )
            assert not self._hook_denied(decision), tool_input
            result = await callback(tool, tool_input, MagicMock())
            assert result.__class__.__name__ == "PermissionResultAllow", tool_input


class TestExtraWriteRoots:
    """dv-3103: profile extra_dirs widen the write boundary, nothing else."""

    def setup_method(self) -> None:
        self.workspace = Path("/home/testuser/Devvybot/workspaces/study")
        self.vault = Path("/home/testuser/notes")
        self.project = Path("/home/testuser/other-project")
        self.callback = _make_can_use_tool_callback(
            security_validator=SecurityValidator(approved_directory=None),
            working_directory=self.workspace,
            approved_directory=None,
            full_access=False,
            extra_write_roots=(self.vault, self.project),
        )

    @pytest.mark.asyncio
    async def test_file_write_into_extra_dir_allowed(self) -> None:
        for target in (self.vault / "study/topic.md", self.project / "src/x.py"):
            result = await self.callback(
                "Write", {"file_path": str(target)}, MagicMock()
            )
            assert result.__class__.__name__ == "PermissionResultAllow", target

    @pytest.mark.asyncio
    async def test_file_write_elsewhere_still_denied(self) -> None:
        for target in (
            "/home/testuser/PROBE.txt",
            "/home/testuser/notes-other/x.md",  # prefix, not a subtree
            "/home/testuser/Devvybot/devvy_bot/src/x.py",
        ):
            result = await self.callback("Write", {"file_path": target}, MagicMock())
            assert result.__class__.__name__ == "PermissionResultDeny", target
        assert str(self.vault) in result.message

    @pytest.mark.asyncio
    async def test_bash_write_into_extra_dir_allowed(self) -> None:
        result = await self.callback(
            "Bash",
            {"command": f"python3 build.py > {self.project}/out.log"},
            MagicMock(),
        )
        assert result.__class__.__name__ == "PermissionResultAllow"

    @pytest.mark.asyncio
    async def test_bash_write_elsewhere_still_denied(self) -> None:
        result = await self.callback(
            "Bash", {"command": "echo hi > /home/testuser/PROBE.txt"}, MagicMock()
        )
        assert result.__class__.__name__ == "PermissionResultDeny"


class _FakeConfirm:
    """Records gate prompts; answers with a fixed decision."""

    def __init__(self, approve: bool) -> None:
        self.approve = approve
        self.calls: list = []  # type: ignore[type-arg]

    async def request(self, reason: str, preview: str) -> bool:
        self.calls.append((reason, preview))
        return self.approve


# Assembled at runtime so this file's own text never looks like an order run.
# dv-8664: a bare name with no `cd` in the same command falls back to a real
# disk lookup under ~/tools/{fetchcrypto,fetchmexc} -- these names are real
# files there, so the bare spelling still gates.
_ORDER_CMD = "node " + "perp" + ".js"
_TRANSFER_CMD = "node " + "transfer" + ".js spot2fut USDT 10"


class TestConfirmationGateInHook:
    """dv-6f50: the gate lives in PreToolUse, which runs even for commands the
    CLI auto-approves via settings allow rules (can_use_tool never sees those)."""

    @staticmethod
    def _hook(tmp_path: Path, confirm: _FakeConfirm, full_access: bool = True):  # type: ignore[no-untyped-def]
        from src.claude.sdk_integration import _make_pre_tool_use_hook

        return _make_pre_tool_use_hook(
            working_directory=tmp_path,
            full_access=full_access,
            confirmation_manager=confirm,  # type: ignore[arg-type]
        )

    @staticmethod
    def _bash(command: str) -> dict:  # type: ignore[type-arg]
        return {"tool_name": "Bash", "tool_input": {"command": command}}

    @pytest.fixture(autouse=True)
    def _exchange_scripts_on_disk(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        # Bare names gate via a disk lookup under ~/tools; give a clean
        # checkout (CI, a contributor) the same files our server has.
        from src.security import destructive

        tools = tmp_path / "tools"
        (tools / "fetchcrypto").mkdir(parents=True)
        for name in ("perp", "transfer"):
            (tools / "fetchcrypto" / f"{name}.js").write_text("")
        monkeypatch.setattr(destructive, "_exchange_tools_root", lambda: tools)

    @pytest.mark.asyncio
    async def test_order_script_denied_without_approval(self, tmp_path: Path) -> None:
        confirm = _FakeConfirm(approve=False)
        out = await self._hook(tmp_path, confirm)(self._bash(_ORDER_CMD), None, None)
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert len(confirm.calls) == 1

    @pytest.mark.asyncio
    async def test_order_script_allowed_after_approval(self, tmp_path: Path) -> None:
        confirm = _FakeConfirm(approve=True)
        out = await self._hook(tmp_path, confirm)(self._bash(_TRANSFER_CMD), None, None)
        assert out == {}
        assert len(confirm.calls) == 1

    @pytest.mark.asyncio
    async def test_harmless_command_never_prompts(self, tmp_path: Path) -> None:
        confirm = _FakeConfirm(approve=False)
        out = await self._hook(tmp_path, confirm)(self._bash("ls -la"), None, None)
        assert out == {}
        assert confirm.calls == []

    @pytest.mark.asyncio
    async def test_sandboxed_profile_skips_gate(self, tmp_path: Path) -> None:
        confirm = _FakeConfirm(approve=False)
        await self._hook(tmp_path, confirm, full_access=False)(
            self._bash(_ORDER_CMD), None, None
        )
        assert confirm.calls == []
