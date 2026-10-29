"""One bot message as data: text, parse mode, keyboard rows, timing."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Button:
    text: str
    callback_data: Optional[str] = None


@dataclass
class Screen:
    id: str
    text: str
    parse_mode: Optional[str]
    rows: List[List[Button]] = field(default_factory=list)
    #: How long the handler took to produce it; None for a frozen fixture or
    #: a pure builder nobody timed.
    render_ms: Optional[float] = None
    #: What the user came to this screen to do ("pick the chat's model").
    goal: str = ""

    @classmethod
    def from_markup(
        cls,
        id: str,
        text: str,
        markup: Any = None,
        parse_mode: Optional[str] = None,
        render_ms: Optional[float] = None,
        goal: str = "",
    ) -> "Screen":
        """From a reply's text and ``InlineKeyboardMarkup`` (or None)."""
        keyboard = getattr(markup, "inline_keyboard", None) or ()
        rows = [
            [
                Button(
                    str(b.text),
                    b.callback_data if isinstance(b.callback_data, str) else None,
                )
                for b in row
            ]
            for row in keyboard
        ]
        return cls(id, text or "", parse_mode, rows, render_ms, goal)

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "Screen":
        """A frozen screen; keys other than the fields (``source``) are notes."""
        rows = [
            [Button(b["text"], b.get("callback_data")) for b in row]
            for row in data.get("rows") or []
        ]
        return cls(
            id=data["id"],
            text=data.get("text") or "",
            parse_mode=data.get("parse_mode"),
            rows=rows,
            render_ms=data.get("render_ms"),
            goal=data.get("goal") or "",
        )

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "goal": self.goal,
            "parse_mode": self.parse_mode,
            "text": self.text,
            "rows": [
                [{"text": b.text, "callback_data": b.callback_data} for b in row]
                for row in self.rows
            ],
            "render_ms": self.render_ms,
        }
