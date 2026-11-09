"""Example design-loop gate: mechanical checks of one variant's renders.

Copy into your repo (e.g. scripts/design_gate.py), commit it, and point the
board's ``design.gate`` at it (docs/pump.md, "Design loop"):

  python scripts/design_gate.py --worktree {worktree} --renders {renders} \\
      --refs {refs} --screen {screen}

Pure Pillow on files: no browser, no app, no DB. ``--refs`` is the loop's
render of the current app (same base, same size). Prints exactly one JSON list
of failures on stdout (``[]`` = pass) and always exits 0: non-JSON stdout is a
broken gate. Logs go to stderr.

A render fails when its ``<state>-<theme>`` set differs from the refs'
(missing or extra), its size differs, it is blank (one colour), its dark and
light themes are the same picture, or it is identical to its ref (the variant
changed nothing visible there). With no PNG in ``--refs`` the committed
``docs/screens/<screen>/`` PNGs of the worktree name the expected states, at
DEFAULT_SIZE.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_SIZE = (1170, 2532)  # your render's fixed viewport x device scale
NAME = re.compile(r"^(?P<state>.+)-(?P<theme>dark|light)$")
MIN_RANGE = 10  # grey levels between darkest and lightest pixel; below = blank
MIN_STDDEV = 1.0


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _pngs(d: Path) -> dict[str, Path]:
    return {p.stem: p for p in sorted(d.glob("*.png"))} if d.is_dir() else {}


def check(screen: str, worktree: Path, renders: Path, refs: Path) -> list[str]:
    from PIL import Image, ImageChops, ImageStat

    def same(a: Image.Image, b: Image.Image) -> bool:
        if a.size != b.size:
            return False
        diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB"))
        return diff.getbbox() is None

    def blank(im: Image.Image) -> bool:
        grey = im.convert("L")
        lo, hi = grey.getextrema()
        return hi - lo < MIN_RANGE or ImageStat.Stat(grey).stddev[0] < MIN_STDDEV

    got, want = _pngs(renders), _pngs(refs)
    compare = True
    if not want:
        want = _pngs(worktree / "docs" / "screens" / screen)
        compare = False
        _log(f"design_gate: no PNG in {refs}; using docs/screens/{screen}")
    if not want:
        return [f"{screen}: no reference states to gate against"]
    failures: list[str] = []
    for name in sorted(set(want) - set(got)):
        failures.append(f"{screen}/{name}: missing render")
    for name in sorted(set(got) - set(want)):
        failures.append(f"{screen}/{name}: extra render (not in the refs)")
    for name in sorted(n for n in got if not NAME.match(n)):
        failures.append(f"{screen}/{name}: name is not <state>-<dark|light>.png")
    imgs: dict[str, Image.Image] = {}
    for name in sorted(set(got) & set(want)):
        try:
            im = Image.open(got[name])
            im.load()
        except Exception as exc:  # noqa: BLE001 - one failure line, no traceback
            failures.append(f"{screen}/{name}: unreadable PNG ({type(exc).__name__})")
            continue
        ref = None
        if compare:
            try:
                ref = Image.open(want[name])
                ref.load()
            except Exception as exc:  # noqa: BLE001
                _log(f"design_gate: ref {want[name]} unreadable: {exc}")
        size = ref.size if ref is not None else DEFAULT_SIZE
        if im.size != size:
            failures.append(
                f"{screen}/{name}: size {im.size[0]}x{im.size[1]}, "
                f"expected {size[0]}x{size[1]}"
            )
            continue
        if blank(im):
            failures.append(f"{screen}/{name}: blank render (one colour)")
            continue
        if ref is not None and same(im, ref):
            failures.append(f"{screen}/{name}: identical to the current app")
        imgs[name] = im
    for name, im in imgs.items():
        m = NAME.match(name)
        if m and m["theme"] == "dark":
            light = imgs.get(f"{m['state']}-light")
            if light is not None and same(im, light):
                failures.append(
                    f"{screen}/{m['state']}: dark and light renders are identical"
                )
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description="Gate one design-loop variant.")
    ap.add_argument("--worktree", type=Path, default=Path("."))
    ap.add_argument("--renders", required=True, type=Path)
    ap.add_argument("--refs", required=True, type=Path)
    ap.add_argument("--screen", required=True)
    a = ap.parse_args()
    try:
        failures = check(a.screen, a.worktree, a.renders, a.refs)
    except Exception as exc:  # noqa: BLE001 - a crash is still one JSON answer
        failures = [f"{a.screen}: gate error {type(exc).__name__}: {exc}"]
    for f in failures:
        _log(f" - {f}")
    print(json.dumps(failures))
    return 0


if __name__ == "__main__":
    sys.exit(main())
