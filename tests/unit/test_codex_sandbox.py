"""dv-e38e: Codex turns get a per-profile sandbox instead of danger-full-access.

Unrestricted profiles (devops) keep ``-s danger-full-access``; every other
profile, and a cwd no profile owns, runs in the confined permission profile
that hides the data-root areas, credential files and other users' workspaces.
The live behaviour of that profile is recorded in docs/FINDINGS.md 2026-09-27.
"""

import tomllib
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from src.agent_backends import codex_sandbox
from src.agent_backends.codex_cli import CodexCliIntegration, InMemoryCodexSessionStore
from src.config.settings import Settings
from src.profiles.registry import ProfileDefinition, ProfileRegistry
from src.security import denylist
from src.security.denylist import ProtectedDirs

NAME = codex_sandbox.PERMISSION_PROFILE


class Tree:
    """<home>/data/{secrets,db,workspaces}, <home>/archive, repo at <home>/repo."""

    def __init__(self, base: Path) -> None:
        home = base / "Devvybot"
        self.root = home / "data"
        self.secrets = self.root / "secrets"
        self.db = self.root / "db"
        self.archive = home / "archive"
        self.repo = home / "repo"
        for d in (self.secrets, self.db, self.archive, self.repo):
            d.mkdir(parents=True)
        ws = self.root / "workspaces"
        self.general = ws / "general"
        self.own = self.general / "users" / "111"
        self.other = self.general / "users" / "222"
        self.guest = self.general / "guests" / "333"
        self.coach_other = ws / "coach" / "users" / "222"
        self.extra = base / "vault"
        for d in (self.own, self.other, self.guest, self.coach_other, self.extra):
            d.mkdir(parents=True)
        self.dirs = ProtectedDirs(
            everyone=(self.secrets.resolve(), self.archive.resolve()),
            tenant=(self.db.resolve(),),
        )

        def profile(slug: str, wd: Path, **kw: object) -> ProfileDefinition:
            return ProfileDefinition(
                slug=slug,
                name=slug,
                working_dir=wd,
                memory_dir=wd / "memory",
                persona=wd / "persona.md",
                **kw,  # type: ignore[arg-type]
            )

        self.registry = ProfileRegistry(
            [
                profile(
                    "devops",
                    self.repo,
                    default=True,
                    sandbox=False,
                    unrestricted=True,
                ),
                profile("general", self.general, sandbox=True),
                profile("coach", ws / "coach", sandbox=True),
                profile(
                    "research",
                    ws / "research",
                    sandbox=True,
                    extra_dirs=(self.extra,),
                ),
                profile("ops2", ws / "ops2", sandbox=False),
            ]
        )


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Tree:
    t = Tree(tmp_path)
    monkeypatch.setattr(denylist, "protected_dirs", lambda: t.dirs)
    return t


@pytest.fixture
def settings() -> Settings:
    return Settings(
        telegram_bot_token="x",
        telegram_bot_username="x",
        enable_mcp=False,
        mcp_config_path=None,
    )


def _policy(args: List[str]) -> Dict[str, str]:
    """Parse the filesystem table out of the argv exactly as codex would."""
    prefix = f"permissions.{NAME}.filesystem="
    raw = next(a for a in args if a.startswith(prefix))
    return tomllib.loads("t = " + raw[len(prefix) :])["t"]


def _argv(
    tree: Tree, settings: Settings, cwd: Path, user_id: int = 111
) -> Tuple[List[str], CodexCliIntegration]:
    integ = CodexCliIntegration(
        config=settings,
        session_store=InMemoryCodexSessionStore(),
        profile_registry=tree.registry,
    )
    argv = integ._build_argv(
        model="openai-codex/gpt-5.6-sol",
        cwd=cwd,
        out_file=cwd / "o.txt",
        resume_id=None,
        user_id=user_id,
    )
    return argv, integ


