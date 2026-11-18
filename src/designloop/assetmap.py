"""The design map: MAP.md, one generated page of what the design pipeline has
made and where it lives, for any board.

Every generated picture goes under the board's *assets root*
(``design.assets`` in the board's ``design`` block; default
``data/workspaces/<profile>/design``)::

    <root>/elements/<run>/<group>/<name>.png   FLUX elements (+ log.jsonl)
    <root>/mockups/<stamp>/<screen>/mockup-N.png   whole-screen mockups
    <root>/MAP.md                              this page, rebuilt from data

The one hand-written input is ``<root>/status.yaml`` (``scripts/design_map.py
<board> --mark <screen> built|picked|redo``): which screens landed in the
build. MAP.md has no hand-kept column: screens come from the board's screen
registry (``design.screens``, a path in the board repo, default
``docs/features.yaml``: the keys under its ``screens:``), each with its owner
pick(s) (``<references dir>/owner-mockups/<screen>[-<suffix>].png``), last
verdict and the next step; pictures from walking the folders and reading
``log.jsonl`` / ``rounds.jsonl``. Folders outside the structure
(``<root>/*`` written by hand, the sibling ``mockups`` dir) are listed where
they are, never moved. Rebuilt at the end of every elements / mockups run;
``scripts/design_map.py <board> [--init]`` by hand (``--init`` bootstraps a
board that has no structure yet).

Two more sections list what exists outside the generated pictures, where it
is. ``## Brand``: ``design.brand.rounds`` (folders of logo / mark / wordmark
rounds; a folder of dated subfolders gives one row per subfolder; plus
``<root>/brand/`` always), logo-named images beside the references file, and
``design.brand.current`` (the files / folders the app and bot use as their
mark now; a missing one shows ``missing``). ``## Templates & references``:
``design.templates`` (UI kits, mockups, badge systems, art folders; a SPEC.md /
README.md beside the images is named). Paths: relative = board repo,
``@assets/<x>`` = under the assets root, absolute as is. The owner pick column
is the first ``likes:`` / ``avoid:`` line of a ``### `<file>` `` entry in the
references file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import yaml

from src.utils.paths import read_data_root

from .references import IMAGE_SUFFIXES, references_path, verdicts_path

MAP_NAME = "MAP.md"
SUBDIRS = ("elements", "mockups")
OWNER_MOCKUPS = "owner-mockups"
DONE_STATUSES = {"done", "closed", "archived", "cancelled", "wontfix"}
_HEAD = re.compile(r"^## (\d{4}-\d{2}-\d{2})[^\n·]*(?:·([^\n]*))?$", re.M)
_CARD = re.compile(r"^[a-z]+-[0-9a-f]{4}$")
_STAMP = re.compile(r"^\d{8}(-\d{6})?-?")
_ROUND = re.compile(r"^r\d+$")
SKIP_NAMES = {"__pycache__", ".git"}


def workspace_root(profile: str, repo_root: Path) -> Path:
    """The profile's workspace: ``<DEVVY_DATA_ROOT>/workspaces/<profile>``,
    else (legacy layout) ``<repo>/data/workspaces/<profile>``."""
    data = read_data_root(dotenv=Path(repo_root) / (".en" + "v"))
    base = data / "workspaces" if data else Path(repo_root) / "data" / "workspaces"
    return base / profile


def assets_root(design: Any, profile: str, repo_root: Path) -> Path:
    """``design.assets`` (relative = in the board workspace), else
    ``<workspace>/design``."""
    ws = workspace_root(profile, repo_root)
    raw = design.get("assets") if isinstance(design, dict) else None
    if isinstance(raw, str) and raw.strip():
        p = Path(raw).expanduser()
        return p if p.is_absolute() else ws / p
    return ws / "design"


def elements_out(root: Path, run: str) -> Path:
    return root / "elements" / run


def mockups_out(root: Path, stamp: str) -> Path:
    return root / "mockups" / stamp


def default_elements_out(root: Path, prompts: Path, by: str = "") -> Path:
    """Where ``design_elements`` writes when no out dir is given:
    ``elements/<by>-<prompts name>``, so two agents of one board never share
    a run folder and the map shows who drew it."""
    run = prompts.stem.removesuffix("-prompts")
    if by and not run.startswith(f"{by}-"):
        run = f"{by}-{run}"
    return elements_out(root, run)


def default_mockups_out(root: Path, now: Optional[datetime] = None) -> Path:
    """Where ``mockup_sprint`` writes when no out dir is given."""
    return mockups_out(root, (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S"))


# --- status marks (the only hand-written input) --------------------------

STATUS_NAME = "status.yaml"
MARK_STATES = ("built", "picked", "redo")


def read_status(root: Path) -> Dict[str, Dict[str, Any]]:
    """``screens:`` of ``<root>/status.yaml``: screen -> {built, picked, redo}
    entries (each ``{at, sha, note}``)."""
    try:
        data = yaml.safe_load((root / STATUS_NAME).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    raw = data.get("screens") if isinstance(data, dict) else None
    return (
        {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        if isinstance(raw, dict)
        else {}
    )


def mark_screen(
    root: Path,
    screen: str,
    state: str,
    *,
    sha: str = "",
    note: str = "",
    at: Optional[datetime] = None,
) -> Path:
    """Record ``state`` for ``screen`` (latest of each state kept)."""
    if state not in MARK_STATES:
        raise ValueError(f"state must be one of {list(MARK_STATES)}")
    data = read_status(root)
    entry: Dict[str, Any] = {
        "at": (at or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    }
    if sha:
        entry["sha"] = sha
    if note:
        entry["note"] = note
    data.setdefault(screen, {})[state] = entry
    root.mkdir(parents=True, exist_ok=True)
    path = root / STATUS_NAME
    path.write_text(
        "# hand-written marks (scripts/design_map.py <board> --mark ...); "
        "MAP.md is generated from them\n"
        + yaml.safe_dump({"screens": data}, sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def _stamp(entry: Any) -> str:
    return str(entry.get("at") or "") if isinstance(entry, dict) else ""


def built_text(entry: Any) -> str:
    if not isinstance(entry, dict):
        return "-"
    sha = str(entry.get("sha") or "")
    return f"{sha[:8]} {_stamp(entry)[:10]}".strip() or "-"


# --- screens ------------------------------------------------------------


@dataclass
class Verdict:
    date: str
    line: str


SCREENS_DEFAULT = "docs/features.yaml"


def registry_screens(repo: Path, design: Any) -> List[str]:
    """Screen ids of the board's registry: the 2-space-indented keys under the
    top-level ``screens:`` of ``design.screens`` (default ``docs/features.yaml``).
    Read line by line: the file need not be valid YAML elsewhere."""
    d = design if isinstance(design, dict) else {}
    raw = d.get("screens")
    rel = raw if isinstance(raw, str) and raw.strip() else SCREENS_DEFAULT
    path = Path(rel).expanduser()
    path = path if path.is_absolute() else Path(repo) / path
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: List[str] = []
    inside = False
    for ln in lines:
        if re.match(r"^screens:\s*(#.*)?$", ln):
            inside = True
            continue
        if inside and re.match(r"^[^\s#]", ln):
            break
        m = re.match(r"^  ([A-Za-z_][\w-]*):", ln) if inside else None
        if m:
            out.append(m.group(1))
    return out


def owner_picks(
    repo: Path, references: Optional[str], screens: Sequence[str]
) -> Dict[str, List[Path]]:
    """screen id -> its pick files: ``<screen>.png`` and ``<screen>-N.png`` /
    ``<screen>-<suffix>.png`` (longest matching id wins)."""
    refs = references_path(repo, references)
    if refs is None:
        return {}
    d = refs.parent / OWNER_MOCKUPS
    if not d.is_dir():
        return {}
    ids = sorted(screens, key=len, reverse=True)
    out: Dict[str, List[Path]] = {}
    for p in sorted(d.iterdir()):
        if p.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        for s in ids:
            if p.stem == s or p.stem.startswith(s + "-"):
                out.setdefault(s, []).append(p)
                break
    return out


def _date(text: str) -> str:
    """``2026-11-14`` or ``14.11.26`` -> ISO date, else ''."""
    m = re.match(r"\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)
    m = re.match(r"\s*(\d{1,2})\.(\d{2})\.(\d{2,4})", text)
    if m:
        y = int(m.group(3))
        return f"{y + 2000 if y < 100 else y:04d}-{m.group(2)}-{int(m.group(1)):02d}"
    return ""


def _phrase(screen: str) -> "re.Pattern[str]":
    words = r"[\s_-]+".join(re.escape(w) for w in re.split(r"[_\s-]+", screen) if w)
    return re.compile(rf"(?<![\w-]){words}(?![\w-])", re.I)


def _clip(text: str, n: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def parse_verdicts(text: str, screens: Sequence[str]) -> Dict[str, Verdict]:
    """screen id -> its last verdict, for registry ``screens`` only.

    Header shapes in the owner-verdicts file: ``## <date time> UTC · [card] ·
    [screen] · <outcome or note>`` (``append_verdict`` / ``design note``; a
    ``· remark`` is no verdict) and the hand-written ``## 15.11.26 08:10 —
    <topic>``. A screen is named by an id among the ``·`` parts of the header,
    else by the id (``event_form`` also as «event form») in the header or the
    first line of the body, else by ``owner-mockups/<id>[-x].png`` in that
    first line. Later entries win."""
    known = set(screens)
    phrases = {s: _phrase(s) for s in screens}
    out: Dict[str, Verdict] = {}
    heads = list(re.finditer(r"^## .*$", text, re.M))
    for i, m in enumerate(heads):
        head = m.group(0)[3:]
        date = _date(head)
        if not date:
            continue
        parts = [p.strip() for p in head.split(" · ")]
        if len(parts) > 1 and parts[-1] == "remark":
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = [ln.strip() for ln in text[m.end() : end].splitlines() if ln.strip()]
        first = body[0] if body else ""
        hit = [p for p in parts[1:] if p in known]
        if not hit:
            blob = f"{head} {first}"
            hit = [s for s in screens if phrases[s].search(blob)]
            for ref in re.findall(r"owner-mockups/([\w-]+)\.\w+", first):
                hit += [
                    s
                    for s in sorted(known, key=len, reverse=True)
                    if ref == s or ref.startswith(s + "-")
                ][:1]
        line = _clip(first or parts[-1])
        for s in hit:
            out[s] = Verdict(date, line)
    return out


def card_meta(path: Path) -> Dict[str, str]:
    """Frontmatter of a card as ``key -> raw value`` (quotes stripped)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    meta: Dict[str, str] = {}
    if text.startswith("---"):
        for ln in text.split("---", 2)[1].splitlines():
            k, _, v = ln.partition(":")
            meta[k.strip()] = v.strip().strip("\"'")
    return meta


