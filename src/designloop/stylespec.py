"""A board's style spec in web-design terms.

One YAML file per board (``design.style_spec`` in ``boards.json``, next to
the references), written so a mockup prompt and the critic's dimensions can
be assembled from it by code, not "two blue buttons":

```yaml
status: draft | approved          # a draft steers nothing
palette:    {<role>: {hex: "#7A9E7E", role: "buttons, active tab"}}
typography: {family: Inter, roles: {<role>: {weight: 700, size: 22}}}
radii:      {<element>: <px>}
borders:    {<element>: {width: <px>, color: "#RRGGBB[AA]"}}
elevation:  {<element>: {offset_y: <px>, blur: <px>, opacity: 0..1}}
glass:      {blur: <px>, tint: "#RRGGBBAA", highlight: "#RRGGBBAA"}
spacing:    [4, 8, 12, 16, 24]    # the scale, px
components: {<name>: {description: "...", states: {<state>: "..."}}}
```

:func:`parse_spec` refuses a missing section or a malformed value with
:class:`StyleSpecError` naming the section. :meth:`StyleSpec.look_block` is
the deterministic "look" part of every mockup prompt; :meth:`StyleSpec.text`
the spec as the critic reads it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple, Union

import yaml

STATUSES = ("draft", "approved")
SECTIONS = (
    "palette",
    "typography",
    "radii",
    "borders",
    "elevation",
    "glass",
    "spacing",
    "components",
)
_HEX = re.compile(r"^#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")


class StyleSpecError(ValueError):
    """The spec is unusable; the message names the section."""


@dataclass(frozen=True)
class StyleSpec:
    status: str
    palette: Mapping[str, Mapping[str, str]]
    typography: Mapping[str, Any]
    radii: Mapping[str, float]
    borders: Mapping[str, Mapping[str, Any]]
    elevation: Mapping[str, Mapping[str, float]]
    glass: Mapping[str, Any]
    spacing: Tuple[float, ...]
    components: Mapping[str, Mapping[str, Any]]
    raw: str = field(repr=False, default="")

    @property
    def approved(self) -> bool:
        return self.status == "approved"

    def look_block(self) -> str:
        """The fixed "look" sentence block of a mockup prompt: same spec, same
        text, so the style does not drift between mockups or runs."""
        colours = "; ".join(
            (
                f"{name} {v['hex']} ({v['role']})"
                if v.get("role")
                else f"{name} {v['hex']}"
            )
            for name, v in self.palette.items()
        )
        roles = self.typography.get("roles") or {}
        type_roles = "; ".join(
            f"{name} {_num(r.get('size'))}px weight {_num(r.get('weight'))}"
            for name, r in roles.items()
        )
        radii = ", ".join(f"{k} {_num(v)}px" for k, v in self.radii.items())
        borders = ", ".join(
            f"{k} {_num(v.get('width'))}px {v.get('color', '')}".rstrip()
            for k, v in self.borders.items()
        )
        shadows = ", ".join(
            f"{k} y{_num(v.get('offset_y', 0))} blur {_num(v.get('blur'))} "
            f"opacity {_num(v.get('opacity'))}"
            for k, v in self.elevation.items()
        )
        glass = ", ".join(f"{k} {_num(v)}" for k, v in self.glass.items())
        spacing = ", ".join(_num(s) for s in self.spacing)
        lines = [
            "LOOK: modern mobile app UI, crisp vector rendering, flat light, "
            "no device frame, no watermark, no logo.",
            f"Colour palette: {colours}.",
            f"Typography: {self.typography.get('family')}; {type_roles}.",
            f"Corner radii: {radii}.",
            f"Borders: {borders}.",
            f"Shadows: {shadows}.",
            f"Glass surfaces: {glass}.",
            f"Spacing scale: {spacing} px.",
        ]
        return "\n".join(lines)

    def component_line(self, name: str) -> str:
        """One component's description and states, "" when unknown."""
        comp = self.components.get(name)
        if not comp:
            return ""
        states = comp.get("states") or {}
        tail = "; ".join(f"{k}: {v}" for k, v in states.items())
        return f"{name}: {comp.get('description', '')}" + (
            f" (states: {tail})" if tail else ""
        )

    def text(self) -> str:
        """The spec as the critic reads it: the file itself."""
        return self.raw.strip() or yaml.safe_dump(self.as_dict(), sort_keys=False)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "palette": dict(self.palette),
            "typography": dict(self.typography),
            "radii": dict(self.radii),
            "borders": dict(self.borders),
            "elevation": dict(self.elevation),
            "glass": dict(self.glass),
            "spacing": list(self.spacing),
            "components": dict(self.components),
        }


