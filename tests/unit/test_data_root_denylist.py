"""dv-2c1c: every security layer follows the data-root layout.

secrets/ and archive/ are denied to EVERY agent (devops included) via the
file-tool hook, Glob/Grep, Bash, and the bwrap denyRead list; db/ is denied
to sandboxed agents. Both layouts are covered: root mode (DEVVY_DATA_ROOT set)
and legacy (unset: data/model_auth, .env and *.db keep today's protection).
"""

from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

import pytest

from src.claude.monitor import check_bash_denied_paths
from src.claude.sdk_integration import _make_pre_tool_use_hook
from src.profiles.registry import (
    ProfileDefinition,
    ProfileRegistry,
    _parse_extra_dirs,
)
from src.security import denylist
from src.security.denylist import (
    DenylistValidator,
    ProtectedDirs,
    build_protected_dirs,
    sandbox_deny_read,
)
from src.security.read_scope import build_read_scope
from src.utils.paths import DataLayout, archive_dir

GUEST_ID = 222
OTHER_ID = 111

# slug, allowed roles, sandboxed
PROFILES: List[Tuple[str, frozenset, bool]] = [
    ("devops", frozenset({"owner"}), False),
    ("channel", frozenset({"owner"}), True),
    ("general", frozenset({"owner", "guest"}), True),
    ("chef", frozenset({"owner", "guest"}), True),
    ("coach", frozenset({"owner", "guest"}), True),
    ("pm", frozenset({"owner", "guest"}), True),
    ("research", frozenset({"owner"}), True),
]
SLUGS = [slug for slug, _, _ in PROFILES]


class Env:
    """A root-mode install under tmp: <home>/data/{db,workspaces,secrets},
    <home>/archive, and the repo (devops cwd) at <home>/devvy_bot."""

    def __init__(self, base: Path) -> None:
        self.home = base / "Devvybot"
        self.root = self.home / "data"
        self.repo = self.home / "devvy_bot"
        self.repo.mkdir(parents=True)
        self.secrets = self.root / "secrets"
        self.db = self.root / "db"
        self.archive = self.home / "archive"
        (self.secrets / "model_auth").mkdir(parents=True)
        self.token = self.secrets / "model_auth" / "openai_codex.json"
        self.token.write_text("{}")
        (self.secrets / "bot.env").write_text("X=1")
        self.db.mkdir(parents=True)
        self.bot_db = self.db / "bot.db"
        self.bot_db.write_text("")
        self.other_db = self.db / "scratch.sqlite"
        self.other_db.write_text("")
        (self.archive / "20260927-storage-reorg").mkdir(parents=True)
        self.archived = self.archive / "20260927-storage-reorg" / "old.md"
        self.archived.write_text("x")
        layout = DataLayout(root=self.root)
        self.dirs = build_protected_dirs(layout, archive_dir(layout, env={}))

        profiles = []
        for slug, roles, sandboxed in PROFILES:
            wd = self.repo if slug == "devops" else self.root / "workspaces" / slug
            wd.mkdir(parents=True, exist_ok=True)
            profiles.append(
                ProfileDefinition(
                    slug=slug,
                    name=slug,
                    working_dir=wd,
                    memory_dir=wd / "memory",
                    persona=wd / "persona.md",
                    default=(slug == "devops"),
                    allowed_roles=roles,
                    sandbox=sandboxed,
                )
            )
        self.registry = ProfileRegistry(profiles)

    def agent(self, slug: str) -> Tuple[Path, bool]:
        profile = self.registry.get_by_slug(slug)
        assert profile is not None
        return profile.working_dir, not profile.sandbox


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    e = Env(tmp_path)
    monkeypatch.setattr(denylist, "protected_dirs", lambda: e.dirs)
    yield e


def _denied(decision: Dict[str, Any]) -> bool:
    specific = decision.get("hookSpecificOutput", {}) if decision else {}
    return specific.get("permissionDecision") == "deny"


async def _hook(
    cwd: Path, full_access: bool, tool: str, tool_input: Dict[str, Any]
) -> Dict[str, Any]:
    hook = _make_pre_tool_use_hook(working_directory=cwd, full_access=full_access)
    return await hook({"tool_name": tool, "tool_input": tool_input}, None, None)


# --- layout -> protected dirs --------------------------------------------


