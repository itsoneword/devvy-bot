"""Rate limiting implementation with multiple strategies.

Features:
- Token bucket algorithm
- Cost-based limiting
- Per-user tracking
- Burst handling
"""

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import structlog

from ..config.settings import Settings

logger = structlog.get_logger()

WATCHDOG_WINDOW = timedelta(hours=1)


@dataclass
class WatchdogAlert:
    """One hourly threshold crossed for one user (dv-8bb5)."""

    user_id: int
    # "cost_per_hour" | "messages_per_hour" | "turns_per_hour" | "tokens_per_hour"
    metric: str
    value: float
    threshold: float


@dataclass
class RateLimitBucket:
    """Token bucket for rate limiting."""

    capacity: int
    tokens: float
    last_update: datetime
    refill_rate: float = 1.0  # tokens per second

    def consume(self, tokens: int = 1) -> bool:
        """Try to consume tokens from bucket."""
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    def _refill(self) -> None:
        """Refill tokens based on time passed."""
        now = datetime.now(UTC)
        elapsed = (now - self.last_update).total_seconds()
        self.tokens = min(self.capacity, self.tokens + (elapsed * self.refill_rate))
        self.last_update = now

    def get_wait_time(self, tokens: int = 1) -> float:
        """Get time to wait before tokens are available."""
        self._refill()
        if self.tokens >= tokens:
            return 0.0

        tokens_needed = tokens - self.tokens
        return tokens_needed / self.refill_rate

    def get_status(self) -> Dict[str, float]:
        """Get current bucket status."""
        self._refill()
        return {
            "capacity": self.capacity,
            "tokens": self.tokens,
            "utilization": (self.capacity - self.tokens) / self.capacity,
            "refill_rate": self.refill_rate,
        }


