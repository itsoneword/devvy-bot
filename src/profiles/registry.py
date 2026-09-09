"""YAML-backed profile registry (mirrors projects/registry.py)."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple

import structlog
import yaml

logger = structlog.get_logger()

# ``${VAR}`` references inside profile paths. Bare ``$VAR`` is deliberately not
# supported: it collides with literal dollars in paths and reads ambiguously.
_PATH_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# Install-root variable, always defined. Defaults to the profiles config file's
# grandparent (``<root>/config/profiles.yaml`` -> ``<root>``) so a shipped
# example resolves correctly from any checkout without anyone exporting it.
ROOT_VAR = "DEVVY_ROOT"

# Roles recognized by profile permission policies.
VALID_PROFILE_ROLES = frozenset({"owner", "guest"})
DEFAULT_ALLOWED_ROLES = frozenset({"owner"})
# Guests default to this sandboxed profile, never the registry default.
GUEST_DEFAULT_SLUG = "general"
# Per-guest workspaces live at <profile.working_dir>/guests/<user_id>.
GUESTS_DIRNAME = "guests"
# Per-user workspaces for OWNER-role users on personal_memory profiles live at
# <profile.working_dir>/users/<user_id>. Separate from guests/ because the two
# are granted for different reasons (untrusted role vs. private-by-topic) and
# reading "guests/111111111" for the bot's owner would be actively misleading.
USERS_DIRNAME = "users"


@dataclass(frozen=True)
class ProfileDefinition:
    """A single profile: persona + memory + working directory."""

    slug: str
    name: str
    working_dir: Path
    memory_dir: Path
    persona: Path
    default: bool = False
    mcp: Optional[Path] = None  # seam used by Spec B; unused in v1
    # Template copied to ``persona`` on first run when the persona file is
    # absent. Unset = look for ``<templates>/personas/<slug>.md`` by
    # convention, so adding a profile means adding one file, not two keys.
    # An explicit value is for profiles whose persona is shared with another
    # bootstrap path (``devops`` reuses ``data/templates/agent.md``).
    persona_template: Optional[Path] = None
    # Per-profile bash-sandbox override. None = inherit the global
    # SANDBOX_ENABLED default. False = full access (bash network + cross-FS
    # writes), used by the trusted `devops` admin profile. True = jailed.
    sandbox: Optional[bool] = None
    # Roles allowed to route into this profile. Parsed from the YAML
    # `permission_policy.allowed_roles` list; absent = owner-only.
    allowed_roles: FrozenSet[str] = field(default=DEFAULT_ALLOWED_ROLES)
    # Whether this profile ever participates in family-shared memory.
    # False locks every member's facts to their own private scope on this
    # profile ONLY — explicit "remember this for the family" framing is
    # still honored on other profiles, this is a per-profile override, not
    # a global family toggle. Default True preserves existing behavior.
    family_memory: bool = True
    # Per-profile agent turn-limit override. None = inherit the global
    # CLAUDE_MAX_TURNS default. The trusted `devops` profile raises this —
    # complex multi-file tasks routinely exceed the conservative default.
    max_turns: Optional[int] = None
    # Per-profile per-request SDK budget cap in USD. None = inherit the global
    # CLAUDE_MAX_COST_PER_REQUEST (which owners are exempt from); an explicit
    # value applies to EVERY role on this profile, including the owner, so a
    # cheap agent can be bounded without bounding the admin one. 0 = uncapped.
    max_cost_per_request: Optional[float] = None
    # Whether every individual user gets their OWN private memory scope on
    # this profile, even users with the "owner" role. Without this, two
    # owner-role family members (e.g. both spouses added as owners) share
    # the exact same working_dir and therefore the exact same private
    # agent_id — each sees the other's facts, not just family-shared ones.
    # True reuses the existing guest-style `{slug}#u{user_id}` scope for
    # EVERY user on this profile. Per-user is the DEFAULT (dv-0fbb): sharing
    # is a deliberate opt-in via the YAML `shared: true` key, which the
    # loader translates to personal_memory=False.
    personal_memory: bool = True
    # Which data.db tool groups this profile mounts (dv-5f3a decision 1:
    # capability config, never a hardcoded slug — any profile becomes
    # mountable by editing YAML alone). Empty = no data tools.
    user_data_tools: FrozenSet[str] = frozenset()
    # Inject the per-turn <today_state> digest for this profile (dv-df45).
    # Off by default and per-profile because the digest is domain-shaped
    # (meals, sleep, sessions) and meaningless outside a tracking profile.
    # Turning it off is the revert path — no code change, just YAML.
    today_digest: bool = False
    # Extra directories a *sandboxed* profile may write to, beyond its own
    # workspace (dv-3103): an Obsidian vault, another project checkout. Wired
    # to the CLI ``--add-dir`` (bash inside the bwrap jail) and to the
    # in-process write boundary, so the two agree. The alternative — flipping
    # ``sandbox: false`` — grants the whole filesystem plus network for one
    # extra folder; this is the narrow grant. Owner-only profiles only: the
    # loader refuses the key on a guest-allowed profile.
    extra_dirs: Tuple[Path, ...] = ()
    # Disabled profiles (dv-ffb4) stay in the YAML — persona, memory, and
    # workspace intact — but are invisible: no ``/slug`` command, no keyboard
    # button, no routing target. Toggled from the /agents Manage menu; the
    # registry is boot-only, so a toggle takes effect on restart.
    enabled: bool = True

    def allows_role(self, role: str) -> bool:
        """Owners always pass; other roles must be explicitly allowed."""
        return role == "owner" or role in self.allowed_roles


class ProfileRegistry:
    """In-memory validated profile registry."""

    def __init__(self, profiles: List[ProfileDefinition]) -> None:
        self._profiles = profiles
        self._by_slug: Dict[str, ProfileDefinition] = {p.slug: p for p in profiles}
        self._by_dir: Dict[Path, ProfileDefinition] = {
            p.working_dir.resolve(): p for p in profiles
        }

    @property
    def profiles(self) -> List[ProfileDefinition]:
        """Every profile, disabled ones included.

        For anything user-facing (keyboards, ``/slug`` commands, routing) use
        ``enabled_profiles`` — a disabled profile must stay reachable here so
        its memory scopes and workspaces still resolve for existing state.
        """
        return list(self._profiles)

    @property
    def enabled_profiles(self) -> List[ProfileDefinition]:
        return [p for p in self._profiles if p.enabled]

    def get_by_slug(self, slug: str) -> Optional[ProfileDefinition]:
        return self._by_slug.get(slug)

    def get_enabled_by_slug(self, slug: str) -> Optional[ProfileDefinition]:
        """``get_by_slug`` for switch targets: a disabled slug is unknown."""
        p = self._by_slug.get(slug)
        return p if p is not None and p.enabled else None

    def get_by_working_dir(self, path: Path) -> Optional[ProfileDefinition]:
        """Profile owning ``path`` — a profile root or a guest workspace."""
        return self.resolve_working_dir(path)[0]

    def resolve_working_dir(
        self, path: Path
    ) -> Tuple[Optional[ProfileDefinition], Optional[int]]:
        """Resolve a working dir to ``(owning profile, guest user id)``.

        Exact profile roots return ``(profile, None)``. A per-guest workspace
        ``<root>/guests/<user_id>`` returns ``(profile, user_id)`` — critical
        so sandbox resolution, persona composition, and memory scoping treat
        guest subdirs as the owning profile rather than falling back to the
        full-access default. Unknown paths return ``(None, None)``.
        """
        resolved = path.resolve()
        profile = self._by_dir.get(resolved)
        if profile is not None:
            return profile, None
        parent = resolved.parent
        if parent.name in (GUESTS_DIRNAME, USERS_DIRNAME) and resolved.name.isdigit():
            owner = self._by_dir.get(parent.parent)
            if owner is not None:
                return owner, int(resolved.name)
        return None, None

    def get_default(self) -> ProfileDefinition:
        for p in self._profiles:
            if p.default:
                return p
        return self._profiles[0]

    def get_default_for_role(self, role: str) -> Optional[ProfileDefinition]:
        """Default profile for a role.

        Owners get the registry default. Other roles prefer the sandboxed
        ``general`` profile, else the first profile that allows the role,
        else None (role has no usable profile at all).
        """
        if role == "owner":
            return self.get_default()
        general = self.get_enabled_by_slug(GUEST_DEFAULT_SLUG)
        if general is not None and general.allows_role(role):
            return general
        for p in self.enabled_profiles:
            if p.allows_role(role):
                return p
        return None


DEFAULT_AGENT_ID = "devops"


def guest_agent_id(slug: str, user_id: int) -> str:
    """Memory-scope agent_id for one guest on one profile."""
    return f"{slug}#u{user_id}"


def family_agent_id(family_id: int, slug: str) -> str:
    """Memory-scope agent_id for family-shared facts on one profile.

    Family facts live under their own agent_id (``f{family_id}:{slug}``) so no
    schema change is needed: the existing agent_id-keyed fact store gains a
    shared scope that every member of the family reads alongside their private
    scope.
    """
    return f"f{family_id}:{slug}"


def family_agent_id_for_agent(agent_id: str, family_id: int) -> str:
    """Family scope for whatever private agent_id a member uses.

    Both the owner scope (``chef``) and a guest scope (``chef#u222``) on the
    same profile map to the same family scope (``f{family_id}:chef``) — the
    single source of truth keeping the write path (evaluator) and read path
    (prompt composition, /memory) on identical family keys.
    """
    return family_agent_id(family_id, agent_id.split("#", 1)[0])


def guest_working_dir(profile: ProfileDefinition, user_id: int) -> Path:
    """Per-guest workspace under a profile root: ``<root>/guests/<user_id>``."""
    return profile.working_dir / GUESTS_DIRNAME / str(user_id)


def ensure_guest_working_dir(profile: ProfileDefinition, user_id: int) -> Path:
    """Guest workspace path, created (parents included) on first use."""
    path = guest_working_dir(profile, user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_working_dir(profile: ProfileDefinition, user_id: int) -> Path:
    """Per-user workspace under a profile root: ``<root>/users/<user_id>``."""
    return profile.working_dir / USERS_DIRNAME / str(user_id)


def user_media_root(profile: ProfileDefinition, role: str, user_id: int) -> Path:
    """Per-user directory for stored uploads — always split, even when shared.

    Unlike :func:`workspace_for`, this ignores ``personal_memory``: an owner on
    a ``shared: true`` profile works in the profile root, but their uploads
    still land in ``<root>/users/<id>``. Two reasons. Uploaded photos and
    documents are personal artifacts even when the subject matter is communal —
    a chef photo is of *your* kitchen — and ``users/``/``guests/`` is the one
    tree :class:`~src.security.read_scope.ReadScope` denies guests, so a shared
    profile's root is guest-readable while this is not (dv-9ebe).

    Still under the profile root, so it stays inside the turn's working
    directory and ``allowed_image_roots`` keeps working unchanged.
    """
    if role == "owner":
        return user_working_dir(profile, user_id)
    return guest_working_dir(profile, user_id)


def needs_own_workspace(profile: ProfileDefinition, role: str) -> bool:
    """Whether this (profile, role) pair must not share the profile root.

    Two independent reasons, and conflating them was the bug: a non-owner role
    is untrusted, while ``personal_memory`` means the profile's *subject matter*
    is private per person regardless of role. `coach` declares
    ``personal_memory: true`` ("health facts stay strictly per-user, never
    shared"), yet every owner-role user shared one directory — the memory layer
    honored the declaration and the filesystem did not.
    """
    return role != "owner" or profile.personal_memory


def workspace_for(
    profile: ProfileDefinition, role: str, user_id: Optional[int]
) -> Path:
    """The working dir this user gets on this profile (no side effects).

    Guests keep ``guests/<id>``; owners on a personal_memory profile get
    ``users/<id>``. Both resolve to the same ``{slug}#u{id}`` memory scope, so
    a role change moves someone's files but never their facts.
    """
    if user_id is None or not needs_own_workspace(profile, role):
        return profile.working_dir
    if role != "owner":
        return guest_working_dir(profile, user_id)
    return user_working_dir(profile, user_id)


def ensure_workspace_for(
    profile: ProfileDefinition, role: str, user_id: Optional[int]
) -> Path:
    """``workspace_for``, created (parents included) on first use."""
    path = workspace_for(profile, role, user_id)
    if path != profile.working_dir:
        path.mkdir(parents=True, exist_ok=True)
    return path


def agent_id_for_working_dir(
    registry: Optional[ProfileRegistry],
    working_dir: object,
    user_id: Optional[int] = None,
) -> str:
    """Resolve the memory-scope agent_id for a working dir.

    Single source of truth shared by the read path (orchestrator) and the
    write path (Claude SDK + Codex backends) so the two can never diverge —
    facts must be written under the same ``agent_id`` they are later read by.

    Profile roots map to the profile slug. Guest workspaces map to
    ``{slug}#u{user_id}`` so each guest gets a private fact store and never
    sees (or pollutes) the owner-tier profile facts. Defaults to ``devops``
    in single-profile mode or when no profile matches.

    ``user_id``, when given, additionally applies the same per-user split to
    profiles with ``personal_memory: true`` — there, EVERY user (including
    "owner"-role family members) gets ``{slug}#u{user_id}``, not just
    guests. Without ``user_id`` (or on a profile that doesn't opt in), owner
    resolution is unchanged: bare ``profile.slug``.
    """
    if not registry:
        return DEFAULT_AGENT_ID
    profile, guest_id = registry.resolve_working_dir(Path(str(working_dir)))
    if profile is None:
        return DEFAULT_AGENT_ID
    if guest_id is not None:
        return guest_agent_id(profile.slug, guest_id)
    if profile.personal_memory and user_id is not None:
        return guest_agent_id(profile.slug, user_id)
    return profile.slug


# data.db tool groups a profile may mount (dv-5f3a). Read groups ship with
# step 4; write/export groups are listed so YAML written for later steps
# validates, but tools only mount once their builders exist.
VALID_USER_DATA_TOOL_GROUPS = frozenset(
    {"logs", "products", "portions", "docs", "issues", "export"}
)


def _parse_user_data_tools(item: Dict[str, Any], slug: str) -> FrozenSet[str]:
    """Parse `user_data_tools: [logs, products, ...]` for one profile."""
    groups = item.get("user_data_tools")
    if groups is None:
        return frozenset()
    if not isinstance(groups, list) or not groups:
        raise ValueError(f"Profile '{slug}': user_data_tools must be a non-empty list")
    parsed = frozenset(str(g).strip() for g in groups)
    unknown = parsed - VALID_USER_DATA_TOOL_GROUPS
    if unknown:
        raise ValueError(
            f"Profile '{slug}': unknown user_data_tools groups:"
            f" {sorted(unknown)} (valid: {sorted(VALID_USER_DATA_TOOL_GROUPS)})"
        )
    return parsed


def _parse_allowed_roles(item: Dict[str, Any], slug: str) -> FrozenSet[str]:
    """Parse `permission_policy: {allowed_roles: [...]}` for one profile."""
    policy = item.get("permission_policy")
    if policy is None:
        return DEFAULT_ALLOWED_ROLES
    if not isinstance(policy, dict):
        raise ValueError(f"Profile '{slug}': permission_policy must be a mapping")
    roles = policy.get("allowed_roles")
    if roles is None:
        return DEFAULT_ALLOWED_ROLES
    if not isinstance(roles, list) or not roles:
        raise ValueError(
            f"Profile '{slug}': permission_policy.allowed_roles must be a"
            " non-empty list"
        )
    parsed = frozenset(str(r).strip() for r in roles)
    unknown = parsed - VALID_PROFILE_ROLES
    if unknown:
        raise ValueError(
            f"Profile '{slug}': unknown roles in permission_policy:"
            f" {sorted(unknown)} (valid: {sorted(VALID_PROFILE_ROLES)})"
        )
    return parsed


def _expand_path_vars(
    raw: str, extra: Mapping[str, str], *, slug: str, field_name: str
) -> str:
    """Substitute ``${VAR}`` from ``extra`` then the environment.

    An undefined variable is an error, not an empty string: silently expanding
    ``${WORKSPACES}/coach`` to ``/coach`` would point a profile at the
    filesystem root and only fail much later, opaquely.
    """

    def _sub(match: "re.Match[str]") -> str:
        name = match.group(1)
        value = extra.get(name)
        if value is None:
            value = os.environ.get(name)
        if value is None:
            raise ValueError(
                f"Profile '{slug}': {field_name} references undefined variable"
                f" ${{{name}}} in {raw!r} — export it, or use a path relative"
                f" to the profiles config file."
            )
        return value

    return _PATH_VAR_RE.sub(_sub, raw)


def _resolve_profile_path(
    raw: Any, *, base: Path, root: Path, slug: str, field_name: str
) -> Path:
    """Resolve one profile path: ``${VAR}`` -> ``~`` -> relative-to-config.

    Absolute paths pass through unchanged, so existing installs keep working.
    Relative paths resolve against the profiles config file's directory, which
    makes a checkout relocatable. Normalisation is lexical (``normpath``), not
    ``resolve()``: symlink following would make a profile's ``working_dir`` stop
    matching the path the rest of the system routes on.
    """
    expanded = _expand_path_vars(
        str(raw), {ROOT_VAR: str(root)}, slug=slug, field_name=field_name
    )
    path = Path(expanded).expanduser()
    if not path.is_absolute():
        path = base / path
    return Path(os.path.normpath(path))


def _validate_profile_paths(defn: "ProfileDefinition") -> None:
    """Fail at boot, naming the profile and the path.

    Without this a broken registry loads fine and the profile fails later, at
    the first turn that touches it, with an error that names neither.
    """
    errors: List[str] = []

    if not defn.persona.exists():
        errors.append(f"persona file does not exist: {defn.persona}")
    elif not defn.persona.is_file():
        errors.append(f"persona is not a file: {defn.persona}")
    elif not os.access(defn.persona, os.R_OK):
        errors.append(f"persona file is not readable: {defn.persona}")

    if not defn.working_dir.exists():
        errors.append(f"working_dir does not exist: {defn.working_dir}")
    elif not defn.working_dir.is_dir():
        errors.append(f"working_dir is not a directory: {defn.working_dir}")
    elif not os.access(defn.working_dir, os.W_OK | os.X_OK):
        errors.append(f"working_dir is not writable: {defn.working_dir}")

    # memory_dir legitimately may not exist yet -- bootstrap materialises it on
    # first run. Require only that it *can* be created.
    if defn.memory_dir.exists():
        if not defn.memory_dir.is_dir():
            errors.append(f"memory_dir is not a directory: {defn.memory_dir}")
        elif not os.access(defn.memory_dir, os.W_OK | os.X_OK):
            errors.append(f"memory_dir is not writable: {defn.memory_dir}")
    else:
        ancestor = defn.memory_dir.parent
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        if not os.access(ancestor, os.W_OK | os.X_OK):
            errors.append(
                f"memory_dir cannot be created: {defn.memory_dir}"
                f" (nearest existing parent {ancestor} is not writable)"
            )

    if defn.mcp is not None and not defn.mcp.exists():
        errors.append(f"mcp config does not exist: {defn.mcp}")

    # Extra write roots are never created for the profile: an extra dir is
    # someone else's tree (a vault, another checkout), so a typo must fail
    # here rather than quietly create an empty folder somewhere.
    for extra in defn.extra_dirs:
        if not extra.is_dir():
            errors.append(f"extra_dirs entry is not a directory: {extra}")
        elif not os.access(extra, os.W_OK | os.X_OK):
            errors.append(f"extra_dirs entry is not writable: {extra}")

    if errors:
        raise ValueError(f"Profile '{defn.slug}': " + "; ".join(errors))


def validate_registry_paths(registry: "ProfileRegistry") -> None:
    """Validate every profile's paths after the fact.

    ``load_profile_registry(validate_paths=True)` validates during parsing,
    which is right when nothing runs in between. Boot needs the other order:
    parse, let ``bootstrap_profiles`` materialise directories and personas from
    templates, *then* validate — otherwise a first run always fails on files it
    was about to create.
    """
    for defn in registry.profiles:
        _validate_profile_paths(defn)


def load_profile_registry(
    config_path: Path,
    *,
    sandbox_default: bool = True,
    validate_paths: bool = False,
) -> ProfileRegistry:
    """Load and validate profile definitions from YAML.

    Requires: non-empty `profiles` list, unique slugs, exactly one default.

    ``sandbox_default`` is the global SANDBOX_ENABLED value used to resolve
    profiles without an explicit ``sandbox`` flag. Hard invariant: a profile
    that resolves to sandbox=false (full access) can never allow guests,
    whatever the YAML says.

    Paths (``working_dir``, ``memory_dir``, ``persona``, ``mcp``) support
    ``${VAR}`` expansion — ``${DEVVY_ROOT}`` is always defined — and resolve
    relative paths against this config file's directory.

    ``validate_paths`` checks those paths exist and are usable. It is off by
    default so parsing stays a pure function; ``src/main.py`` turns it on so a
    misconfigured install fails at boot rather than mid-conversation.
    """
    if not config_path.exists():
        raise ValueError(f"Profiles config file does not exist: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("Profiles config must be a YAML object")

    raw = data.get("profiles")
    if not isinstance(raw, list) or not raw:
        raise ValueError("Profiles config must contain a non-empty 'profiles' list")

    seen_slugs: set[str] = set()
    default_count = 0
    profiles: List[ProfileDefinition] = []

    # Relative paths resolve against the config file's directory; ${DEVVY_ROOT}
    # defaults to its grandparent (<root>/config/profiles.yaml -> <root>).
    base_dir = config_path.parent.resolve()
    root_dir = Path(os.environ.get(ROOT_VAR) or base_dir.parent)

    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"Profile #{idx} must be a mapping")
        try:
            slug = str(item["slug"]).strip()
            name = str(item["name"]).strip()
            raw_working_dir = item["working_dir"]
            raw_memory_dir = item["memory_dir"]
            raw_persona = item["persona"]
        except KeyError as e:
            raise ValueError(f"Profile #{idx} missing required field: {e}") from e

        def _resolve(value: Any, field_name: str) -> Path:
            return _resolve_profile_path(
                value, base=base_dir, root=root_dir, slug=slug, field_name=field_name
            )

        working_dir = _resolve(raw_working_dir, "working_dir")
        memory_dir = _resolve(raw_memory_dir, "memory_dir")
        persona = _resolve(raw_persona, "persona")

        if not slug:
            raise ValueError(f"Profile #{idx} has empty slug")
        if slug in seen_slugs:
            raise ValueError(f"Profiles config has duplicate slug: {slug}")
        seen_slugs.add(slug)

        is_default = bool(item.get("default", False))
        if is_default:
            default_count += 1

        mcp = _resolve(item["mcp"], "mcp") if item.get("mcp") else None
        persona_template = (
            _resolve(item["persona_template"], "persona_template")
            if item.get("persona_template")
            else None
        )
        sandbox = item.get("sandbox")
        if sandbox is not None:
            sandbox = bool(sandbox)

        allowed_roles = _parse_allowed_roles(item, slug)
        # Hard security invariant (enforced in code, not config): guests may
        # only ever reach sandboxed profiles. Resolution mirrors
        # ClaudeSDKManager._sandbox_enabled_for: explicit flag wins, else the
        # global default applies.
        resolved_sandbox = sandbox if sandbox is not None else sandbox_default
        if "guest" in allowed_roles and not resolved_sandbox:
            raise ValueError(
                f"Profile '{slug}' resolves to sandbox=false (full access)"
                " and can never be guest-allowed"
            )

        extra_dirs = _parse_extra_dirs(item, slug, _resolve)
        # Extra write roots widen a sandboxed profile's reach beyond its own
        # workspace. On a guest-reachable profile that hands every guest a
        # write path into the owner's files, so the grant is owner-only —
        # the same fail-closed shape as the sandbox invariant above.
        if extra_dirs and "guest" in allowed_roles:
            raise ValueError(
                f"Profile '{slug}': extra_dirs is not allowed on a"
                " guest-allowed profile"
            )
        if extra_dirs and not resolved_sandbox:
            logger.warning(
                "extra_dirs has no effect on a full-access profile",
                profile=slug,
            )

        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"Profile '{slug}': enabled must be true or false")
        if is_default and not enabled:
            raise ValueError(f"Profile '{slug}' is the default and cannot be disabled")

        family_memory = bool(item.get("family_memory", True))
        # Per-user is the default (dv-0fbb). `shared: true` is the readable
        # opt-out spelling; `personal_memory` remains accepted for explicit
        # per-user declarations (e.g. coach). Setting both is ambiguous.
        if "shared" in item and "personal_memory" in item:
            raise ValueError(
                f"Profile '{slug}': set either 'shared' or 'personal_memory',"
                " not both"
            )
        if "personal_memory" in item:
            personal_memory = bool(item["personal_memory"])
        elif "shared" in item:
            personal_memory = not bool(item["shared"])
        else:
            personal_memory = True

        max_turns = item.get("max_turns")
        if max_turns is not None:
            if not isinstance(max_turns, int) or isinstance(max_turns, bool):
                raise ValueError(f"Profile '{slug}': max_turns must be an integer")
            if max_turns < 1:
                raise ValueError(f"Profile '{slug}': max_turns must be >= 1")

        max_cost_per_request = item.get("max_cost_per_request")
        if max_cost_per_request is not None:
            if isinstance(max_cost_per_request, bool) or not isinstance(
                max_cost_per_request, (int, float)
            ):
                raise ValueError(
                    f"Profile '{slug}': max_cost_per_request must be a number"
                )
            if max_cost_per_request < 0:
                raise ValueError(f"Profile '{slug}': max_cost_per_request must be >= 0")
            max_cost_per_request = float(max_cost_per_request)

        definition = ProfileDefinition(
            slug=slug,
            name=name,
            working_dir=working_dir,
            memory_dir=memory_dir,
            persona=persona,
            default=is_default,
            mcp=mcp,
            persona_template=persona_template,
            sandbox=sandbox,
            allowed_roles=allowed_roles,
            max_turns=max_turns,
            max_cost_per_request=max_cost_per_request,
            family_memory=family_memory,
            personal_memory=personal_memory,
            user_data_tools=_parse_user_data_tools(item, slug),
            today_digest=bool(item.get("today_digest", False)),
            extra_dirs=extra_dirs,
            enabled=enabled,
        )
        if validate_paths:
            _validate_profile_paths(definition)
        profiles.append(definition)

    if default_count != 1:
        raise ValueError(
            f"Profiles config must mark exactly one default (found {default_count})"
        )

    _validate_extra_dirs_disjoint(profiles)

    logger.info(
        "Loaded profile registry",
        count=len(profiles),
        disabled=[p.slug for p in profiles if not p.enabled],
    )
    return ProfileRegistry(profiles)


def _parse_extra_dirs(
    item: Dict[str, Any], slug: str, resolve: Any
) -> Tuple[Path, ...]:
    """Parse `extra_dirs: [/path, ...]` for one profile (dv-3103)."""
    raw = item.get("extra_dirs")
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) and x for x in raw):
        raise ValueError(f"Profile '{slug}': extra_dirs must be a list of paths")
    resolved: List[Path] = []
    for entry in raw:
        path = resolve(entry, "extra_dirs").resolve()
        if path == Path("/") or path == Path.home():
            raise ValueError(
                f"Profile '{slug}': extra_dirs entry '{entry}' is too broad"
            )
        if path not in resolved:
            resolved.append(path)
    return tuple(resolved)


def _validate_extra_dirs_disjoint(profiles: List[ProfileDefinition]) -> None:
    """An extra dir may not contain or sit inside another profile's tree.

    Otherwise ``extra_dirs: [<repo>]`` on a sandboxed profile is
    ``sandbox: false`` through the back door (it could edit the bot's own
    source), and an extra dir under another profile's workspace reads that
    profile's private memory. Resolve both sides so symlinks cannot dodge it.
    """
    for p in profiles:
        for extra in p.extra_dirs:
            for other in profiles:
                if other.slug == p.slug:
                    continue
                for label, tree in (
                    ("working_dir", other.working_dir),
                    ("memory_dir", other.memory_dir),
                ):
                    tree = tree.resolve()
                    if extra == tree or extra in tree.parents or tree in extra.parents:
                        raise ValueError(
                            f"Profile '{p.slug}': extra_dirs entry {extra} overlaps"
                            f" profile '{other.slug}' {label} {tree}"
                        )
