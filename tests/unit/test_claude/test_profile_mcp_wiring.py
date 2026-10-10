"""Tests for per-profile MCP wiring in ClaudeSDKManager."""

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import src.channel_intel  # noqa: F401 – verify package is importable
from src.claude.sdk_integration import ClaudeSDKManager
from src.config.settings import Settings
from src.profiles.registry import ProfileDefinition, ProfileRegistry

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="test:token",
        telegram_bot_username="testbot",
        approved_directory=tmp_path,
        claude_timeout_seconds=2,
        enable_mcp=False,
    )


def _build_profile(
    slug: str,
    working_dir: Path,
    *,
    default: bool = False,
    mcp: Optional[Path] = None,
) -> ProfileDefinition:
    return ProfileDefinition(
        slug=slug,
        name=slug.capitalize(),
        working_dir=working_dir,
        memory_dir=working_dir / "memory",
        persona=working_dir / "agent.md",
        default=default,
        mcp=mcp,
    )


@pytest.fixture
def make_manager(tmp_path: Path):
    """Factory: build a ClaudeSDKManager with a stub config + ProfileRegistry.

    Usage::

        mgr = make_manager(profiles=[
            {"slug": "channel", "working_dir": ..., "mcp": ...},
            {"slug": "devops",  "working_dir": ..., "default": True},
        ])
    """

    def _factory(profiles: List[Dict[str, Any]]) -> ClaudeSDKManager:
        profile_defs = []
        for i, p in enumerate(profiles):
            slug = p["slug"]
            working_dir = Path(p["working_dir"])
            working_dir.mkdir(parents=True, exist_ok=True)
            mcp = Path(p["mcp"]) if p.get("mcp") else None
            is_default = bool(p.get("default", False))
            profile_defs.append(
                _build_profile(slug, working_dir, default=is_default, mcp=mcp)
            )

        registry = ProfileRegistry(profile_defs)
        config = _make_settings(tmp_path)
        return ClaudeSDKManager(config, profile_registry=registry)

    return _factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_mcp_servers_for_channel(tmp_path, monkeypatch, make_manager):
    """Profile with mcp seam gets channel-intel MCP config; devops gets nothing."""
    seam = tmp_path / "channel-intel.json"
    seam.write_text(
        '{"type":"http","url":"http://127.0.0.1:8765/mcp","auth_env":"CHANNEL_INTEL_TOKEN"}'
    )
    monkeypatch.setenv("CHANNEL_INTEL_TOKEN", "tkn")

    mgr = make_manager(
        profiles=[
            {"slug": "channel", "working_dir": tmp_path / "chan", "mcp": seam},
            {"slug": "devops", "working_dir": tmp_path / "dev", "default": True},
        ]
    )

    chan = mgr._mcp_servers_for(tmp_path / "chan")
    assert chan["channel-intel"]["type"] == "http"
    assert chan["channel-intel"]["url"] == "http://127.0.0.1:8765/mcp"
    # By reference: the SDK puts mcp_servers in the CLI's argv (dv-2976).
    auth = chan["channel-intel"]["headers"]["Authorization"]
    assert auth == "Bearer ${CHANNEL_INTEL_TOKEN}"
    assert "tkn" not in auth
    assert mgr._seam_env(tmp_path / "chan") == {"CHANNEL_INTEL_TOKEN": "tkn"}

    assert mgr._mcp_servers_for(tmp_path / "dev") == {}
    assert mgr._seam_env(tmp_path / "dev") == {}


def test_seam_token_comes_from_settings_not_environ(
    tmp_path, monkeypatch, make_manager
):
    """After the startup scrub the token exists only in Settings (dv-2976)."""
    from pydantic import SecretStr

    seam = tmp_path / "channel-intel.json"
    seam.write_text(
        '{"type":"http","url":"http://127.0.0.1:8765/mcp","auth_env":"CHANNEL_INTEL_TOKEN"}'
    )
    monkeypatch.delenv("CHANNEL_INTEL_TOKEN", raising=False)
    mgr = make_manager(
        profiles=[
            {"slug": "channel", "working_dir": tmp_path / "chan", "mcp": seam},
        ]
    )
    mgr.config.channel_intel_token = SecretStr("from-settings-123")
    chan = mgr._mcp_servers_for(tmp_path / "chan")
    auth = chan["channel-intel"]["headers"]["Authorization"]
    assert auth == "Bearer ${CHANNEL_INTEL_TOKEN}"
    assert mgr._seam_env(tmp_path / "chan") == {
        "CHANNEL_INTEL_TOKEN": "from-settings-123"
    }


def test_mcp_servers_no_token(tmp_path, monkeypatch, make_manager):
    """When the auth_env var is unset, headers are omitted."""
    seam = tmp_path / "channel-intel.json"
    seam.write_text(
        '{"type":"http","url":"http://127.0.0.1:8765/mcp","auth_env":"MISSING_VAR"}'
    )
    monkeypatch.delenv("MISSING_VAR", raising=False)

    mgr = make_manager(
        profiles=[
            {
                "slug": "channel",
                "working_dir": tmp_path / "chan",
                "mcp": seam,
                "default": True,
            },
        ]
    )

    result = mgr._mcp_servers_for(tmp_path / "chan")
    assert "channel-intel" in result
    assert "headers" not in result["channel-intel"]


def test_mcp_servers_unreadable_seam(tmp_path, make_manager):
    """When the seam file is missing, returns empty dict (logs warning)."""
    mgr = make_manager(
        profiles=[
            {
                "slug": "channel",
                "working_dir": tmp_path / "chan",
                "mcp": tmp_path / "nonexistent.json",
                "default": True,
            }
        ]
    )
    assert mgr._mcp_servers_for(tmp_path / "chan") == {}


def test_mcp_servers_invalid_seam(tmp_path, make_manager):
    """Malformed or unsupported seam files are ignored."""
    seam = tmp_path / "channel-intel.json"
    seam.write_text('{"type":"stdio"}')

    mgr = make_manager(
        profiles=[
            {
                "slug": "channel",
                "working_dir": tmp_path / "chan",
                "mcp": seam,
                "default": True,
            }
        ]
    )

    assert mgr._mcp_servers_for(tmp_path / "chan") == {}


def test_mcp_servers_no_registry(tmp_path):
    """Without a ProfileRegistry, always returns empty dict."""
    config = _make_settings(tmp_path)
    mgr = ClaudeSDKManager(config)
    assert mgr._mcp_servers_for(tmp_path) == {}
