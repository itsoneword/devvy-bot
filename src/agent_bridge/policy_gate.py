"""The policy endpoint a subprocess backend's PreToolUse hook calls (dv-1601).

Codex runs as a separate process, so its hook (``codex_policy_hook.py``) cannot
call ``src/security/policy.py`` directly. It POSTs each tool call here with the
turn's bridge token; the gate resolves the turn, runs the same ``decide()`` the
Claude hook runs, and turns an ``ask`` into the owner's Approve/Deny button.

Fail-closed rules:

1. No resolver, no working directory or a crash in the policy -> deny.
2. An ``ask`` with no confirmation manager -> deny.
3. The answer is only ever ``allow`` or ``deny``; the hook never decides.
4. The bridge runs a tool whose call ``decide()`` answers ``ask`` only with an
   owner approval this gate granted for that exact call in that turn
   (:func:`check_tool_call`). A hook that never ran (not mounted, failed to
   start -- codex may then go ahead) does not let a money move through.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import structlog

from src.claude.tool_context import TurnContext
from src.security import policy

logger = structlog.get_logger()

POLICY_PATH = "/policy/check"
# The env var the hook reads the endpoint URL from (set on the child only).
POLICY_URL_ENV_VAR = "DEVVY_POLICY_URL"

_MAX_REMEMBERED = 512

PolicyResolver = Callable[[TurnContext], policy.TurnPolicy]

# The name both backends mount the bot's own tools under (``mcp__agent__x``).
AGENT_SERVER = "agent"


def _call_key(tool_name: str, tool_input: Dict[str, Any]) -> str:
    """One tool call's identity: bare tool name + its arguments."""
    name = tool_name.split("__")[-1] if tool_name.startswith("mcp__") else tool_name
    args = tool_input.get("arguments") if isinstance(tool_input, dict) else None
    if not isinstance(args, dict):
        args = tool_input
    return name + "\0" + json.dumps(args, sort_keys=True, default=str)


class PolicyGate:
    """Decide one hook call: allow or deny, asking the owner when needed."""

    def __init__(
        self,
        resolver: PolicyResolver,
        confirmation_manager: Any = None,
    ) -> None:
        self._resolver = resolver
        self._confirmation_manager = confirmation_manager
        # Owner approvals not yet used by the bridge: (turn key, call key).
        self._approved: "Counter[Tuple[str, str]]" = Counter()
        # Codex can fire the hook twice for one tool call (seen on 0.157.1):
        # the second call must get the first one's answer, not a second button.
        self._calls: "OrderedDict[Tuple[str, str], asyncio.Task[Dict[str, str]]]" = (
            OrderedDict()
        )

    async def check(
        self, turn_key: str, ctx: TurnContext, body: Dict[str, Any]
    ) -> Dict[str, str]:
        tool_use_id = str(body.get("tool_use_id") or "")
        if not tool_use_id:
            return await self._check(turn_key, ctx, body)
        key = (turn_key, tool_use_id)
        task = self._calls.get(key)
        if task is None:
            task = asyncio.ensure_future(self._check(turn_key, ctx, body))
            self._calls[key] = task
            while len(self._calls) > _MAX_REMEMBERED:
                self._calls.popitem(last=False)
        return await asyncio.shield(task)

    async def _check(
        self, turn_key: str, ctx: TurnContext, body: Dict[str, Any]
    ) -> Dict[str, str]:
        tool_name = str(body.get("tool_name") or "")
        tool_input = body.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        if ctx.working_dir is None:
            return _deny("this turn has no working directory")
        try:
            decision = policy.decide(tool_name, tool_input, self._resolver(ctx))
        except Exception as exc:
            logger.warning(
                "policy gate failed -> denied",
                tool_name=tool_name,
                profile=ctx.profile_slug,
                error=str(exc),
            )
            return _deny("the policy check failed")

        if decision.action == policy.ALLOW:
            return {"decision": policy.ALLOW, "reason": ""}
        if decision.action == policy.DENY:
            logger.warning(
                "PreToolUse hook denied tool call",
                backend="codex",
                tool_name=tool_name,
                profile=ctx.profile_slug,
                reason=decision.reason,
            )
            return _deny(decision.reason)

        if self._confirmation_manager is None:
            return _deny(
                f"{decision.reason}: needs the owner's confirmation, "
                "which is not available"
            )
        approved = await self._confirmation_manager.request(
            decision.reason, decision.preview
        )
        if not approved:
            logger.warning(
                "Destructive op denied by confirmation gate",
                backend="codex",
                tool_name=tool_name,
                profile=ctx.profile_slug,
                reason=decision.reason,
            )
            return _deny(f"Denied by owner (confirmation gate): {decision.reason}")
        if tool_name.startswith(f"mcp__{AGENT_SERVER}__"):
            self._approved[(turn_key, _call_key(tool_name, tool_input))] += 1
            while len(self._approved) > _MAX_REMEMBERED:
                del self._approved[next(iter(self._approved))]
        return {"decision": policy.ALLOW, "reason": ""}

    def turn_policy(self, ctx: TurnContext) -> policy.TurnPolicy:
        return self._resolver(ctx)

    def take_approval(self, turn_key: str, tool_name: str, args: Dict[str, Any]) -> bool:
        """Use up one owner approval for this exact call; False if none."""
        key = (turn_key, _call_key(tool_name, args))
        if self._approved.get(key, 0) <= 0:
            return False
        self._approved[key] -= 1
        if self._approved[key] <= 0:
            del self._approved[key]
        return True


def check_tool_call(
    gate: Optional[PolicyGate],
    turn_key: str,
    ctx: TurnContext,
    name: str,
    args: Dict[str, Any],
) -> Optional[str]:
    """Why the bridge must refuse to run tool ``name`` now, or None to run it.

    The same ``decide()`` the hook ran: ``allow`` runs; ``deny`` refuses;
    ``ask`` runs only on an approval the hook already got from the owner.
    """
    tool_name = f"mcp__{AGENT_SERVER}__{name}"
    try:
        turn = (
            gate.turn_policy(ctx)
            if gate is not None
            else policy.TurnPolicy(working_directory=ctx.working_dir or Path.cwd())
        )
        decision = policy.decide(tool_name, args, turn)
    except Exception as exc:
        logger.warning("bridge policy check failed -> refused", tool=name, error=str(exc))
        return "the policy check failed"
    if decision.action == policy.ALLOW:
        return None
    if decision.action == policy.DENY:
        return decision.reason
    if gate is not None and gate.take_approval(turn_key, tool_name, args):
        return None
    return f"{decision.reason}: needs the owner's approval, which this call does not have"


def _deny(reason: str) -> Dict[str, str]:
    return {"decision": policy.DENY, "reason": reason}


def gate_for(app_state: Any) -> Optional[PolicyGate]:
    gate = getattr(app_state, "policy_gate", None)
    return gate if isinstance(gate, PolicyGate) else None