class TestSelectionPerProfile:
    def test_unrestricted_devops_keeps_full_access(self, tree, settings) -> None:
        argv, _ = _argv(tree, settings, tree.repo)
        i = argv.index("-s")
        assert argv[i + 1] == "danger-full-access"
        assert not any(a.startswith("default_permissions=") for a in argv)

    @pytest.mark.parametrize("slug", ["general", "coach", "research", "ops2"])
    def test_every_other_profile_is_confined(self, tree, settings, slug) -> None:
        profile = tree.registry.get_by_slug(slug)
        argv, _ = _argv(tree, settings, profile.working_dir)
        assert "danger-full-access" not in argv
        # codex refuses sandbox_mode together with default_permissions.
        assert "-s" not in argv
        assert f'default_permissions="{NAME}"' in argv
        assert f"permissions.{NAME}.network.enabled=true" in argv

    def test_per_user_workspace_resolves_to_its_profile(self, tree, settings) -> None:
        argv, _ = _argv(tree, settings, tree.own)
        assert "danger-full-access" not in argv
        assert _policy(argv)[str(tree.own.resolve())] == "write"

    def test_unknown_cwd_fails_closed(self, tree, settings, tmp_path) -> None:
        stray = tmp_path / "stray"
        stray.mkdir()
        argv, _ = _argv(tree, settings, stray)
        assert "danger-full-access" not in argv
        assert _policy(argv)[":root"] == "read"

    def test_no_registry_fails_closed(self, settings, tmp_path) -> None:
        integ = CodexCliIntegration(
            config=settings, session_store=InMemoryCodexSessionStore()
        )
        argv = integ._build_argv(
            model="m", cwd=tmp_path, out_file=tmp_path / "o", resume_id=None
        )
        assert "danger-full-access" not in argv

    def test_sandbox_precedes_resume(self, tree, settings) -> None:
        integ = CodexCliIntegration(
            config=settings,
            session_store=InMemoryCodexSessionStore(),
            profile_registry=tree.registry,
        )
        argv = integ._build_argv(
            model="m",
            cwd=tree.own,
            out_file=tree.own / "o",
            resume_id="th-1",
            user_id=111,
        )
        assert argv[-2:] == ["resume", "th-1"]
        assert argv.index(f'default_permissions="{NAME}"') < argv.index("resume")


class TestConfinedPolicy:
    def test_sandboxed_reads_all_writes_workspace_only(self, tree, settings) -> None:
        policy = _policy(_argv(tree, settings, tree.own)[0])
        assert policy[":root"] == "read"
        assert policy[":project_roots"] == "write"
        assert policy[":slash_tmp"] == "write"
        assert policy[":tmpdir"] == "write"

    def test_hides_data_root_areas(self, tree, settings) -> None:
        policy = _policy(_argv(tree, settings, tree.own)[0])
        for area in (tree.secrets, tree.archive, tree.db):
            assert policy[str(area.resolve())] == "none"

    def test_hides_other_users_and_guests_not_own(self, tree, settings) -> None:
        policy = _policy(_argv(tree, settings, tree.own, user_id=111)[0])
        for other in (tree.other, tree.guest, tree.coach_other):
            assert policy[str(other.resolve())] == "none"
        assert str(tree.own.resolve()) not in [
            k for k, v in policy.items() if v == "none"
        ]

    def test_hides_credentials_and_legacy_files(self, tree, settings) -> None:
        policy = _policy(_argv(tree, settings, tree.own)[0])
        home = Path.home()
        for name in (".ssh", ".aws", ".claude", ".claude.json"):
            assert policy[str(home / name)] == "none"
        assert policy[str(codex_sandbox._codex_home() / "auth.json")] == "none"
        from src.utils.paths import REPO_ROOT

        assert policy[str(REPO_ROOT / ".env")] == "none"
        assert policy[str(REPO_ROOT / "data" / "*.db")] == "none"

    def test_extra_dirs_are_writable(self, tree, settings) -> None:
        research = tree.registry.get_by_slug("research")
        policy = _policy(_argv(tree, settings, research.working_dir)[0])
        assert policy[str(tree.extra.resolve())] == "write"

    def test_full_access_not_unrestricted_keeps_db_hides_secrets(
        self, tree, settings
    ) -> None:
        ops2 = tree.registry.get_by_slug("ops2")
        policy = _policy(_argv(tree, settings, ops2.working_dir)[0])
        assert policy[":root"] == "write"
        assert policy[str(tree.secrets.resolve())] == "none"
        assert str(tree.db.resolve()) not in policy

    def test_workspace_never_hidden(self, tree) -> None:
        # A missing user id must not hide the turn's own cwd (users/111).
        args = codex_sandbox.sandbox_args(
            unrestricted=False,
            full_access=False,
            cwd=tree.own,
            registry=tree.registry,
            user_id=None,
        )
        policy = _policy(args)
        assert policy[str(tree.own.resolve())] == "write"
        assert policy[str(tree.other.resolve())] == "none"


