"""sandbox_allowed_domains / sandbox_read_dirs (dv-98f2): profile keys.

The domain list is parsed but NOT enforced (the bash box shares the network).
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.claude.sdk_integration import ClaudeSDKManager, _make_can_use_tool_callback
from src.config import create_test_config
from src.profiles.registry import (
    ProfileDefinition,
    ProfileRegistry,
    load_profile_registry,
)
from src.security.validators import SecurityValidator

_BASE = """
profiles:
  - slug: devops
    name: DevOps
    working_dir: {root}/repo
    memory_dir: {root}/repo/memory
    persona: {root}/repo/agent.md
    default: true
    sandbox: false
  - slug: markets
    name: Markets
    working_dir: {root}/ws/markets
    memory_dir: {root}/ws/markets/memory
    persona: {root}/ws/markets/persona.md
"""


def _load(tmp_path: Path, extra: str = ""):
    p = tmp_path / "profiles.yaml"
    p.write_text(_BASE.format(root=tmp_path) + extra)
    return load_profile_registry(p)


def test_parsed_lowercased_deduped(tmp_path):
    reg = _load(
        tmp_path,
        "    sandbox_allowed_domains: [API.Hyperliquid.xyz, pypi.org, pypi.org]\n",
    )
    assert reg.get_by_slug("markets").sandbox_allowed_domains == (
        "api.hyperliquid.xyz",
        "pypi.org",
    )
    assert reg.get_by_slug("devops").sandbox_allowed_domains == ()


@pytest.mark.parametrize(
    "bad", ["https://pypi.org", "pypi.org/simple", "'*'", "a b.com", "''"]
)
def test_rejects_non_hostnames(tmp_path, bad):
    with pytest.raises(ValueError, match="sandbox_allowed_domains"):
        _load(tmp_path, f"    sandbox_allowed_domains: [{bad}]\n")


def test_rejects_non_list(tmp_path):
    with pytest.raises(ValueError, match="list of hostnames"):
        _load(tmp_path, "    sandbox_allowed_domains: pypi.org\n")


def test_refused_on_guest_profile(tmp_path):
    with pytest.raises(ValueError, match="guest-allowed"):
        _load(
            tmp_path,
            "    permission_policy:\n      allowed_roles: [owner, guest]\n"
            "    sandbox_allowed_domains: [pypi.org]\n",
        )


def test_sdk_maps_profile_dirs_into_bash_box(tmp_path):
    """The box (not the dead CLI sandbox) carries the profile's directories."""
    ws = tmp_path / "markets"
    extra = tmp_path / "hlscan"
    tool = tmp_path / "fetch"
    registry = ProfileRegistry(
        [
            ProfileDefinition(
                slug="markets",
                name="Markets",
                working_dir=ws,
                memory_dir=ws / "m",
                persona=ws / "p.md",
                default=True,
                extra_dirs=(extra,),
                sandbox_read_dirs=(tool,),
                sandbox_allowed_domains=("api.hyperliquid.xyz", "pypi.org"),
            ),
        ]
    )
    mgr = ClaudeSDKManager(create_test_config(), profile_registry=registry)
    box = mgr._bash_box_for(ws)
    assert box.write_dirs == (ws.resolve(), extra)
    assert box.read_dirs == (tool,)


def test_sandbox_read_dirs_parsed_and_refused_for_guests(tmp_path):
    tool = tmp_path / "tools" / "fetch"
    tool.mkdir(parents=True)
    reg = _load(tmp_path, f"    sandbox_read_dirs: [{tool}]\n")
    assert reg.get_by_slug("markets").sandbox_read_dirs == (tool.resolve(),)
    with pytest.raises(ValueError, match="guest"):
        _load(
            tmp_path,
            f"    sandbox_read_dirs: [{tool}]\n"
            "    permission_policy:\n      allowed_roles: [owner, guest]\n",
        )


@pytest.mark.asyncio
async def test_unlisted_host_ask_is_denied():
    """The CLI asks (SandboxNetworkAccess) for hosts outside the allowlist; the
    default-allow of the callback used to let every one through."""
    cb = _make_can_use_tool_callback(
        security_validator=SecurityValidator(approved_directory=None),
        working_directory=Path("/tmp/ws"),
        approved_directory=None,
        full_access=False,
    )
    result = await cb(
        "SandboxNetworkAccess",
        {"host": "api.hyperliquid.xyz", "port": 443},
        MagicMock(),
    )
    assert result.__class__.__name__ == "PermissionResultDeny"