CARDS_PER_SCREEN = 5


def open_cards_for(tasks: Path, screens: Sequence[str]) -> Dict[str, List[str]]:
    """screen id -> ``id (status)`` of the open cards that are about it: its
    TITLE names the screen (id, or the id as words), or the card is a
    designloop / design card whose ``design_screen:`` is the id. Newest first,
    at most ``CARDS_PER_SCREEN``."""
    out: Dict[str, List[str]] = {s: [] for s in screens}
    if not screens or not tasks.is_dir():
        return out
    pats = {s: _phrase(s) for s in screens}
    found: Dict[str, List[Tuple[str, str]]] = {s: [] for s in screens}
    for path in sorted(tasks.glob("*.md")):
        meta = card_meta(path)
        status = meta.get("status", "")
        if status in DONE_STATUSES:
            continue
        title = meta.get("title", "")
        design = bool(
            re.search(r"\b(?:designloop|design)\b", meta.get("labels", ""))
        ) or meta.get("kind", "") in ("designloop", "design")
        when = meta.get("updated") or meta.get("created") or ""
        label = f"{meta.get('id', path.stem)} ({status or '?'})"
        for s in screens:
            if pats[s].search(title) or (design and meta.get("design_screen") == s):
                found[s].append((when, label))
    for s, items in found.items():
        items.sort(key=lambda t: t[0], reverse=True)
        out[s] = [lbl for _, lbl in items[:CARDS_PER_SCREEN]]
    return out