class TestMcpAutoApproval:
    """dv-c7d1: under the confined profile codex 0.157 asks before each MCP
    call and exec's approval policy is ``never``, so every bot tool call was
    rejected. Each of the bot's own servers is pre-approved; the filesystem
    confinement stays."""

    APPROVE = 'mcp_servers.{}.default_tools_approval_mode="approve"'

    def _integ(self, tree, settings, tmp_path) -> CodexCliIntegration:
        mcp_json = tmp_path / "mcp.json"
        mcp_json.write_text(
            '{"mcpServers": {"telegram": {"command": "python3", "args": ["t.py"]}}}'
        )
        settings = settings.model_copy(
            update={"enable_mcp": True, "mcp_config_path": mcp_json}
        )
        integ = CodexCliIntegration(
            config=settings,
            session_store=InMemoryCodexSessionStore(),
            profile_registry=tree.registry,
        )
        integ.attach_agent_bridge(type("B", (), {"url": "http://127.0.0.1:1/mcp"})())
        return integ

    def test_confined_turn_preapproves_every_bot_server(
        self, tree, settings, tmp_path
    ) -> None:
        integ = self._integ(tree, settings, tmp_path)
        cwd = tree.registry.get_by_slug("coach").working_dir
        argv = integ._build_argv(
            model="m", cwd=cwd, out_file=cwd / "o", resume_id="th-1", user_id=111
        )
        for name in ("agent", "telegram"):
            i = argv.index(self.APPROVE.format(name))
            assert argv[i - 1] == "-c"
            assert i < argv.index("resume")
        # Still confined: the fix must not fall back to full access.
        assert f'default_permissions="{NAME}"' in argv
        assert "danger-full-access" not in argv
        assert _policy(argv)[":root"] == "read"

    def test_each_server_approved_once_including_seam(self) -> None:
        from src.agent_backends.codex_cli import _with_mcp_auto_approval

        overrides = [
            "-c", 'mcp_servers.agent.url="u"',
            "-c", 'mcp_servers.agent.bearer_token_env_var="T"',
            "-c", 'mcp_servers.channel_intel.url="v"',
        ]  # fmt: skip
        out = _with_mcp_auto_approval(overrides)
        assert out[: len(overrides)] == overrides
        assert out[len(overrides) :] == [
            "-c", self.APPROVE.format("agent"),
            "-c", self.APPROVE.format("channel_intel"),
        ]  # fmt: skip
        # The value parses as TOML exactly as codex reads it.
        assert tomllib.loads(out[-1])["mcp_servers"]["channel_intel"] == {
            "default_tools_approval_mode": "approve"
        }

    def test_no_servers_no_approval(self, tree, settings) -> None:
        argv, _ = _argv(tree, settings, tree.own)
        assert not any("default_tools_approval_mode" in a for a in argv)
