"""Buttons of a pushed card (the «ready to publish» card, ``publish_card``).

Pure: no telegram import. Buttons are plain rows of ``{"text",
"callback_data"}``; ``/internal/notify`` carries them and the notification
service turns them into an inline keyboard. ``clean_buttons`` is the one
filter a push passes: only a known card prefix survives.
"""

from typing import Any, Dict, List

# Every card prefix a notify may carry (``publish_card``, dv-b05e; the
# feature-feedback score buttons ``fb:<feature_id>:<n|nt|skip>``, dv-a42c).
CARD_PREFIXES = ("publish:", "fb:")
_CALLBACK_BYTES = 64

Button = Dict[str, str]
Rows = List[List[Button]]


def button(text: str, data: str) -> Button:
    assert len(data.encode()) <= _CALLBACK_BYTES, data
    return {"text": text, "callback_data": data}


def clean_buttons(rows: Any) -> Rows:
    """Only card buttons (``CARD_PREFIXES``) survive a push; anything else —
    malformed, foreign callback data — is dropped."""
    out: Rows = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, list):
            continue
        kept = [
            {"text": str(b["text"]), "callback_data": str(b["callback_data"])}
            for b in row
            if isinstance(b, dict)
            and isinstance(b.get("text"), str)
            and isinstance(b.get("callback_data"), str)
            and b["callback_data"].startswith(CARD_PREFIXES)
            and len(b["callback_data"].encode()) <= _CALLBACK_BYTES
        ]
        if kept:
            out.append(kept)
    return out