class TestProtectedDirs:
    def test_root_mode(self, tmp_path: Path) -> None:
        layout = DataLayout(root=tmp_path / "Devvybot" / "data")
        dirs = build_protected_dirs(layout, archive_dir(layout, env={}))
        home = (tmp_path / "Devvybot").resolve()
        assert dirs.everyone == (home / "data" / "secrets", home / "archive")
        assert dirs.tenant == (home / "data" / "db",)

    def test_legacy_mode(self, tmp_path: Path) -> None:
        layout = DataLayout(root=None, legacy_dir=tmp_path / "repo" / "data")
        dirs = build_protected_dirs(layout, tmp_path / "archive")
        assert dirs.everyone == (
            (tmp_path / "repo" / "data" / "model_auth").resolve(),
            (tmp_path / "archive").resolve(),
        )
        # Legacy DBs share data/ with non-DB files: matched by basename.
        assert dirs.tenant == ()

    def test_archive_dir_env_override(self, tmp_path: Path) -> None:
        layout = DataLayout(root=tmp_path / "data")
        custom = archive_dir(layout, env={"DEVVY_ARCHIVE_DIR": str(tmp_path / "bin")})
        assert custom == tmp_path / "bin"

    def test_archive_dir_from_dotenv(self, tmp_path: Path) -> None:
        dotenv = tmp_path / ".env"
        dotenv.write_text(f"DEVVY_ARCHIVE_DIR={tmp_path / 'bin2'}\n")
        layout = DataLayout(root=tmp_path / "data")
        assert archive_dir(layout, env={}, dotenv=dotenv) == tmp_path / "bin2"

    def test_archive_dir_next_to_root(self, tmp_path: Path) -> None:
        layout = DataLayout(root=tmp_path / "Devvybot" / "data")
        assert archive_dir(layout, env={}, dotenv=tmp_path / "none") == (
            tmp_path / "Devvybot" / "archive"
        )

    def test_archive_dir_legacy_next_to_checkout(self, tmp_path: Path) -> None:
        from src.utils.paths import REPO_ROOT

        layout = DataLayout(root=None)
        assert archive_dir(layout, env={}, dotenv=tmp_path / "none") == (
            REPO_ROOT.parent / "archive"
        )


# --- per agent: secrets + archive denied everywhere ------------------------


@pytest.mark.parametrize("slug", SLUGS)
class TestEveryAgentDeniedSecretsAndArchive:
    async def test_read(self, env: Env, slug: str) -> None:
        cwd, full = env.agent(slug)
        for target in (env.token, env.secrets / "bot.env", env.archived):
            assert _denied(await _hook(cwd, full, "Read", {"file_path": str(target)}))

    async def test_write_and_edit(self, env: Env, slug: str) -> None:
        cwd, full = env.agent(slug)
        for tool in ("Write", "Edit"):
            decision = await _hook(cwd, full, tool, {"file_path": str(env.archived)})
            assert _denied(decision)

    async def test_glob_path(self, env: Env, slug: str) -> None:
        cwd, full = env.agent(slug)
        for root in (env.secrets, env.archive):
            decision = await _hook(
                cwd, full, "Glob", {"pattern": "**/*", "path": str(root)}
            )
            assert _denied(decision)

    async def test_glob_absolute_pattern(self, env: Env, slug: str) -> None:
        cwd, full = env.agent(slug)
        decision = await _hook(cwd, full, "Glob", {"pattern": f"{env.secrets}/**/*"})
        assert _denied(decision)

    async def test_grep_from_ancestor(self, env: Env, slug: str) -> None:
        cwd, full = env.agent(slug)
        decision = await _hook(
            cwd, full, "Grep", {"pattern": "x", "path": str(env.home)}
        )
        assert _denied(decision)

    async def test_bash_cat(self, env: Env, slug: str) -> None:
        cwd, full = env.agent(slug)
        for command in (
            f"cat {env.token}",
            f"cat {env.archived}",
            f"cp {env.secrets}/bot.env /tmp/x",
            f"cd {env.root} && cat secrets/bot.env",
            f"python3 -c \"print(open('{env.token}').read())\"",
            f"grep -r TOKEN {env.home}",
            f"tar czf /tmp/all.tgz {env.root}",
        ):
            ok, _ = check_bash_denied_paths(command, cwd, full_access=full)
            assert not ok, command
            decision = await _hook(cwd, full, "Bash", {"command": command})
            assert _denied(decision), command

    async def test_sandbox_bind_list(self, env: Env, slug: str) -> None:
        _cwd, full = env.agent(slug)
        deny = sandbox_deny_read(full)
        assert str(env.secrets.resolve()) in deny
        assert str(env.archive.resolve()) in deny

    async def test_denylist_validator_prefixes(self, env: Env, slug: str) -> None:
        validator = DenylistValidator()
        for target in (env.token, env.archived):
            allowed, _ = validator.is_path_allowed(str(target.resolve()))
            assert not allowed