def _num(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _mapping(data: Mapping[str, Any], section: str) -> Mapping[str, Any]:
    value = data.get(section)
    if value is None:
        raise StyleSpecError(f"{section}: section missing")
    if not isinstance(value, dict) or not value:
        raise StyleSpecError(f"{section}: must be a non-empty mapping")
    return value


def _hex(value: Any, section: str, where: str) -> str:
    if not isinstance(value, str) or not _HEX.match(value.strip()):
        raise StyleSpecError(f"{section}.{where}: {value!r} is not a hex colour")
    return value.strip().upper()


def parse_spec(text: str) -> StyleSpec:
    """The YAML text as a :class:`StyleSpec`; :class:`StyleSpecError` names
    the first bad section."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise StyleSpecError(f"spec: not YAML ({exc})") from exc
    if not isinstance(data, dict):
        raise StyleSpecError("spec: must be a YAML mapping")
    status = str(data.get("status") or "draft")
    if status not in STATUSES:
        raise StyleSpecError(f"status: {status!r} is not draft|approved")

    palette: Dict[str, Dict[str, str]] = {}
    for name, value in _mapping(data, "palette").items():
        if isinstance(value, str):
            value = {"hex": value}
        if not isinstance(value, dict):
            raise StyleSpecError(f"palette.{name}: needs hex and role")
        palette[str(name)] = {
            "hex": _hex(value.get("hex"), "palette", str(name)),
            "role": str(value.get("role") or "").strip(),
        }

    typography = dict(_mapping(data, "typography"))
    if not str(typography.get("family") or "").strip():
        raise StyleSpecError("typography.family: missing")
    roles = typography.get("roles")
    if not isinstance(roles, dict) or not roles:
        raise StyleSpecError("typography.roles: needs weight and size per role")
    for name, role in roles.items():
        if not isinstance(role, dict) or not (
            _is_num(role.get("weight")) and _is_num(role.get("size"))
        ):
            raise StyleSpecError(f"typography.roles.{name}: needs weight and size")

    radii = dict(_mapping(data, "radii"))
    for name, value in radii.items():
        if not _is_num(value):
            raise StyleSpecError(f"radii.{name}: must be a number (px)")

    borders: Dict[str, Dict[str, Any]] = {}
    for name, value in _mapping(data, "borders").items():
        if not isinstance(value, dict) or not _is_num(value.get("width")):
            raise StyleSpecError(f"borders.{name}: needs width and color")
        borders[str(name)] = dict(
            value, color=_hex(value.get("color"), "borders", str(name))
        )

    elevation: Dict[str, Dict[str, float]] = {}
    for name, value in _mapping(data, "elevation").items():
        if not isinstance(value, dict) or not all(
            _is_num(value.get(k)) for k in ("blur", "opacity")
        ):
            raise StyleSpecError(f"elevation.{name}: needs offset_y, blur, opacity")
        if not 0 <= float(value["opacity"]) <= 1:
            raise StyleSpecError(f"elevation.{name}.opacity: must be 0..1")
        elevation[str(name)] = dict(value)

    glass = dict(_mapping(data, "glass"))
    if not _is_num(glass.get("blur")):
        raise StyleSpecError("glass.blur: must be a number (px)")
    for key in ("tint", "highlight"):
        glass[key] = _hex(glass.get(key), "glass", key)

    spacing_raw = data.get("spacing")
    if spacing_raw is None:
        raise StyleSpecError("spacing: section missing")
    if (
        not isinstance(spacing_raw, list)
        or not spacing_raw
        or not all(_is_num(s) for s in spacing_raw)
    ):
        raise StyleSpecError("spacing: must be a list of px values")

    components: Dict[str, Dict[str, Any]] = {}
    for name, value in _mapping(data, "components").items():
        if (
            not isinstance(value, dict)
            or not str(value.get("description") or "").strip()
        ):
            raise StyleSpecError(f"components.{name}: needs a description")
        states = value.get("states") or {}
        if not isinstance(states, dict):
            raise StyleSpecError(f"components.{name}.states: must be a mapping")
        components[str(name)] = dict(value)

    return StyleSpec(
        status=status,
        palette=palette,
        typography=typography,
        radii=radii,
        borders=borders,
        elevation=elevation,
        glass=glass,
        spacing=tuple(spacing_raw),
        components=components,
        raw=text,
    )


def load_spec(path: Union[str, Path]) -> StyleSpec:
    """Read and parse; ``FileNotFoundError`` when the file is missing."""
    return parse_spec(Path(path).read_text(encoding="utf-8"))


def set_status(text: str, status: str) -> str:
    """``text`` with its top-level ``status:`` line set (added when absent),
    the rest of the file byte-for-byte."""
    if status not in STATUSES:
        raise ValueError(status)
    lines: List[str] = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if re.match(r"^status\s*:", line):
            lines[i] = f"status: {status}\n"
            return "".join(lines)
    return f"status: {status}\n" + text
