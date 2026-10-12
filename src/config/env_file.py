"""The one ``.env`` writer: set keys in place, keep everything else.

Used by the installer's owner capture (``scripts/capture_owner.py``, which
runs before ``poetry install`` and loads this file by path) and by the bot
when a setting is changed from Telegram (``/voice``, dv-6b6c).

Standard library only, on purpose — see ``capture_owner.py``. Never write a
secret here: keys live in the secrets file or the tool key store.
"""

import os
import re
import tempfile
import time
from pathlib import Path
from typing import Dict


def upsert_env(text: str, key: str, value: str) -> str:
    """Set ``key`` in a .env body, preserving everything else.

    Replaces the last uncommented assignment (that is the one dotenv honours),
    or appends. A commented-out ``# KEY=`` line is left as documentation and
    the real assignment is appended below it.
    """
    pattern = re.compile(rf"^(\s*){re.escape(key)}\s*=.*$")
    lines = text.splitlines()
    target = None
    for idx, line in enumerate(lines):
        if pattern.match(line):
            target = idx
    if target is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{key}={value}")
    else:
        lines[target] = f"{key}={value}"
    return "\n".join(lines) + "\n"


def write_env(env_path: Path, values: Dict[str, str], backup: bool = True) -> Path:
    """Apply ``values`` to ``env_path`` atomically (mode 0600).

    With ``backup`` an existing file is copied to ``.bak-<ts>`` first; returns
    that backup path, or the env path itself when none was made.
    """
    env_path = Path(env_path)
    saved = env_path
    if env_path.exists():
        text = env_path.read_text(encoding="utf-8")
        if backup:
            saved = env_path.with_suffix(env_path.suffix + f".bak-{int(time.time())}")
            saved.write_text(text, encoding="utf-8")
            saved.chmod(0o600)
    else:
        text = ""
    for key, value in values.items():
        text = upsert_env(text, key, value)
    fd, tmp = tempfile.mkstemp(dir=env_path.parent, prefix=".env.tmp-", text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, env_path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return saved
