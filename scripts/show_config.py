#!/usr/bin/env python3
"""Print the bot's EFFECTIVE config: what the running bot uses, and from where.

``src/config/settings.py`` holds defaults only. ``.env``, the secrets file and
the per-environment overrides in ``load_config`` all sit on top, so a default
read from settings.py is often not the live value (2026-10-03: an agent quoted
the 220k window default; ``.env`` sets 150k).

It shows config only: a user's ``/model`` pick and other runtime choices
override it per turn. For the model that actually ran, query
``llm_analytics.db`` (``api_calls.model``) — see docs/configuration.md.

Usage (from the repo root, like the service's WorkingDirectory):
    poetry run python scripts/show_config.py              # values that differ from default
    poetry run python scripts/show_config.py window eval  # fields matching any substring
    poetry run python scripts/show_config.py --all        # every field
"""

import contextlib
import io
import os
import sys
from pathlib import Path

from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config.loader import load_config, secrets_file_for  # noqa: E402
from src.config.settings import Settings  # noqa: E402


def _keys(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    out = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            out.add(line.split("=", 1)[0].strip().upper())
    return out


def main(argv: list[str]) -> None:
    show_all = "--all" in argv
    filters = [a.lower() for a in argv if not a.startswith("--")]

    environ_before = set(os.environ)  # load_config exports .env into os.environ
    env_file = Path(".env")
    env_keys = _keys(env_file)
    secrets_file = secrets_file_for(env_file)
    secret_keys = _keys(secrets_file)
    with contextlib.redirect_stdout(io.StringIO()):  # loader's structlog noise
        settings = load_config()
    # Same files, no per-ENVIRONMENT overrides: anything that differs was
    # replaced by load_config's override layer, whatever .env says.
    files = tuple(p for p in (env_file, secrets_file) if p and p.exists())
    raw = Settings(_env_file=files)  # type: ignore[call-arg]

    for name, field in Settings.model_fields.items():
        if filters and not any(f in name for f in filters):
            continue
        value = getattr(settings, name)
        default = field.get_default(call_default_factory=True)
        env_name = name.upper()
        if value != getattr(raw, name):
            source = f"ENVIRONMENT override in loader.py, files say {getattr(raw, name)!r}"
        elif env_name in secret_keys:
            source = "secrets file"
        elif env_name in env_keys:
            source = ".env"
        elif env_name in environ_before:
            source = "process env"
        else:
            source = "default"
        if source == "default" and not (show_all or filters):
            continue
        if isinstance(value, SecretStr) or env_name in secret_keys:
            shown = "***" if value else "(unset)"
            print(f"{name} = {shown}  [{source}]")
            continue
        note = f"; default {default!r}" if value != default else ""
        print(f"{name} = {value!r}  [{source}{note}]")


if __name__ == "__main__":
    main(sys.argv[1:])
