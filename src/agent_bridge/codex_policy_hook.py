#!/usr/bin/env python3
"""Codex PreToolUse hook: ask the bot whether this tool call may run (dv-1601).

Codex runs this before every shell command, file patch and MCP call, with the
call as JSON on stdin. It forwards the call to the bot's policy endpoint
(``src/agent_bridge/policy_gate.py``) and prints a deny decision unless the bot
answered ``allow``. All the rules live in the bot; this file decides nothing.

Standard library only, and it must not import ``src``: it runs as a bare script
in the Codex child's environment. It fails closed: an unreadable payload,
a missing endpoint or an unreachable bot is a deny.
"""

import json
import os
import sys
import urllib.request

# The owner has 120 s to answer a confirmation; Codex kills the hook at 300 s.
REQUEST_TIMEOUT_SECONDS = 280


def deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        deny("Blocked: the tool call could not be read by the policy hook.")
    url = os.environ.get("DEVVY_POLICY_URL")
    token = os.environ.get("AGENT_BRIDGE_TOKEN")
    if not url or not token:
        deny("Blocked: no policy endpoint is configured for this turn.")
    body = json.dumps(
        {
            "tool_name": payload.get("tool_name"),
            "tool_input": payload.get("tool_input"),
            "tool_use_id": payload.get("tool_use_id"),
        }
    ).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    # Loopback only: never route this through a configured proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            answer = json.load(response)
    except Exception:
        deny("Blocked: the bot's policy service did not answer.")
    if answer.get("decision") == "allow":
        sys.exit(0)
    deny(str(answer.get("reason") or "Blocked by policy."))


if __name__ == "__main__":
    main()