def awaiting_cards_for(tasks: Path, screens: Sequence[str]) -> Dict[str, List[str]]:
    """screen id -> ids of open designloop cards waiting for the owner's pick
    (``design_state: awaiting_pick`` / ``awaiting_mockup``)."""
    out: Dict[str, List[str]] = {}
    if not tasks.is_dir():
        return out
    for path in sorted(tasks.glob("*.md")):
        meta = card_meta(path)
        if meta.get("status", "") in DONE_STATUSES:
            continue
        screen = meta.get("design_screen", "")
        if screen in screens and meta.get("design_state") in (
            "awaiting_pick",
            "awaiting_mockup",
        ):
            out.setdefault(screen, []).append(meta.get("id", path.stem))
    return out


def card_variant_dirs(
    repo: Path, tasks: Path, screens: Sequence[str]
) -> Dict[str, List[str]]:
    """screen id -> ``.screens/design/<card>`` dirs (a pump design run's
    ``v1..``, ``mockups/``, ``collage.png``) of its open designloop cards."""
    out: Dict[str, List[str]] = {}
    if not tasks.is_dir():
        return out
    for path in sorted(tasks.glob("*.md")):
        meta = card_meta(path)
        screen = meta.get("design_screen", "")
        if screen not in screens or meta.get("status", "") in DONE_STATUSES:
            continue
        cid = meta.get("id", path.stem)
        d = Path(repo) / ".screens" / "design" / cid
        if not d.is_dir():
            continue
        n = sum(1 for p in d.iterdir() if p.is_dir() and re.fullmatch(r"v\d+", p.name))
        m = d / "mockups"
        mk = sum(1 for p in m.glob("*") if _is_image(p)) if m.is_dir() else 0
        bits = [f"variants: {n}"] if n else []
        if mk:
            bits.append(f"mockups: {mk}")
        out.setdefault(screen, []).append(
            f"`.screens/design/{cid}`" + (f" ({', '.join(bits)})" if bits else "")
        )
    return out


