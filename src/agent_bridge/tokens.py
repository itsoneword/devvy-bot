"""Per-turn bearer tokens for the agent-tools bridge.

A token is minted when a turn starts, handed to that turn's child process in
its own environment, and revoked when the turn returns. It resolves — server
side, never from anything the model sent — to the :class:`TurnContext` the
turn is allowed to act as.

Why per-turn and not a shared secret like ``CHANNEL_INTEL_TOKEN``: every
agent tool is scoped to one user and one profile. A long-lived token would
let any Codex turn read and write every user's memory and food log, which is
exactly the boundary dv-6dbc closed on the SDK path.

Three rules this module exists to keep:

* the raw token is never stored — only its SHA-256 — so nothing that dumps
  this object's state (a repr, a traceback, a heap inspection) leaks a
  working credential;
* the token is never logged, in any form, at any level (docs/FINDINGS.md
  2026-08-04: a token reached journald and had to be rotated). Log lines use
  ``lease_id``, a prefix of the hash, which is useless to an attacker;
* an unknown or expired token resolves to ``None``. There is no default
  context, so a failure to authenticate can never become "acts as somebody".
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

import structlog

from src.claude.tool_context import TurnContext

logger = structlog.get_logger()

# Backstop only. The normal path is mint -> use -> revoke inside one
# ``run_command``; the TTL exists because a crashed or killed turn may never
# reach its revoke, and a token that outlives its turn forever is a standing
# credential (see the docstring). Comfortably longer than
# ``AGENT_TIMEOUT_SECONDS`` so it never expires under a turn that is still
# running.
DEFAULT_TTL_SECONDS = 1800.0


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Lease:
    context: TurnContext
    expires_at: float


class TokenRegistry:
    """In-memory ``token -> TurnContext`` table with a TTL backstop."""

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._leases: Dict[str, _Lease] = {}

    def __len__(self) -> int:
        return len(self._leases)

    @staticmethod
    def lease_id(token: str) -> str:
        """A loggable handle for a token: the first bytes of its hash."""
        return _hash(token)[:8]

    def mint(self, context: TurnContext) -> str:
        """Issue a token for one turn. The caller owns revoking it."""
        self.purge_expired()
        token = secrets.token_urlsafe(32)
        self._leases[_hash(token)] = _Lease(
            context=context, expires_at=self._clock() + self._ttl
        )
        logger.debug(
            "agent bridge token minted",
            lease_id=self.lease_id(token),
            profile=context.profile_slug,
            leases=len(self._leases),
        )
        return token

    def resolve(self, token: Optional[str]) -> Optional[TurnContext]:
        """The context this token acts as, or None if unknown or expired.

        None is the only failure mode: there is deliberately no fallback
        context to return.
        """
        if not token:
            return None
        lease = self._leases.get(_hash(token))
        if lease is None:
            return None
        if lease.expires_at <= self._clock():
            self._leases.pop(_hash(token), None)
            logger.info("agent bridge token expired", lease_id=self.lease_id(token))
            return None
        return lease.context

    def revoke(self, token: Optional[str]) -> bool:
        """Drop a token. Idempotent, so a double-revoke is not an error."""
        if not token:
            return False
        removed = self._leases.pop(_hash(token), None) is not None
        if removed:
            logger.debug(
                "agent bridge token revoked",
                lease_id=self.lease_id(token),
                leases=len(self._leases),
            )
        return removed

    def purge_expired(self) -> int:
        """Drop every expired lease; returns how many went."""
        now = self._clock()
        stale = [key for key, lease in self._leases.items() if lease.expires_at <= now]
        for key in stale:
            self._leases.pop(key, None)
        return len(stale)

    def clear(self) -> None:
        self._leases.clear()