# --- db/: sandboxed agents denied, devops keeps admin access ----------------


@pytest.mark.parametrize("slug", [s for s in SLUGS if s != "devops"])
class TestSandboxedAgentsDeniedDb:
    async def test_read_any_db_file(self, env: Env, slug: str) -> None:
        cwd, full = env.agent(slug)
        assert not full
        for target in (env.bot_db, env.other_db):
            assert _denied(await _hook(cwd, full, "Read", {"file_path": str(target)}))

    async def test_glob_db_dir(self, env: Env, slug: str) -> None:
        cwd, full = env.agent(slug)
        decision = await _hook(cwd, full, "Glob", {"pattern": "*", "path": str(env.db)})
        assert _denied(decision)

    async def test_bash_sqlite(self, env: Env, slug: str) -> None:
        cwd, full = env.agent(slug)
        ok, _ = check_bash_denied_paths(
            f"sqlite3 {env.other_db} .dump", cwd, full_access=full
        )
        assert not ok

    async def test_sandbox_bind_list_hides_db(self, env: Env, slug: str) -> None:
        _cwd, full = env.agent(slug)
        assert str(env.db.resolve()) in sandbox_deny_read(full)


class TestDevopsKeepsDb:
    async def test_devops_reads_db(self, env: Env) -> None:
        cwd, full = env.agent("devops")
        assert full
        decision = await _hook(cwd, full, "Read", {"file_path": str(env.other_db)})
        assert not _denied(decision)
        ok, _ = check_bash_denied_paths(f"sqlite3 {env.bot_db} .tables", cwd, True)
        assert ok
        assert str(env.db.resolve()) not in sandbox_deny_read(True)

    async def test_devops_everyday_commands_still_allowed(self, env: Env) -> None:
        cwd, full = env.agent("devops")
        for command in (
            "grep -rn foo src/",
            "cp README.md /tmp/x",
            f"ls {env.home}",
            f"cat {env.root}/workspaces/coach/persona.md",
        ):
            ok, error = check_bash_denied_paths(command, cwd, full_access=full)
            assert ok, (command, error)
        decision = await _hook(cwd, full, "Grep", {"pattern": "x", "path": str(cwd)})
        assert not _denied(decision)


# --- guest scope on the new workspace paths --------------------------------


class TestGuestScopeNewLayout:
    def test_guest_scope_on_root_workspaces(self, env: Env) -> None:
        coach = env.registry.get_by_slug("coach")
        assert coach is not None
        cwd = coach.working_dir / "guests" / str(GUEST_ID)
        cwd.mkdir(parents=True)
        other = coach.working_dir / "users" / str(OTHER_ID)
        other.mkdir(parents=True)
        scope = build_read_scope(env.registry, cwd, user_id=GUEST_ID, role="guest")
        assert scope.is_guest
        assert scope.check_read(cwd / "notes.md")[0]
        chef = env.registry.get_by_slug("chef")
        assert chef is not None
        assert scope.check_read(chef.working_dir / "persona.md")[0]
        assert not scope.check_read(other / "memory.md")[0]
        assert not scope.check_read(env.token)[0]
        assert not scope.check_read(env.bot_db)[0]

    async def test_guest_hook_denies_areas(self, env: Env) -> None:
        coach = env.registry.get_by_slug("coach")
        assert coach is not None
        cwd = coach.working_dir / "guests" / str(GUEST_ID)
        cwd.mkdir(parents=True)
        scope = build_read_scope(env.registry, cwd, user_id=GUEST_ID, role="guest")
        hook = _make_pre_tool_use_hook(cwd, full_access=False, read_scope=scope)
        for target in (env.token, env.archived, env.bot_db):
            decision = await hook(
                {"tool_name": "Read", "tool_input": {"file_path": str(target)}},
                None,
                None,
            )
            assert _denied(decision)
        ok = await hook(
            {"tool_name": "Read", "tool_input": {"file_path": str(cwd / "a.md")}},
            None,
            None,
        )
        assert not _denied(ok)


# --- legacy layout keeps today's protections --------------------------------


