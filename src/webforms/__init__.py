"""Web forms the bot fills on the owner's behalf (dv-5a25).

One mechanism, many forms: a form is a :class:`FormDef` in :data:`FORMS`
(id, domain, page flow, declarative field map, and a ``plan`` that turns the
claim data into per-page values). The ``webform_*`` tools
(``src/claude/webform_tools.py``) take a form id; the filler
(``src/webforms/engine.py``) is generic; the browser runs in a child process
(``src/webforms/runner.py``). A second form is a new definition, not a tool.

Nothing here imports Playwright: it is optional (not in the base install) and
only the child runner needs it.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

# Field kinds the page driver knows.
TEXT = "text"  # input / textarea: fill + read value
SELECT = "select"  # <select>: options map data value -> option label
RADIO = "radio"  # options map data value -> the choice's selector
CHECK = "check"  # checkbox: value True / False
FILE = "file"  # file input


class Unsupported(Exception):
    """The data asks for something this form definition does not cover."""


class BadData(Exception):
    """The data is incomplete or malformed; ``key`` names the field."""

    def __init__(self, key: str, message: str) -> None:
        super().__init__(message)
        self.key = key


@dataclass(frozen=True)
class FieldDef:
    key: str
    kind: str
    selector: str = ""
    options: Mapping[str, str] = field(default_factory=dict)
    # Several logical fields sharing one input, joined by a space in index
    # order (e.g. currency + amount in one "Currency and amount" box).
    part: Optional[Tuple[int, int]] = None
    # Close a date picker after filling.
    escape: bool = False


@dataclass(frozen=True)
class PageDef:
    number: int
    selector: str
    fields: Tuple[FieldDef, ...]


@dataclass
class FillPlan:
    """What to put on each page, in fill order; ``files`` go to ``upload``."""

    values: Dict[int, List[Tuple[str, Any]]]
    upload: Optional[Tuple[int, str]] = None
    files: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class FormDef:
    id: str
    title: str
    domain: str
    url: str
    pages: Tuple[PageDef, ...]
    plan: Callable[[Dict[str, Any]], FillPlan]
    form_selector: str = ""
    next_selector: str = ".gform_next_button"
    submit_selector: str = ""
    confirmation_selector: str = ".gform_confirmation_message"
    validation_selector: str = (
        ".gform_validation_errors, .gfield_validation_message, .validation_message"
    )
    upload_pending_selector: str = (
        ".gfield_fileupload_progress, .ginput_preview .gfield_fileupload_percent"
    )
    overlay_selectors: Tuple[str, ...] = ()
    # Shown in the ask preview, in this order, when present in the data.
    preview_keys: Tuple[str, ...] = ()

    def field(self, page: int, key: str) -> FieldDef:
        for p in self.pages:
            if p.number == page:
                for f in p.fields:
                    if f.key == key:
                        return f
        raise KeyError(f"{self.id}: no field {key!r} on page {page}")


def _load_forms() -> Dict[str, FormDef]:
    from .william_russell import FORM as WILLIAM_RUSSELL

    return {f.id: f for f in (WILLIAM_RUSSELL,)}


FORMS: Dict[str, FormDef] = _load_forms()


def _browser_dir_ok(browsers_path: Optional[str]) -> Optional[str]:
    """None when a Chromium build is present, else what is missing."""
    root = (
        Path(browsers_path).expanduser()
        if browsers_path
        else Path.home() / ".cache" / "ms-playwright"
    )
    if not root.is_dir():
        return f"Chromium for Playwright not found: {root} does not exist"
    if not any(root.glob("chromium*-*")):
        return f"Chromium for Playwright not found in {root}"
    return None


def runtime_problem(registry: Any, browsers_path: Optional[str]) -> Optional[str]:
    """The restart-gate check: None when no profile uses ``web_forms`` or the
    browser can start, else one line saying what is missing.

    Checks that Playwright is importable in this venv (``poetry install -E
    webforms``) and that a Chromium build sits in ``browsers_path``
    (``WEBFORMS_BROWSERS_PATH``; unset = Playwright's default cache).
    """
    users = [
        p.slug
        for p in getattr(registry, "profiles", []) or []
        if getattr(p, "web_forms", ())
    ]
    if not users:
        return None
    who = ", ".join(users)
    if importlib.util.find_spec("playwright") is None:
        return (
            f"web_forms on {who}: Playwright is not installed "
            "(install playwright==1.64.0 in the bot venv)"
        )
    missing = _browser_dir_ok(browsers_path)
    if missing:
        return f"web_forms on {who}: {missing} (set WEBFORMS_BROWSERS_PATH)"
    return None
