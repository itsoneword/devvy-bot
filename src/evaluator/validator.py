"""Evaluator output validation: memory ops against a strict schema."""

import json
from typing import Dict, List, Optional

import structlog

logger = structlog.get_logger()

VALID_OPS = {"add", "supersede", "invalidate", "flag_conflict"}
VALID_TIERS = {"pinned", "core", "general"}
VALID_SCOPES = {"private", "family"}
VALID_KINDS = {"user_fact", "conversation_result"}
MAX_CONTENT_LENGTH = 2000


def validate_evaluator_output(
    raw_json: str, allow_family: bool = False
) -> List[Dict[str, object]]:
    """Validate evaluator JSON into memory ops.

    Accepts a JSON array, or an object {"extractions": [...]} (Codex structured
    output). Raises ValueError/json.JSONDecodeError only when the response is
    structurally unusable (bad JSON, or not a list/{"extractions": [...]}).

    Individual malformed *entries* are handled defensively instead of
    aborting the whole batch — one bad op from the LLM must not discard
    every other valid fact extracted from the same conversation window
    (this matters a lot right at a 150k-token refresh: the window rolls
    immediately after, so a discarded batch means those facts are gone for
    good, not just delayed to the next checkpoint):
    - a "supersede" with a missing/non-integer 'supersedes_id' is downgraded
      to "add" — the content is still a real fact worth keeping, it's just
      not linked to an existing row.
    - an "invalidate"/"flag_conflict" with missing/non-integer ids is skipped
      with a warning — these ops carry no content, so there is nothing to
      save by downgrading.
    - any other malformed entry (bad op/tier/scope/content/label) is skipped
      with a warning log; the rest of the batch still applies.

    Returns dicts: {op, tier, content, label, supersedes_id, scope, kind,
    source_ref, target_id, ids}.

    ``kind`` defaults to "user_fact" when absent or invalid — defensive: a
    model that forgets ``kind`` must not lose the fact.

    ``scope`` defaults to "private". "family" is only honored when
    ``allow_family`` is True (the speaking user belongs to a family);
    otherwise it is conservatively coerced back to "private" — a user
    without a family can never write into a family scope.
    """
    cleaned = raw_json.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    data = json.loads(cleaned)
    if isinstance(data, dict) and "extractions" in data:
        data = data["extractions"]
    if not isinstance(data, list):
        raise ValueError("Expected JSON array or {extractions: [...]}")

    validated: List[Dict[str, object]] = []
    for entry in data:
        try:
            op_dict = _validate_entry(entry, allow_family=allow_family)
        except ValueError as e:
            logger.warning("Skipping malformed evaluator op", entry=entry, error=str(e))
            continue
        validated.append(op_dict)
    return validated


def _validate_entry(entry: object, *, allow_family: bool) -> Dict[str, object]:
    """Validate a single op entry.

    Raises ValueError for anything the caller should skip entirely. A
    "supersede" with a bad/missing id is NOT raised here — it's downgraded
    to "add" so the fact survives instead of being dropped.
    """
    if not isinstance(entry, dict):
        raise ValueError("Expected dict entry")
    op = entry.get("op")
    if op not in VALID_OPS:
        raise ValueError(f"Invalid op: {op}")

    # Codex structured output may emit an explicit null scope.
    scope = entry.get("scope") or "private"
    if scope not in VALID_SCOPES:
        raise ValueError(f"Invalid scope: {scope}")
    if scope == "family" and not allow_family:
        scope = "private"

    # Content-less metadata ops. Any content the model attached is stripped:
    # the structural guarantee that no op rewrites an existing row's text
    # lives here, not in the prompt.
    if op == "invalidate":
        target_id = entry.get("target_id")
        if not isinstance(target_id, int):
            raise ValueError("invalidate requires integer 'target_id'")
        return {"op": "invalidate", "target_id": target_id, "scope": scope}
    if op == "flag_conflict":
        ids = entry.get("ids")
        if (
            not isinstance(ids, list)
            or len(ids) != 2
            or not all(isinstance(i, int) for i in ids)
        ):
            raise ValueError("flag_conflict requires 'ids' of exactly 2 integers")
        return {"op": "flag_conflict", "ids": ids, "scope": scope}

    tier = entry.get("tier")
    content = entry.get("content")
    label: Optional[str] = entry.get("label")
    supersedes_id = entry.get("supersedes_id")
    kind = entry.get("kind") or "user_fact"
    if kind not in VALID_KINDS:
        kind = "user_fact"
    source_ref = entry.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref.strip():
        source_ref = None

    if tier not in VALID_TIERS:
        raise ValueError(f"Invalid tier: {tier}")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Missing/empty 'content'")
    if len(content) > MAX_CONTENT_LENGTH:
        raise ValueError(f"Content too large: {len(content)} chars")
    if label is not None and not isinstance(label, str):
        raise ValueError("'label' must be a string")

    if op == "supersede" and not isinstance(supersedes_id, int):
        logger.warning(
            "supersede missing/invalid supersedes_id, downgrading to add",
            entry=entry,
        )
        op = "add"
        supersedes_id = None

    out: Dict[str, object] = {
        "op": op,
        "tier": tier,
        "content": content,
        "label": label,
        # Kept on "add" too: post-R5 the extraction prompt emits candidates
        # with supersedes_id as a HINT for the reconciler, not a decision.
        "supersedes_id": supersedes_id if isinstance(supersedes_id, int) else None,
        "scope": scope,
        "kind": kind,
        "source_ref": source_ref,
    }
    # Screen-path fields (dv-7e11 step c). Only the targeted prompt asks for
    # them; they are left out unless set, so the classic path's ops are
    # unchanged.
    if entry.get("revokes") is True:
        out["revokes"] = True
    conflicts_with_id = entry.get("conflicts_with_id")
    if isinstance(conflicts_with_id, int) and not isinstance(conflicts_with_id, bool):
        out["conflicts_with_id"] = conflicts_with_id
    return out