def report_dirs(workspace: Path) -> List[Tuple[str, int, str]]:
    """(folder, images, date) of each ``reports/<dir>`` of the workspace."""
    base = workspace / "reports"
    out: List[Tuple[str, int, str]] = []
    if not base.is_dir():
        return out
    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        if d.name in SKIP_NAMES or d.name.startswith("."):
            continue
        imgs = [p for p in d.rglob("*") if p.is_file() and _is_image(p)]
        if not imgs:
            continue
        when = datetime.fromtimestamp(max(p.stat().st_mtime for p in imgs), UTC)
        out.append((f"reports/{d.name}", len(imgs), when.strftime("%Y-%m-%d")))
    return out


# --- pictures -----------------------------------------------------------


@dataclass
class Unit:
    path: Path
    rel: str
    images: int = 0
    source: str = "unknown"
    failed: List[Tuple[str, str]] = field(default_factory=list)  # (name, job id)
    errors: int = 0
    date: str = ""
    tokens: Set[str] = field(default_factory=set)
    legacy: bool = False
    other: bool = False  # not mockup output (a legacy-root folder of something else)

    @property
    def status(self) -> str:
        if self.failed or self.errors:
            return "FAILED" if not self.images else "partial"
        return "empty" if not self.images else "ok"


def _is_image(p: Path) -> bool:
    """A picture; the collage ``*-sheet`` / ``*-rounds`` overviews are not."""
    return p.suffix.lower() in IMAGE_SUFFIXES and not re.search(
        r"-(?:sheet|rounds)$", p.stem
    )


def _jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _has_marker(d: Path) -> bool:
    try:
        return any((d / n).exists() for n in ("log.jsonl", "rounds.jsonl")) or any(
            _is_image(p) for p in d.iterdir() if p.is_file()
        )
    except OSError:
        return False


def _tokens(unit: Path) -> Set[str]:
    names = {unit.name}
    for p in unit.iterdir():
        if p.is_dir():
            names.add(p.name)
        else:
            m = re.match(r"(.+)-(?:sheet|rounds)\.\w+$", p.name)
            if m:
                names.add(m.group(1))
    toks: Set[str] = set()
    for n in names:
        n = _STAMP.sub("", n)
        toks.add(n)
        toks.update(t for t in re.split(r"-", n) if t and not _ROUND.match(t))
    return toks


def inspect_unit(path: Path, rel: str, legacy: bool) -> Unit:
    u = Unit(path=path, rel=rel, legacy=legacy)
    files = [p for p in path.rglob("*") if p.is_file()]
    imgs = [p for p in files if _is_image(p)]
    u.images = len(imgs)
    stamps = [p.stat().st_mtime for p in (imgs or files)]
    if stamps:
        u.date = datetime.fromtimestamp(max(stamps), UTC).strftime("%Y-%m-%d")
    u.tokens = _tokens(path)
    log = path / "log.jsonl"
    rounds = path / "rounds.jsonl"
    last: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for rec in _jsonl(log):
        last[(str(rec.get("group")), str(rec.get("name")))] = rec
    for (group, name), rec in last.items():
        if rec.get("ok") is False and not any(path.rglob(f"{name}.png")):
            u.errors += 1
            u.failed.append((f"{group}/{name}", str(rec.get("job_id") or "")))
    if rounds.exists():
        recs = _jsonl(rounds)
        u.errors += sum(1 for r in recs if r.get("error"))
        u.source = "nano banana / flux (mockup sprint, rounds.jsonl)"
    elif last:
        u.source = "flux (elements, log.jsonl)"
    elif any(p.name.startswith("mockup-") for p in imgs):
        u.source = "mockup sprint (no rounds.jsonl)"
    elif any(p.name.endswith(".prompt.txt") for p in files):
        u.source = "unknown (prompt files only)"
    elif imgs:
        u.source = "unknown (no log: fetched or drawn by hand)"
    else:
        u.source = "-"
    if (
        not rounds.exists()
        and not last
        and any(p.name.startswith("mockup-") for p in imgs)
    ):
        u.source = "mockup sprint (no rounds.jsonl)"
    return u