@pytest.fixture
def legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Dict[str, Path]]:
    repo = tmp_path / "Devvybot" / "devvy_bot"
    data = repo / "data"
    (data / "model_auth").mkdir(parents=True)
    token = data / "model_auth" / "openai_codex.json"
    token.write_text("{}")
    (data / "bot.db").write_text("")
    (repo / ".env").write_text("X=1")
    archive = tmp_path / "Devvybot" / "archive"
    archive.mkdir(parents=True)
    layout = DataLayout(root=None, legacy_dir=data)
    dirs = build_protected_dirs(layout, archive)
    monkeypatch.setattr(denylist, "protected_dirs", lambda: dirs)
    ws = tmp_path / "Devvybot" / "workspaces" / "coach"
    ws.mkdir(parents=True)
    yield {"repo": repo, "token": token, "archive": archive, "ws": ws, "data": data}


class TestLegacyLayout:
    @pytest.mark.parametrize("who", ["devops", "coach"])
    async def test_env_token_archive_denied(
        self, legacy: Dict[str, Path], who: str
    ) -> None:
        cwd, full = (legacy["repo"], True) if who == "devops" else (legacy["ws"], False)
        for target in (legacy["repo"] / ".env", legacy["token"], legacy["archive"]):
            assert _denied(await _hook(cwd, full, "Read", {"file_path": str(target)}))
        for command in (
            f"cat {legacy['repo']}/.env",
            f"cat {legacy['token']}",
            f"ls {legacy['archive']}",
        ):
            ok, _ = check_bash_denied_paths(command, cwd, full_access=full)
            assert not ok, command

    async def test_devops_relative_token_denied(self, legacy: Dict[str, Path]) -> None:
        ok, _ = check_bash_denied_paths(
            "cat data/model_auth/openai_codex.json", legacy["repo"], True
        )
        assert not ok

    async def test_db_by_basename(self, legacy: Dict[str, Path]) -> None:
        db = legacy["data"] / "bot.db"
        assert _denied(await _hook(legacy["ws"], False, "Read", {"file_path": str(db)}))
        assert not _denied(
            await _hook(legacy["repo"], True, "Read", {"file_path": str(db)})
        )

    async def test_devops_search_from_repo_still_allowed(
        self, legacy: Dict[str, Path]
    ) -> None:
        """model_auth sits inside the devops cwd in legacy mode: searching the
        repo must keep working (the in-cwd exemption)."""
        repo = legacy["repo"]
        assert not _denied(
            await _hook(repo, True, "Grep", {"pattern": "x", "path": "."})
        )
        ok, _ = check_bash_denied_paths("grep -rn foo .", repo, True)
        assert ok

    def test_sandbox_list_only_existing_dirs(self, legacy: Dict[str, Path]) -> None:
        deny = sandbox_deny_read(False)
        assert deny == [
            str((legacy["data"] / "model_auth").resolve()),
            str(legacy["archive"].resolve()),
        ]


# --- other consumers --------------------------------------------------------


class TestOtherLayers:
    def test_extra_dirs_cannot_overlap_areas(self, env: Env) -> None:
        def resolve(entry: str, _key: str) -> Path:
            return Path(entry)

        for bad in (env.secrets, env.archive / "x", env.root, env.db):
            with pytest.raises(ValueError, match="no agent may read"):
                _parse_extra_dirs({"extra_dirs": [str(bad)]}, "research", resolve)
        ok = _parse_extra_dirs(
            {"extra_dirs": [str(env.root / "workspaces" / "shared")]},
            "research",
            resolve,
        )
        assert len(ok) == 1

    def test_image_in_secrets_not_deliverable(self, env: Env) -> None:
        from src.bot.utils.image_extractor import validate_image_path

        img = env.secrets / "qr.png"
        img.write_bytes(b"\x89PNG")
        assert validate_image_path(str(img), [env.home]) is None
        fine = env.root / "workspaces" / "coach" / "chart.png"
        fine.write_bytes(b"\x89PNG")
        assert validate_image_path(str(fine), [env.home]) is not None

    def test_self_modify_secrets_need_owner(self, env: Env) -> None:
        from src.bot.features.self_modify import SelfModificationGuard

        guard = SelfModificationGuard(bot_root=env.repo)
        assert guard.get_tier(str(env.token)) == 2
        assert guard.get_tier(str(env.repo / ".env")) == 2
        assert guard.get_tier(str(env.root / "workspaces" / "coach" / "a.md")) == 0

    def test_protected_dirs_type(self, env: Env) -> None:
        assert isinstance(denylist.protected_dirs(), ProtectedDirs)


# --- unrestricted profile flag (dv-e38e) -----------------------------------


