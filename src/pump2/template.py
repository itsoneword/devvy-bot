"""The one ``{{KEY}}`` template filler: the design loop's generator prompt and
pump2's custom stage prompts (dv-3b99). A key with no value stays as written."""

from __future__ import annotations

from collections.abc import Mapping


def fill_template(text: str, values: Mapping[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text