def scan_units(root: Path, legacy_roots: Sequence[Path] = ()) -> List[Unit]:
    """Folders that hold pictures or a log, found top-down; a folder with a
    log or images is one unit (its subfolders counted in it)."""
    units: List[Unit] = []

    def walk(d: Path, base: Path, legacy: bool, depth: int) -> None:
        try:
            kids = sorted(p for p in d.iterdir() if p.is_dir())
        except OSError:
            return
        for k in kids:
            if k.name in SKIP_NAMES or k.name.startswith("."):
                continue
            rel = str(k.relative_to(base.parent))
            if _has_marker(k) or (depth == 0 and k.name not in SUBDIRS):
                units.append(inspect_unit(k, rel, legacy))
            else:
                walk(k, base, legacy, depth + 1)

    for base, legacy in [(root, False)] + [(p, True) for p in legacy_roots]:
        if base.is_dir():
            walk(base, base, legacy, 0)
    return units


def mark_other(units: List[Unit], screens: Sequence[str]) -> None:
    """Folders of the old ``mockups/`` root that are not mockup output (build
    screenshots, tools, scratch): no ``mockup-N`` picture, no rounds log and no
    screen id in the name or inside. They stay listed, as source ``other``."""
    for u in units:
        if not u.legacy:
            continue
        mockup = u.source.startswith(("mockup sprint", "nano banana"))
        if not mockup and not any(t in screens for t in u.tokens):
            u.other = True
            u.source = "other (not mockup output)"


# --- brand and templates ------------------------------------------------

BRAND_SUFFIXES = tuple(IMAGE_SUFFIXES) + (".svg",)
_LOGO_NAME = re.compile(r"logo|wordmark|mark|brand", re.I)
ASSETS_PREFIX = "@assets/"
WALK_MAX = 5000


@dataclass
class AssetRow:
    path: str
    kind: str
    images: int = 0
    date: str = "-"
    note: str = "-"
    missing: bool = False


def _asset_path(raw: Any, repo: Path, root: Path) -> Optional[Path]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text: str = raw.strip()
    if text.startswith(ASSETS_PREFIX):
        return root / text[len(ASSETS_PREFIX) :]
    p = Path(text).expanduser()
    return p if p.is_absolute() else Path(repo) / p


def _show(p: Path, repo: Path) -> str:
    try:
        return str(p.relative_to(repo))
    except ValueError:
        return str(p)


def _asset_files(p: Path) -> List[Path]:
    """Brand-type image files under ``p`` (``p`` itself when a file); dot
    folders, ``__pycache__`` and ``.git`` skipped, at most ``WALK_MAX``."""
    if p.is_file():
        return [p] if p.suffix.lower() in BRAND_SUFFIXES else []
    out: List[Path] = []
    stack = [p]
    while stack and len(out) < WALK_MAX:
        d = stack.pop()
        try:
            kids = sorted(d.iterdir())
        except OSError:
            continue
        for k in kids:
            if k.name in SKIP_NAMES or k.name.startswith("."):
                continue
            if k.is_dir():
                stack.append(k)
            elif k.suffix.lower() in BRAND_SUFFIXES and not re.search(
                r"-(?:sheet|rounds)$", k.stem
            ):
                out.append(k)
    return out


def _day(paths: Sequence[Path]) -> str:
    return _mtime(paths)[:10] or "-"


def reference_picks(path: Optional[Path]) -> Dict[Path, str]:
    """file beside the references file -> first ``likes:`` / ``avoid:`` line
    of its ``### `<file>` `` entry there."""
    try:
        text = path.read_text(encoding="utf-8") if path else ""
    except OSError:
        return {}
    out: Dict[Path, str] = {}
    name: Optional[Path] = None
    for ln in text.splitlines():
        m = re.match(r"^###\s+`([^`]+)`", ln)
        if m:
            name = path.parent / Path(m.group(1)).name if path else None
            continue
        if ln.startswith("#"):
            name = None
            continue
        m = re.match(r"^\s*[-*]?\s*((?:likes|avoid):.*)$", ln, re.I)
        if name and m and name not in out:
            out[name] = _clip(m.group(1), 70)
    return out