class RateLimiter:
    """Main rate limiting system with request and cost-based limits."""

    def __init__(self, config: Settings):
        self.config = config
        self.request_buckets: Dict[int, RateLimitBucket] = {}
        self.cost_tracker: Dict[int, float] = defaultdict(float)
        self.cost_reset_time: Dict[int, datetime] = {}
        # Unmetered-backend ledger (dv-0722): tokens spent on turns that
        # carry no USD price (ClaudeResponse.cost is None -- Codex on a
        # ChatGPT subscription). Same daily window and reset anchor as
        # cost_tracker; a user is blocked when EITHER ledger is exhausted.
        # NOT seeded from SQLite at startup (nothing persists these tokens
        # per day yet), so a restart zeroes it.
        self.token_tracker: Dict[int, int] = defaultdict(int)
        self.locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

        # Session watchdog (dv-8bb5): a second, independent hourly window per
        # user tracking real spend/turns/completed-messages -- separate from
        # cost_tracker above, which is a *daily* budget that owners are
        # exempt from. Fixed reset (like _maybe_reset_cost_tracker), not a
        # true sliding window: simpler, and a late alert is an acceptable
        # failure mode for a soft signal.
        self.watchdog_hour_cost: Dict[int, float] = defaultdict(float)
        self.watchdog_hour_messages: Dict[int, int] = defaultdict(int)
        self.watchdog_hour_turns: Dict[int, int] = defaultdict(int)
        self.watchdog_hour_tokens: Dict[int, int] = defaultdict(int)
        self.watchdog_hour_reset_time: Dict[int, datetime] = {}
        # Metrics already alerted on in the current window, so a sustained
        # overage fires once per metric per hour instead of on every turn.
        self._watchdog_alerted: Dict[int, Set[str]] = defaultdict(set)

        # Calculate refill rate from config
        self.refill_rate = (
            self.config.rate_limit_requests / self.config.rate_limit_window
        )

        logger.info(
            "Rate limiter initialized",
            requests_per_window=self.config.rate_limit_requests,
            window_seconds=self.config.rate_limit_window,
            burst_capacity=self.config.rate_limit_burst,
            max_cost_per_user=self.config.claude_max_cost_per_user,
            refill_rate=self.refill_rate,
        )

    async def check_rate_limit(
        self, user_id: int, cost: float = 1.0, tokens: int = 1, role: str = "owner"
    ) -> Tuple[bool, Optional[str]]:
        """Check if request is allowed under rate limits.

        ``role`` selects the cost cap tier: guests get the (lower)
        ``claude_max_cost_per_user_guest`` budget; everyone else the owner cap.
        """
        async with self.locks[user_id]:
            # Check request rate limit
            rate_allowed, rate_message = self._check_request_rate(user_id, tokens)
            if not rate_allowed:
                logger.warning(
                    "Request rate limit exceeded",
                    user_id=user_id,
                    tokens_requested=tokens,
                )
                return False, rate_message

            # Check cost limit
            cost_allowed, cost_message = self._check_cost_limit(user_id, cost, role)
            if not cost_allowed:
                logger.warning(
                    "Cost limit exceeded",
                    user_id=user_id,
                    role=role,
                    cost_requested=cost,
                    current_usage=self.cost_tracker[user_id],
                )
                return False, cost_message

            # If both checks pass, consume resources
            self._consume_request_tokens(user_id, tokens)
            self._track_cost(user_id, cost)

            logger.debug(
                "Rate limit check passed", user_id=user_id, cost=cost, tokens=tokens
            )
            return True, None

    def _check_request_rate(
        self, user_id: int, tokens: int
    ) -> Tuple[bool, Optional[str]]:
        """Check request rate limit."""
        bucket = self._get_or_create_bucket(user_id)

        if bucket.consume(tokens):
            return True, None

        wait_time = bucket.get_wait_time(tokens)
        status = bucket.get_status()

        message = (
            f"Rate limit exceeded. Please wait {wait_time:.1f} seconds "
            f"before making more requests. "
            f"Bucket: {status['tokens']:.1f}/{status['capacity']} tokens available."
        )
        return False, message

    def _cost_cap_for(self, role: str, user_id: Optional[int] = None) -> float:
        """Daily cost cap: per-user override first, then the role tier
        (guests get the lower guest cap)."""
        overrides = getattr(self.config, "claude_cost_limit_overrides", {}) or {}
        if user_id is not None and user_id in overrides:
            return float(overrides[user_id])
        if role == "guest":
            return self.config.claude_max_cost_per_user_guest
        return self.config.claude_max_cost_per_user

    def effective_daily_cap(self, user_id: int, role: str) -> Optional[float]:
        """The cap actually enforced for this user, or None if uncapped.

        Single source of truth for both the gate and the /usage readout — they
        drifted apart once already (owners were shown "no cap" while an
        override was, or should have been, enforced).
        """
        overrides = getattr(self.config, "claude_cost_limit_overrides", {}) or {}
        if user_id in overrides:
            return float(overrides[user_id])
        if role == "owner":
            return None
        return self._cost_cap_for(role, user_id)

    def _token_cap_for(self, role: str) -> int:
        """Daily unmetered-token cap for the role tier (dv-0722)."""
        if role == "guest":
            return int(self.config.unmetered_max_tokens_per_user_guest)
        return int(self.config.unmetered_max_tokens_per_user)

    def _token_gate_exempt(self, user_id: int, role: str) -> bool:
        """Whether this user is exempt from the unmetered-token hard cap.

        Token-ledger only — it must never be consulted for the USD gate, which
        has its own exemption rule in ``effective_daily_cap``. A per-user
        override is deliberately a USD-budget setting: owners must not be
        blocked by the local aggregate-token counter, which sums all model and
        tool flow rather than measuring a context window or a reliably
        attributable subscription cost.
        """
        return role == "owner"

    def effective_daily_token_cap(self, user_id: int, role: str) -> Optional[int]:
        """The unmetered-token cap enforced for this user, or None if uncapped.

        This intentionally differs from ``effective_daily_cap``: a USD
        override may budget an owner, but cannot enable the local token gate.
        /usage and the gate still share this one source of truth.
        """
        if self._token_gate_exempt(user_id, role):
            return None
        return self._token_cap_for(role)

    def _check_cost_limit(
        self, user_id: int, cost: float, role: str = "owner"
    ) -> Tuple[bool, Optional[str]]:
        """Check cost-based limit against the role's cap."""
        # cost=0 means "this update spends no LLM money" (commands, button
        # presses) — the budget gate must not apply, or an exhausted budget
        # locks the user out of /usage and /restart, the very commands needed
        # to see and fix the situation. The request-rate bucket still applies.
        if cost <= 0:
            return True, None

        # Reset cost tracker if enough time has passed. Must run before the
        # owner short-circuit below — it also anchors cost_reset_time on
        # first use; skipping it left that anchor unset and the very next
        # _maybe_reset_cost_tracker call (e.g. from get_user_status) saw no
        # anchor and immediately wiped the just-tracked cost.
        self._maybe_reset_cost_tracker(user_id)

        # Owners are uncapped BY DEFAULT: the daily budget is meant to bound
        # guest/family spend, not the owner's own use of their own bot —
        # hitting it just locks the owner out of their own account with no way
        # to self-serve a raise. Cost is still tracked (for /usage) via
        # _track_cost in check_rate_limit; only the block is skipped.
        #
        # An explicit per-user override wins over that exemption. Role tiers
        # cannot tell two owner-role users apart, which is the entire reason
        # CLAUDE_COST_LIMIT_OVERRIDES exists (e.g. primary owner $100/day,
        # spouse $50/day). Short-circuiting on role first silently voided
        # every override set on an owner.
        #
        # Both the gate and /usage read that decision from
        # effective_daily_cap: None there means "genuinely uncapped", and an
        # override makes it a number for owners too. Asking the token
        # ledger's exemption instead (role == "owner") voided every override
        # again — the exact regression this comment block records.
        usd_cap = self.effective_daily_cap(user_id, role)
        if usd_cap is None:
            return True, None

        cap = usd_cap
        current_cost = self.cost_tracker[user_id]
        if current_cost + cost > cap:
            remaining = max(0, cap - current_cost)
            message = (
                f"Cost limit exceeded. Remaining budget: ${remaining:.2f}. "
                f"Current usage: ${current_cost:.2f}/"
                f"${cap:.2f}"
            )
            return False, message

        # Unmetered ledger (dv-0722). There is no pre-turn token estimate
        # worth having (the USD one is a ~$0.001 placeholder anyway), so the
        # gate is "already exhausted", trued up after each turn by
        # record_tokens. Checked regardless of which backend the coming turn
        # will use: the budget is per person, not per backend.
        token_cap = self.effective_daily_token_cap(user_id, role)
        if token_cap is None:
            return True, None
        used_tokens = self.token_tracker[user_id]
        if used_tokens >= token_cap:
            message = (
                "Token budget exceeded (unmetered backend). "
                f"Current usage: {used_tokens:,}/{token_cap:,} tokens"
            )
            return False, message

        return True, None

    def _consume_request_tokens(self, user_id: int, tokens: int) -> None:
        """Consume tokens from request bucket."""
        bucket = self._get_or_create_bucket(user_id)
        bucket.consume(tokens)

    def _track_cost(self, user_id: int, cost: float) -> None:
        """Track cost usage for user."""
        self.cost_tracker[user_id] += cost

        logger.debug(
            "Cost tracked",
            user_id=user_id,
            cost=cost,
            total_usage=self.cost_tracker[user_id],
        )

    def _get_or_create_bucket(self, user_id: int) -> RateLimitBucket:
        """Get or create rate limit bucket for user."""
        if user_id not in self.request_buckets:
            self.request_buckets[user_id] = RateLimitBucket(
                capacity=self.config.rate_limit_burst,
                tokens=self.config.rate_limit_burst,
                last_update=datetime.now(UTC),
                refill_rate=self.refill_rate,
            )
            logger.debug("Created rate limit bucket", user_id=user_id)

        return self.request_buckets[user_id]

    def _maybe_reset_cost_tracker(self, user_id: int) -> None:
        """Reset cost tracker if reset period has passed."""
        now = datetime.now(UTC)
        last_reset = self.cost_reset_time.get(user_id, now - timedelta(days=1))

        # Reset daily (configurable)
        reset_interval = timedelta(hours=24)
        if now - last_reset >= reset_interval:
            old_cost = self.cost_tracker[user_id]
            old_tokens = self.token_tracker[user_id]
            self.cost_tracker[user_id] = 0
            self.token_tracker[user_id] = 0
            self.cost_reset_time[user_id] = now

            if old_cost > 0 or old_tokens > 0:
                logger.info(
                    "Cost tracker reset",
                    user_id=user_id,
                    old_cost=old_cost,
                    old_tokens=old_tokens,
                    reset_time=now.isoformat(),
                )

    async def record_cost(self, user_id: int, cost: float) -> None:
        """Record REAL spend after a turn completes.

        check_rate_limit only ever consumed pre-turn estimates (~$0.001 per
        message), so the per-user budget was never enforced against actual
        cost — a $2 turn counted as a tenth of a cent. This is the true-up:
        call it with ClaudeResponse.cost once the turn is done.
        """
        if cost <= 0:
            return
        async with self.locks[user_id]:
            self._maybe_reset_cost_tracker(user_id)
            self._track_cost(user_id, cost)

    async def adjust_cost(self, user_id: int, delta: float) -> None:
        """Correct an already-recorded turn cost by ``delta`` USD (dv-d2f6:
        a third-party turn is recorded at its estimate, then settled to the
        provider's exact figure). Negative is fine; the day never goes < 0.
        A correction that lands after the daily reset applies to the new day.
        """
        if not delta:
            return
        async with self.locks[user_id]:
            self._maybe_reset_cost_tracker(user_id)
            self.cost_tracker[user_id] = max(self.cost_tracker[user_id] + delta, 0.0)

    async def record_tokens(self, user_id: int, tokens: int) -> None:
        """Record a turn that has NO USD price (dv-0722).

        The unmetered counterpart of ``record_cost``: call it with
        ``input_tokens + output_tokens`` when ``ClaudeResponse.cost`` is
        None. Shares the daily window with the USD ledger.
        """
        if tokens <= 0:
            return
        async with self.locks[user_id]:
            self._maybe_reset_cost_tracker(user_id)
            self.token_tracker[user_id] += int(tokens)
            logger.debug(
                "Unmetered tokens tracked",
                user_id=user_id,
                tokens=tokens,
                total_tokens=self.token_tracker[user_id],
            )

    def record_watchdog_turn(
        self, user_id: int, cost: Optional[float], turns: int, tokens: int = 0
    ) -> List[WatchdogAlert]:
        """Track one completed turn against the hourly watchdog thresholds.

        Fire-and-forget: this never blocks anything, it just reports which
        thresholds (if any) were newly crossed this window so the caller can
        alert the admin. Call once per completed turn with the REAL cost
        (post-turn, same as ``record_cost``) and ``ClaudeResponse.num_turns``.
        A turn with no USD price (``cost is None``, dv-0722) contributes its
        ``tokens`` to the ``tokens_per_hour`` metric instead of
        ``cost_per_hour``; a priced turn's tokens are ignored here because
        its dollars already cover it.
        """
        self._maybe_reset_watchdog_window(user_id)
        if cost is None:
            self.watchdog_hour_tokens[user_id] += max(int(tokens), 0)
        else:
            self.watchdog_hour_cost[user_id] += max(cost, 0.0)
        self.watchdog_hour_messages[user_id] += 1
        self.watchdog_hour_turns[user_id] += max(turns, 0)

        checks = (
            (
                "cost_per_hour",
                self.watchdog_hour_cost[user_id],
                self.config.watchdog_max_cost_per_hour,
            ),
            (
                "tokens_per_hour",
                float(self.watchdog_hour_tokens[user_id]),
                float(self.config.watchdog_max_tokens_per_hour),
            ),
            (
                "messages_per_hour",
                float(self.watchdog_hour_messages[user_id]),
                float(self.config.watchdog_max_messages_per_hour),
            ),
            (
                "turns_per_hour",
                float(self.watchdog_hour_turns[user_id]),
                float(self.config.watchdog_max_turns_per_hour),
            ),
        )

        already_alerted = self._watchdog_alerted[user_id]
        alerts: List[WatchdogAlert] = []
        for metric, value, threshold in checks:
            if value > threshold and metric not in already_alerted:
                already_alerted.add(metric)
                alerts.append(
                    WatchdogAlert(
                        user_id=user_id, metric=metric, value=value, threshold=threshold
                    )
                )
        if alerts:
            logger.warning(
                "Session watchdog threshold crossed",
                user_id=user_id,
                metrics=[a.metric for a in alerts],
            )
        return alerts

    def _maybe_reset_watchdog_window(self, user_id: int) -> None:
        """Reset the hourly watchdog window if it has elapsed."""
        now = datetime.now(UTC)
        last_reset = self.watchdog_hour_reset_time.get(user_id)
        if last_reset is None:
            self.watchdog_hour_reset_time[user_id] = now
            return
        if now - last_reset >= WATCHDOG_WINDOW:
            self.watchdog_hour_cost[user_id] = 0.0
            self.watchdog_hour_messages[user_id] = 0
            self.watchdog_hour_turns[user_id] = 0
            self.watchdog_hour_tokens[user_id] = 0
            self.watchdog_hour_reset_time[user_id] = now
            self._watchdog_alerted[user_id] = set()

    def seed_costs(self, costs_by_user: Dict[int, float]) -> None:
        """Prime the in-memory tracker from persisted daily costs (startup).

        Without this every bot restart zeroed the tracker and the budget
        window silently started over.
        """
        # Anchor the 24h reset to the start of the current UTC day so it
        # matches the per-date semantics of the persisted costs. Without an
        # anchor, _maybe_reset_cost_tracker's default (now - 1 day) would wipe
        # the seeded value on the very first check.
        day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        for user_id, cost in costs_by_user.items():
            if cost > 0:
                self.cost_tracker[user_id] = cost
                self.cost_reset_time[user_id] = day_start
        if costs_by_user:
            logger.info("Rate limiter cost tracker seeded", users=len(costs_by_user))

    async def reset_user_limits(self, user_id: int) -> None:
        """Reset all limits for a user (admin function)."""
        async with self.locks[user_id]:
            # Reset cost tracking (both ledgers)
            old_cost = self.cost_tracker[user_id]
            self.cost_tracker[user_id] = 0
            self.token_tracker[user_id] = 0
            self.cost_reset_time[user_id] = datetime.now(UTC)

            # Reset request bucket
            if user_id in self.request_buckets:
                self.request_buckets[user_id].tokens = self.request_buckets[
                    user_id
                ].capacity
                self.request_buckets[user_id].last_update = datetime.now(UTC)

            logger.info("User limits reset", user_id=user_id, old_cost=old_cost)

    def get_user_status(self, user_id: int, role: str = "owner") -> Dict[str, Any]:
        """Get current rate limit status for user (cap picked by role)."""
        # Get request bucket status
        bucket = self._get_or_create_bucket(user_id)
        bucket_status = bucket.get_status()

        # Get cost status
        self._maybe_reset_cost_tracker(user_id)
        cap = self._cost_cap_for(role, user_id)
        current_cost = self.cost_tracker[user_id]
        cost_remaining = max(0, cap - current_cost)
        token_cap = self._token_cap_for(role)
        current_tokens = self.token_tracker[user_id]

        return {
            "request_bucket": bucket_status,
            "cost_usage": {
                "current": current_cost,
                "limit": cap,
                "remaining": cost_remaining,
                "utilization": current_cost / cap,
            },
            # Unmetered-backend tokens in the same daily window (dv-0722).
            "token_usage": {
                "current": current_tokens,
                "limit": token_cap,
                "remaining": max(0, token_cap - current_tokens),
                "utilization": (current_tokens / token_cap) if token_cap else 0.0,
            },
            "last_reset": self.cost_reset_time.get(
                user_id, datetime.now(UTC)
            ).isoformat(),
        }

    def get_global_status(self) -> Dict[str, Any]:
        """Get global rate limiter statistics."""
        return {
            "active_users": len(self.request_buckets),
            "total_cost_tracked": sum(self.cost_tracker.values()),
            "config": {
                "requests_per_window": self.config.rate_limit_requests,
                "window_seconds": self.config.rate_limit_window,
                "burst_capacity": self.config.rate_limit_burst,
                "max_cost_per_user": self.config.claude_max_cost_per_user,
                "refill_rate": self.refill_rate,
            },
        }

    async def cleanup_inactive_users(
        self, inactive_threshold: timedelta = timedelta(hours=24)
    ) -> int:
        """Clean up rate limit data for inactive users."""
        now = datetime.now(UTC)
        inactive_users = []

        # Find users with old buckets
        for user_id, bucket in self.request_buckets.items():
            if now - bucket.last_update > inactive_threshold:
                inactive_users.append(user_id)

        # Clean up data
        for user_id in inactive_users:
            self.request_buckets.pop(user_id, None)
            self.cost_tracker.pop(user_id, None)
            self.cost_reset_time.pop(user_id, None)
            self.locks.pop(user_id, None)

        if inactive_users:
            logger.info(
                "Cleaned up inactive users",
                count=len(inactive_users),
                threshold_hours=inactive_threshold.total_seconds() / 3600,
            )

        return len(inactive_users)
