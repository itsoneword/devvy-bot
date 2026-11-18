"""A board's owner references and the log of his verdicts.

Both live in the board repo, named by its ``design`` block:

- ``references``: a markdown file in the owner's words (e.g.
  ``docs/design-code/references/README.md``). Its text goes into the brief;
  the image files it names that exist in its folder (in order of mention)
  are attached to the critic call as "owner reference" images.
- ``verdicts``: the log of his answers to design runs (pick + a line, or
  «none, because …») and of remarks made outside a run, one dated entry
  each, his words verbatim. Default: ``owner-verdicts.md`` next to the
  references file. The next run's brief carries its last entries.

The run reads both from the board repo's main checkout (working tree), so an
answer logged a minute ago already counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Collection, List, Optional, Sequence, Tuple

VERDICTS_FILE = "owner-verdicts.md"
REFERENCES_CAP = 8000  # chars of the references text in the brief
VERDICTS_KEEP = 10  # newest log entries in the brief
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
LOG_HEAD = (
    "# Owner verdicts on design runs\n\n"
    "Appended by `pump2 answer` / `pump2 design note`; the owner's "
    "words verbatim. The design loop's brief reads the newest entries.\n"
)
_ENTRY = re.compile(r"^## \d{4}-\d{2}-\d{2}", re.M)
# A bare file name: not part of a path (``docs/screens/x.png``) or a
# placeholder (``<name>.png``).
_IMAGE_NAME = re.compile(
    r"(?<![\w./<>-])([\w-][\w.-]*\.(?:png|jpe?g|webp))\b", re.IGNORECASE
)


@dataclass(frozen=True)
class OwnerInputs:
    """What one run reads: the references text, the verdict log's newest
    entries and the reference images (absolute paths)."""

    references: str = ""
    verdicts: str = ""
    images: Tuple[Path, ...] = ()
    # The board's UI kit: attached first, outside the ``images`` cap.
    kit: Tuple[Path, ...] = ()


def kit_paths(repo: Path, kit: Sequence[str]) -> Tuple[Path, ...]:
    """The ``design.kit`` entries as paths (relative ones in ``repo``)."""
    return tuple(Path(k) if Path(k).is_absolute() else Path(repo) / k for k in kit)


def references_path(repo: Path, references: Optional[str]) -> Optional[Path]:
    if not references:
        return None
    path = Path(references)
    return path if path.is_absolute() else Path(repo) / path


def verdicts_path(
    repo: Path, references: Optional[str], verdicts: Optional[str]
) -> Optional[Path]:
    """The verdict log: ``verdicts`` when set, else next to the references
    file; None when the board names neither (the feature is off)."""
    if verdicts:
        path = Path(verdicts)
        return path if path.is_absolute() else Path(repo) / path
    refs = references_path(repo, references)
    return refs.with_name(VERDICTS_FILE) if refs else None


def reference_images(
    path: Path, text: str, cap: int, exclude: Collection[Path] = ()
) -> Tuple[Path, ...]:
    """Image file names mentioned in ``text`` that exist next to ``path``,
    in order of first mention, at most ``cap``; ``exclude`` (the kit) takes
    no slot."""
    out: List[Path] = []
    skip = {p.resolve() for p in exclude}
    for name in _IMAGE_NAME.findall(text):
        image = path.parent / name
        if image in out or not image.is_file() or image.resolve() in skip:
            continue
        out.append(image)
        if len(out) >= cap:
            break
    return tuple(out)


def last_entries(text: str, keep: int = VERDICTS_KEEP) -> str:
    """The newest ``keep`` dated entries of the log; a cut is marked."""
    starts = [m.start() for m in _ENTRY.finditer(text)]
    if not starts:
        return ""
    entries = [text[a:b].strip() for a, b in zip(starts, starts[1:] + [len(text)])]
    shown = entries[-keep:]
    cut = len(entries) - len(shown)
    head = [f"({cut} older verdict(s) not shown)"] if cut else []
    return "\n\n".join(head + shown)


def read_inputs(
    repo: Path,
    references: Optional[str],
    verdicts: Optional[str],
    max_images: int,
    kit: Sequence[str] = (),
) -> OwnerInputs:
    """The run's owner inputs. ``FileNotFoundError`` when a configured
    references file or kit image is missing (the run refuses, never judges
    without it)."""
    kit_files = kit_paths(repo, kit)
    for name, path in zip(kit, kit_files):
        if not path.is_file():
            raise FileNotFoundError(f"design.kit file {name} is missing in {repo}")
    refs_file = references_path(repo, references)
    text = ""
    images: Tuple[Path, ...] = ()
    if refs_file is not None:
        if not refs_file.is_file():
            raise FileNotFoundError(f"references file {refs_file} is missing")
        full = refs_file.read_text(encoding="utf-8").strip()
        text = full
        if len(text) > REFERENCES_CAP:
            text = text[:REFERENCES_CAP] + "\n… (references cut here)"
        images = (
            reference_images(refs_file, full, max_images, kit_files)
            if max_images
            else ()
        )
    log = verdicts_path(repo, references, verdicts)
    log_text = ""
    if log is not None and log.is_file():
        log_text = last_entries(log.read_text(encoding="utf-8"))
    return OwnerInputs(references=text, verdicts=log_text, images=images, kit=kit_files)


def append_verdict(
    log: Path,
    words: str,
    *,
    card: str = "",
    screen: str = "",
    outcome: str = "",
    now: Optional[datetime] = None,
) -> None:
    """One dated entry: ``## <date time> UTC · <card> · <screen> · <outcome>``
    and the owner's words verbatim. A remark outside a run has no card."""
    when = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    head = " · ".join(x for x in (when, card, screen, outcome or "remark") if x)
    entry = f"## {head}\n\n{words.strip()}\n"
    log.parent.mkdir(parents=True, exist_ok=True)
    cur = log.read_text(encoding="utf-8") if log.exists() else LOG_HEAD
    sep = "" if cur.endswith("\n\n") else ("\n" if cur.endswith("\n") else "\n\n")
    log.write_text(f"{cur}{sep}{entry}", encoding="utf-8")