def _pick_for(files: Sequence[Path], picks: Dict[Path, str]) -> str:
    for f in files:
        if f in picks:
            return (f"{f.name}: " if len(files) > 1 else "") + picks[f]
    return "-"


def _row(p: Path, repo: Path, kind: str, picks: Dict[Path, str]) -> AssetRow:
    files = _asset_files(p)
    return AssetRow(
        path=_show(p, repo),
        kind=kind,
        images=len(files),
        date=_day(files or [p]),
        note=_pick_for(files, picks),
    )


def _round_rows(p: Path, repo: Path, picks: Dict[Path, str]) -> List[AssetRow]:
    """One row per subfolder holding images, plus one for loose images; a
    plain folder of images is one row."""
    if not p.is_dir():
        return [_row(p, repo, "round", picks)]
    subs = [
        k
        for k in sorted(p.iterdir())
        if k.is_dir() and k.name not in SKIP_NAMES and not k.name.startswith(".")
    ]
    rows = [r for r in (_row(k, repo, "round", picks) for k in subs) if r.images]
    if not rows:
        return [_row(p, repo, "round", picks)]
    loose = [
        f for f in p.iterdir() if f.is_file() and f.suffix.lower() in BRAND_SUFFIXES
    ]
    if loose:
        rows.insert(
            0,
            AssetRow(
                _show(p, repo),
                "round",
                len(loose),
                _day(loose),
                _pick_for(loose, picks),
            ),
        )
    return rows


def _missing(p: Path, repo: Path, kind: str) -> AssetRow:
    return AssetRow(path=_show(p, repo), kind=kind, missing=True, note="missing")


def brand_rows(repo: Path, design: Any, root: Path) -> List[AssetRow]:
    """Rows of the Brand section; an unreadable source is a ``missing`` row."""
    d = design if isinstance(design, dict) else {}
    braw = d.get("brand")
    b: Dict[str, Any] = braw if isinstance(braw, dict) else {}
    refs = references_path(repo, d.get("references"))
    picks = reference_picks(refs)
    rows: List[AssetRow] = []

    def lst(key: str) -> List[Any]:
        v = b.get(key)
        return v if isinstance(v, list) else ([v] if isinstance(v, str) else [])

    rounds = [_asset_path(r, repo, root) for r in lst("rounds")]
    own = root / "brand"
    if own.is_dir() and own not in rounds:
        rounds.append(own)
    for p in rounds:
        if p is None:
            continue
        try:
            rows += (
                _round_rows(p, repo, picks)
                if p.exists()
                else [_missing(p, repo, "round")]
            )
        except OSError:
            rows.append(_missing(p, repo, "round"))
    if refs is not None:
        try:
            for f in sorted(refs.parent.iterdir()):
                if (
                    f.is_file()
                    and f.suffix.lower() in BRAND_SUFFIXES
                    and _LOGO_NAME.search(f.stem)
                ):
                    rows.append(_row(f, repo, "reference", picks))
        except OSError:
            pass
    for raw in lst("current"):
        p = _asset_path(raw, repo, root)
        if p is None:
            continue
        try:
            rows.append(
                _row(p, repo, "current", picks)
                if p.exists()
                else _missing(p, repo, "current")
            )
        except OSError:
            rows.append(_missing(p, repo, "current"))
    return rows


def _docs_beside(p: Path) -> str:
    d = p if p.is_dir() else p.parent
    try:
        docs = sorted(f.name for f in d.iterdir() if f.is_file() and f.suffix == ".md")
    except OSError:
        return "-"
    return ", ".join(docs[:4]) + (f" +{len(docs) - 4}" if len(docs) > 4 else "") or "-"


def template_rows(repo: Path, design: Any, root: Path) -> List[AssetRow]:
    d = design if isinstance(design, dict) else {}
    raw = d.get("templates")
    items = raw if isinstance(raw, list) else ([raw] if isinstance(raw, str) else [])
    rows: List[AssetRow] = []
    for item in items:
        p = _asset_path(item, repo, root)
        if p is None:
            continue
        try:
            if not p.exists():
                rows.append(_missing(p, repo, "template"))
                continue
            files = _asset_files(p)
            rows.append(
                AssetRow(
                    _show(p, repo),
                    "template",
                    len(files),
                    _day(files or [p]),
                    _docs_beside(p),
                )
            )
        except OSError:
            rows.append(_missing(p, repo, "template"))
    return rows


