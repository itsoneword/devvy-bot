"""Devvy.

A self-hosted Telegram front-end for the Claude Code and OpenAI Codex CLIs: one
owner, one machine, conversational access to the agent from anywhere.
"""

import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

# Read version from pyproject.toml when running from source (always current).
# Fall back to installed package metadata for pip installs without source tree.
_pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
try:
    with open(_pyproject, "rb") as _f:
        __version__: str = tomllib.load(_f)["project"]["version"]
except Exception:
    try:
        __version__ = _pkg_version("devvy")
    except PackageNotFoundError:
        __version__ = "0.0.0-dev"

__author__ = "itsoneword"
__license__ = "MIT"
__homepage__ = "https://github.com/itsoneword/devvy-bot"