async def _hook_u(cwd: Path, tool: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    hook = _make_pre_tool_use_hook(
        working_directory=cwd, full_access=True, unrestricted=True
    )
    return await hook({"tool_name": tool, "tool_input": tool_input}, None, None)


class TestUnrestrictedProfile:
    """devops with `unrestricted: true` is the server admin: no data-root
    denies, but the baseline credential rules still hold."""

    async def test_file_tools_reach_areas(self, env: Env) -> None:
        cwd, _ = env.agent("devops")
        for target in (env.token, env.secrets / "bot.env", env.archived, env.bot_db):
            decision = await _hook_u(cwd, "Read", {"file_path": str(target)})
            assert not _denied(decision), target
        for root in (env.secrets, env.archive, env.home):
            decision = await _hook_u(cwd, "Grep", {"pattern": "x", "path": str(root)})
            assert not _denied(decision), root
        decision = await _hook_u(cwd, "Glob", {"pattern": f"{env.secrets}/**/*"})
        assert not _denied(decision)

    async def test_bash_reaches_areas(self, env: Env) -> None:
        cwd, _ = env.agent("devops")
        for command in (
            f"cat {env.token}",
            f"cat {env.archived}",
            f"cd {env.root} && cat secrets/bot.env",
            f"grep -r TOKEN {env.home}",
            f"find {env.home} -name '*.md'",
            f"tar czf /tmp/all.tgz {env.root}",
        ):
            ok, error = check_bash_denied_paths(
                command, cwd, full_access=True, unrestricted=True
            )
            assert ok, (command, error)
            assert not _denied(await _hook_u(cwd, "Bash", {"command": command}))

    async def test_baseline_credentials_still_denied(self, env: Env) -> None:
        cwd, _ = env.agent("devops")
        dotenv = env.repo / ".env"
        for target in (dotenv, Path.home() / ".ssh" / "id_rsa"):
            decision = await _hook_u(cwd, "Read", {"file_path": str(target)})
            assert _denied(decision), target
        for command in (f"cat {dotenv}", "cat ~/.ssh/id_ed25519"):
            ok, _ = check_bash_denied_paths(
                command, cwd, full_access=True, unrestricted=True
            )
            assert not ok, command

    def test_validators_and_sandbox_list(self, env: Env) -> None:
        validator = DenylistValidator()
        target = str(env.token.resolve())
        assert not validator.is_path_allowed(target)[0]
        assert validator.is_path_allowed(target, unrestricted=True)[0]
        assert not validator.is_path_allowed(str(env.repo / ".env"), unrestricted=True)[
            0
        ]
        assert sandbox_deny_read(True, unrestricted=True) == []

    async def test_flag_off_keeps_dv_2c1c(self, env: Env) -> None:
        cwd, full = env.agent("devops")
        assert _denied(await _hook(cwd, full, "Read", {"file_path": str(env.token)}))


class TestUnrestrictedRegistry:
    def _load(self, tmp_path: Path, extra: str) -> Any:
        from src.profiles.registry import load_profile_registry

        cfg = tmp_path / "profiles.yaml"
        cfg.write_text(
            "profiles:\n"
            "  - slug: devops\n"
            "    name: Ops\n"
            f"    working_dir: {tmp_path}\n"
            f"    memory_dir: {tmp_path}/m\n"
            f"    persona: {tmp_path}/p.md\n"
            "    default: true\n" + extra
        )
        return load_profile_registry(cfg, data_root=None)

    def test_parsed(self, tmp_path: Path) -> None:
        reg = self._load(tmp_path, "    sandbox: false\n    unrestricted: true\n")
        assert reg.get_by_slug("devops").unrestricted is True

    def test_default_false(self, tmp_path: Path) -> None:
        reg = self._load(tmp_path, "    sandbox: false\n")
        assert reg.get_by_slug("devops").unrestricted is False

    def test_requires_sandbox_off(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="requires sandbox: false"):
            self._load(tmp_path, "    unrestricted: true\n")

    def test_never_guest_allowed(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            self._load(
                tmp_path,
                "    sandbox: false\n    unrestricted: true\n"
                "    permission_policy:\n      allowed_roles: [owner, guest]\n",
            )

    def test_shipped_devops_template_is_unrestricted(self) -> None:
        from src.utils.paths import REPO_ROOT

        for path in (
            REPO_ROOT / "data/templates/profiles/devops/profile.yaml",
            REPO_ROOT / "config/profiles.example.yaml",
        ):
            assert "unrestricted: true" in path.read_text(), path
