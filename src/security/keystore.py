"""Tool key store (dv-0202): API keys for the owner's tools, one place.

Layout: ``<root>/secrets/tools/<service>.env`` (KEY=VALUE, mode 0600). It sits
under ``<root>/secrets``, which the denylist already closes to every agent
(``DataLayout.secret_dirs``), so an agent cannot Read it.

Keys enter only through the ``/setkey`` bot command, handled by bot code
before any model sees the message, and leave only as subprocess env for the
tool that needs them (``ExchangeClient``, ``scripts/withkeys``). Nothing here
returns a value to a caller that prints it; ``mask`` is the only display form.
"""

import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MIN_REDACT = 6


class KeyStoreError(ValueError):
    """Bad service / key name / value. Message is safe to show."""


def mask(value: str) -> str:
    """``abcd…wxyz``-style tail only; short values are fully hidden."""
    return f"…{value[-4:]}" if len(value) >= 12 else "…"


def _parse(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out


class KeyStore:
    """Read/write ``<dir>/<service>.env`` files atomically at 0600."""

    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)

    def _path(self, service: str) -> Path:
        if not SERVICE_RE.match(service):
            raise KeyStoreError(
                "service: lowercase letters, digits, - or _ (e.g. bybit)"
            )
        return self.dir / f"{service}.env"

    def env(self, service: str) -> Dict[str, str]:
        """``{NAME: value}`` for one service; empty if none stored."""
        try:
            return _parse(self._path(service).read_text())
        except OSError:
            return {}

    def services(self) -> List[str]:
        if not self.dir.is_dir():
            return []
        return sorted(p.stem for p in self.dir.glob("*.env"))

    def names(self) -> Dict[str, List[str]]:
        """``{service: [key names]}`` — names only, never values."""
        return {s: sorted(self.env(s)) for s in self.services()}

    def set(self, service: str, name: str, value: str) -> None:
        path = self._path(service)
        if not NAME_RE.match(name):
            raise KeyStoreError("key name: UPPER_CASE, e.g. BYBIT_API_KEY")
        value = value.strip()
        if not value or any(c in value for c in "\r\n\0") or len(value) > 4096:
            raise KeyStoreError("value must be one non-empty line")
        data = self.env(service)
        data[name] = value
        self._write(path, data)

    def delete(self, service: str, name: Optional[str] = None) -> bool:
        """Drop one key, or the whole service when ``name`` is None."""
        path = self._path(service)
        data = self.env(service)
        if name is None or (name in data and len(data) == 1):
            existed = path.exists()
            path.unlink(missing_ok=True)
            return existed
        if name not in data:
            return False
        del data[name]
        self._write(path, data)
        return True

    def secret_values(self, service: Optional[str] = None) -> List[str]:
        """Every stored value (for output redaction), longest first."""
        services = [service] if service else self.services()
        vals = {
            v for s in services for v in self.env(s).values() if len(v) >= _MIN_REDACT
        }
        return sorted(vals, key=len, reverse=True)

    def redact(self, text: str, service: Optional[str] = None) -> str:
        for value in self.secret_values(service):
            text = text.replace(value, "<redacted>")
        return text

    def _write(self, path: Path, data: Dict[str, str]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        body = "".join(f"{k}={v}\n" for k, v in sorted(data.items()))
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-", text=True)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(body)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def default_keystore() -> Optional[KeyStore]:
    """The live store, or None in the legacy (no data root) layout — there
    ``secrets_dir`` is the whole ``data/`` and would not be agent-denied."""
    from ..utils.paths import data_layout

    layout = data_layout()
    if layout.root is None:
        return None
    return KeyStore(layout.secrets_dir / "tools")