def _cell(text: str) -> str:
    return text.replace("|", "/")


# --- the page -----------------------------------------------------------


def _screens_for(unit: Unit, screen: str) -> bool:
    return not unit.other and screen in unit.tokens


def _mtime(paths: Sequence[Path]) -> str:
    try:
        t = max(p.stat().st_mtime for p in paths)
    except (OSError, ValueError):
        return ""
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_step(
    has_pick: bool,
    has_folders: bool,
    *,
    awaiting: Sequence[str] = (),
    marks: Optional[Dict[str, Any]] = None,
    pick_at: str = "",
) -> str:
    """A built mark wins (done, or rebuild when a pick / ``redo`` mark is
    newer than the build; a design card still waiting on a built screen is
    named as possibly stale); then a card waiting for the owner's pick; else
    the pick / folders decide."""
    marks = marks or {}
    built = _stamp(marks.get("built"))
    if built:
        newer = max(pick_at, _stamp(marks.get("picked")))
        if _stamp(marks.get("redo")) > built:
            return "rebuild: marked redo"
        if newer > built:
            return "rebuild: pick newer than build"
        done = f"done (built {built[:10]})"
        if awaiting:
            done += f"; card still waiting: {', '.join(awaiting)} (stale?)"
        return done
    if awaiting:
        return f"needs owner pick ({', '.join(awaiting)})"
    if has_pick:
        return "build to pick"
    return "needs owner pick" if has_folders else "needs mockups"


def render_map(
    *,
    board: str,
    root: Path,
    picks: Dict[str, List[Path]],
    verdicts: Dict[str, Verdict],
    cards: Dict[str, List[str]],
    units: List[Unit],
    screens: Sequence[str],
    repo: Optional[Path] = None,
    now: Optional[datetime] = None,
    status: Optional[Dict[str, Dict[str, Any]]] = None,
    awaiting: Optional[Dict[str, List[str]]] = None,
    variants: Optional[Dict[str, List[str]]] = None,
    reports: Sequence[Tuple[str, int, str]] = (),
    brand: Sequence[AssetRow] = (),
    templates: Sequence[AssetRow] = (),
) -> str:
    status = status or {}
    awaiting = awaiting or {}
    variants = variants or {}
    when = (now or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")
    out = [
        f"# Design map: {board}",
        "",
        f"Generated {when} by `scripts/design_map.py {board}` from the folders, "
        "logs, verdict file and cards. Do not edit: rebuilt after every "
        "elements / mockups run.",
        f"Pictures root: `{root}` (`elements/<run>/<group>/`, "
        "`mockups/<stamp>/<screen>/`; folders outside it are listed where they are).",
        "",
        "## Screens",
        "",
        "| screen | next | built | owner pick | last verdict | mockup folders "
        "| open cards |",
        "|---|---|---|---|---|---|---|",
    ]

    def rel(p: Path) -> str:
        try:
            return str(p.relative_to(repo)) if repo else str(p)
        except ValueError:
            return str(p)

    for s in screens:
        ps = picks.get(s) or []
        pick = (
            ", ".join(f"`{rel(p)}`" for p in ps[:3])
            + (f" +{len(ps) - 3}" if len(ps) > 3 else "")
            if ps
            else "no"
        )
        v = verdicts.get(s)
        verdict = f"{v.date}: {v.line}".replace("|", "/") if v else "-"
        mine = [u for u in units if _screens_for(u, s)]
        found = [f"`{u.rel}`" for u in mine] + variants.get(s, [])
        folders = ", ".join(found) or "-"
        card = ", ".join(cards.get(s) or [])
        marks = status.get(s) or {}
        nxt = next_step(
            bool(ps),
            bool(found),
            awaiting=awaiting.get(s, ()),
            marks=marks,
            pick_at=_mtime(ps),
        )
        out.append(
            f"| {s} | {nxt} | {built_text(marks.get('built'))} | {pick} | {verdict}"
            f" | {folders} | {card or '-'} |"
        )
    if not screens:
        out.append("| (no screens: set design.screens) | | | | | | |")
    out += [
        "",
        "## Pictures",
        "",
        "| folder | images | status | source | date | failures |",
        "|---|---|---|---|---|---|",
    ]
    for u in units:
        fetch = [f"{n} {j}" for n, j in u.failed if j]
        lost = [n for n, j in u.failed if not j]
        bits = fetch[:8]
        if len(fetch) > 8:
            bits.append(f"+{len(fetch) - 8} more fetchable")
        if lost:
            bits.append(
                f"{len(lost)} lost (no job id): "
                + ", ".join(lost[:3])
                + (", ..." if len(lost) > 3 else "")
            )
        fails = "; ".join(bits)
        tag = " (legacy)" if u.legacy else ""
        state = u.status if u.status in ("ok", "partial") else u.status.upper()
        out.append(
            f"| `{u.rel}`{tag} | {u.images} | {state}"
            f" | {u.source} | {u.date or '-'} | {fails or '-'} |"
        )
    if not units:
        out.append("| (nothing generated yet) | 0 | empty | - | - | - |")
    out += ["", "## Build sheets and screenshots", ""]
    out += ["| folder | images | date |", "|---|---|---|"]
    out += [f"| `{f}` | {n} | {d} |" for f, n, d in reports]
    if not reports:
        out.append("| (none under reports/) | 0 | - |")
    out += ["", "## Brand", ""]
    out.append(
        "Logo / mark / wordmark rounds (`design.brand.rounds`, `<root>/brand/`), "
        "logo references beside the references file, and the mark in use now "
        "(`design.brand.current`)."
    )
    out += ["", "| path | kind | images | date | owner pick |", "|---|---|---|---|---|"]
    for r in brand:
        n = "missing" if r.missing else str(r.images)
        pick = "-" if r.missing else _cell(r.note)
        out.append(f"| {_cell(r.path)} | {r.kind} | {n} | {r.date} | {pick} |")
    if not brand:
        out.append("| (no brand sources: set design.brand) | - | 0 | - | - |")
    out += ["", "## Templates & references", ""]
    out += ["| path | images | date | notes |", "|---|---|---|---|"]
    for r in templates:
        n = "missing" if r.missing else str(r.images)
        out.append(f"| {_cell(r.path)} | {n} | {r.date} | {_cell(r.note)} |")
    if not templates:
        out.append("| (no templates: set design.templates) | 0 | - | - |")
    out.append("")
    return "\n".join(out)


def legacy_roots(root: Path) -> List[Path]:
    """Sibling ``mockups`` dir of an assets root named ``design`` (the
    layout older runs used)."""
    sib = root.parent / "mockups"
    return [sib] if root.name == "design" and sib.is_dir() else []


def build_map(
    *,
    name: str,
    repo: Path,
    profile: str,
    design: Any,
    tasks: Path,
    repo_root: Path,
    now: Optional[datetime] = None,
) -> Tuple[Path, str]:
    """(MAP.md path, its text) for one board; writes nothing."""
    root = assets_root(design, profile, repo_root)
    d = design if isinstance(design, dict) else {}
    screens = registry_screens(repo, design)
    picks = owner_picks(repo, d.get("references"), screens)
    vpath = verdicts_path(repo, d.get("references"), d.get("verdicts"))
    verdicts: Dict[str, Verdict] = {}
    if vpath is not None and vpath.is_file():
        verdicts = parse_verdicts(vpath.read_text(encoding="utf-8"), screens)
    units = scan_units(root, legacy_roots(root))
    mark_other(units, screens)
    cards = open_cards_for(tasks, screens)
    text = render_map(
        status=read_status(root),
        awaiting=awaiting_cards_for(tasks, screens),
        variants=card_variant_dirs(repo, tasks, screens),
        reports=report_dirs(workspace_root(profile, repo_root)),
        brand=brand_rows(repo, design, root),
        templates=template_rows(repo, design, root),
        board=name,
        root=root,
        picks=picks,
        verdicts=verdicts,
        cards=cards,
        units=units,
        screens=screens,
        repo=repo,
        now=now,
    )
    return root / MAP_NAME, text


def write_map(board: Any, repo_root: Path, *, init: bool = False) -> Path:
    """Rebuild and write MAP.md for a pump2 ``Board``; ``init`` creates the
    folder structure first."""
    root = assets_root(board.design, board.profile, repo_root)
    if init:
        for sub in SUBDIRS:
            (root / sub).mkdir(parents=True, exist_ok=True)
    path, text = build_map(
        name=board.name,
        repo=board.repo,
        profile=board.profile,
        design=board.design,
        tasks=board.tasks,
        repo_root=repo_root,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def refresh(board: Any, repo_root: Path) -> Optional[Path]:
    """The end-of-run call: never fails the run."""
    try:
        path = write_map(board, repo_root)
    except Exception as exc:  # a stale map must not fail a finished run
        print(f"design map not updated: {exc!r}", flush=True)
        return None
    print(f"map: {path}", flush=True)
    return path
