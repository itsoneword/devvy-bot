"""Tests for rate limiting system."""

from datetime import UTC, datetime, timedelta

import pytest

from src.config import create_test_config
from src.security.rate_limiter import RateLimitBucket, RateLimiter


class TestRateLimitBucket:
    """Test rate limit bucket functionality."""

    def test_bucket_creation(self):
        """Test bucket creation with initial tokens."""
        bucket = RateLimitBucket(
            capacity=10, tokens=10, last_update=datetime.now(UTC), refill_rate=1.0
        )

        assert bucket.capacity == 10
        assert bucket.tokens == 10
        assert bucket.refill_rate == 1.0

    def test_token_consumption(self):
        """Test consuming tokens from bucket."""
        bucket = RateLimitBucket(
            capacity=10, tokens=5, last_update=datetime.now(UTC), refill_rate=1.0
        )

        # Should be able to consume available tokens
        assert bucket.consume(3) is True
        assert abs(bucket.tokens - 2) < 0.01  # Allow for floating point precision

        # Should fail to consume more than available
        assert bucket.consume(5) is False
        assert abs(bucket.tokens - 2) < 0.01  # Should remain unchanged (with precision)

    def test_token_refill(self):
        """Test token refill over time."""
        past_time = datetime.now(UTC) - timedelta(seconds=5)
        bucket = RateLimitBucket(
            capacity=10,
            tokens=5,
            last_update=past_time,
            refill_rate=1.0,  # 1 token per second
        )

        # Trigger refill by checking status
        status = bucket.get_status()

        # Should have refilled ~5 tokens (5 seconds * 1 token/second)
        assert bucket.tokens == 10  # Capped at capacity
        assert status["tokens"] == 10

    def test_wait_time_calculation(self):
        """Test wait time calculation when tokens not available."""
        bucket = RateLimitBucket(
            capacity=10, tokens=2, last_update=datetime.now(UTC), refill_rate=1.0
        )

        # Should be able to consume available tokens immediately
        wait_time = bucket.get_wait_time(2)
        assert wait_time == 0.0

        # Should need to wait for more tokens
        wait_time = bucket.get_wait_time(5)
        assert abs(wait_time - 3.0) < 0.01  # Need 3 more tokens at 1 token/second

    def test_bucket_status(self):
        """Test bucket status reporting."""
        bucket = RateLimitBucket(
            capacity=10, tokens=7, last_update=datetime.now(UTC), refill_rate=2.0
        )

        status = bucket.get_status()

        assert status["capacity"] == 10
        assert abs(status["tokens"] - 7) < 0.01  # Allow for floating point precision
        assert abs(status["utilization"] - 0.3) < 0.01  # (10-7)/10
        assert status["refill_rate"] == 2.0


class TestRateLimiter:
    """Test rate limiter functionality."""

    @pytest.fixture
    def config(self):
        return create_test_config(
            rate_limit_requests=10,
            rate_limit_window=60,
            rate_limit_burst=20,
            claude_max_cost_per_user=5.0,
        )

    @pytest.fixture
    def rate_limiter(self, config):
        return RateLimiter(config)

    def test_rate_limiter_initialization(self, rate_limiter, config):
        """Test rate limiter initialization."""
        assert rate_limiter.config == config
        assert (
            rate_limiter.refill_rate
            == config.rate_limit_requests / config.rate_limit_window
        )
        assert len(rate_limiter.request_buckets) == 0
        assert len(rate_limiter.cost_tracker) == 0

    async def test_successful_rate_limit_check(self, rate_limiter):
        """Test successful rate limit check."""
        user_id = 123

        # First request should pass
        allowed, message = await rate_limiter.check_rate_limit(
            user_id, cost=0.5, tokens=1
        )
        assert allowed is True
        assert message is None

        # Should have created bucket and tracked cost
        assert user_id in rate_limiter.request_buckets
        assert rate_limiter.cost_tracker[user_id] == 0.5

    async def test_request_rate_limit_exceeded(self, rate_limiter):
        """Test request rate limit exceeded."""
        user_id = 123

        # Consume all tokens
        bucket = rate_limiter._get_or_create_bucket(user_id)
        bucket.tokens = 0

        # Should be rate limited
        allowed, message = await rate_limiter.check_rate_limit(user_id, tokens=1)
        assert allowed is False
        assert "Rate limit exceeded" in message
        assert "wait" in message.lower()

    async def test_cost_limit_exceeded(self, rate_limiter):
        """Test cost limit exceeded (guest role — owners are uncapped)."""
        user_id = 123

        # Set cost near limit and set reset time to prevent auto-reset
        rate_limiter.cost_tracker[user_id] = 4.8
        rate_limiter.cost_reset_time[user_id] = datetime.now(UTC)  # Prevent reset

        # Request that would exceed limit
        allowed, message = await rate_limiter.check_rate_limit(
            user_id, cost=0.5, role="guest"
        )
        assert allowed is False
        assert "Cost limit exceeded" in message
        assert "Remaining budget" in message

    async def test_owner_role_is_uncapped(self, rate_limiter):
        """Owners never get blocked by the cost cap — self-locking the
        owner out is worse than an unbounded spend."""
        user_id = 123

        rate_limiter.cost_tracker[user_id] = 4.8
        rate_limiter.cost_reset_time[user_id] = datetime.now(UTC)

        allowed, message = await rate_limiter.check_rate_limit(
            user_id, cost=0.5, role="owner"
        )
        assert allowed is True
        assert message is None
        # Spend is still tracked for /usage even though it isn't enforced.
        assert rate_limiter.cost_tracker[user_id] == pytest.approx(5.3)

    async def test_cost_tracking(self, rate_limiter):
        """Test cost tracking functionality."""
        user_id = 123

        # Track multiple costs
        await rate_limiter.check_rate_limit(user_id, cost=1.0)
        await rate_limiter.check_rate_limit(user_id, cost=1.5)
        await rate_limiter.check_rate_limit(user_id, cost=0.5)

        assert rate_limiter.cost_tracker[user_id] == 3.0

    async def test_user_limit_reset(self, rate_limiter):
        """Test resetting user limits."""
        user_id = 123

        # Set up some usage
        await rate_limiter.check_rate_limit(user_id, cost=2.0, tokens=5)

        # Verify usage
        assert rate_limiter.cost_tracker[user_id] == 2.0
        bucket = rate_limiter.request_buckets[user_id]
        assert bucket.tokens < bucket.capacity

        # Reset limits
        await rate_limiter.reset_user_limits(user_id)

        # Verify reset
        assert rate_limiter.cost_tracker[user_id] == 0
        assert (
            rate_limiter.request_buckets[user_id].tokens
            == rate_limiter.request_buckets[user_id].capacity
        )

    async def test_cost_tracker_auto_reset(self, rate_limiter):
        """Test automatic cost tracker reset after time period."""
        user_id = 123

        # Set old reset time
        old_time = datetime.now(UTC) - timedelta(days=2)
        rate_limiter.cost_reset_time[user_id] = old_time
        rate_limiter.cost_tracker[user_id] = 3.0

        # Trigger reset check
        rate_limiter._maybe_reset_cost_tracker(user_id)

        # Should have reset
        assert rate_limiter.cost_tracker[user_id] == 0
        assert rate_limiter.cost_reset_time[user_id] > old_time

    async def test_user_status_reporting(self, rate_limiter):
        """Test user status reporting."""
        user_id = 123

        # Set up some usage
        await rate_limiter.check_rate_limit(user_id, cost=2.0, tokens=3)

        status = rate_limiter.get_user_status(user_id)

        assert "request_bucket" in status
        assert "cost_usage" in status
        assert status["cost_usage"]["current"] == 2.0
        assert status["cost_usage"]["remaining"] == 3.0  # 5.0 - 2.0
        assert 0 <= status["cost_usage"]["utilization"] <= 1

    async def test_global_status_reporting(self, rate_limiter):
        """Test global status reporting."""
        # Set up multiple users
        await rate_limiter.check_rate_limit(123, cost=1.0)
        await rate_limiter.check_rate_limit(456, cost=2.0)

        status = rate_limiter.get_global_status()

        assert status["active_users"] == 2
        assert status["total_cost_tracked"] == 3.0
        assert "config" in status
        assert (
            status["config"]["max_cost_per_user"]
            == rate_limiter.config.claude_max_cost_per_user
        )

    async def test_cleanup_inactive_users(self, rate_limiter):
        """Test cleanup of inactive users."""
        user_id = 123

        # Create old bucket
        bucket = rate_limiter._get_or_create_bucket(user_id)
        bucket.last_update = datetime.now(UTC) - timedelta(hours=25)
        rate_limiter.cost_tracker[user_id] = 1.0

        # Cleanup
        cleaned = await rate_limiter.cleanup_inactive_users(timedelta(hours=24))

        assert cleaned == 1
        assert user_id not in rate_limiter.request_buckets
        assert user_id not in rate_limiter.cost_tracker

    async def test_concurrent_access(self, rate_limiter):
        """Test concurrent access to rate limiter."""
        import asyncio

        user_id = 123

        async def make_request():
            return await rate_limiter.check_rate_limit(user_id, cost=0.1, tokens=1)

        # Make multiple concurrent requests
        tasks = [make_request() for _ in range(10)]
        results = await asyncio.gather(*tasks)

        # All should succeed (bucket has enough capacity)
        assert all(result[0] for result in results)

        # Cost should be properly tracked
        expected_cost = 10 * 0.1
        assert abs(rate_limiter.cost_tracker[user_id] - expected_cost) < 0.01

    async def test_bucket_creation_per_user(self, rate_limiter):
        """Test that buckets are created per user."""
        user1, user2 = 123, 456

        # Make requests for different users
        await rate_limiter.check_rate_limit(user1, cost=1.0)
        await rate_limiter.check_rate_limit(user2, cost=2.0)

        # Should have separate buckets and cost tracking
        assert user1 in rate_limiter.request_buckets
        assert user2 in rate_limiter.request_buckets
        assert rate_limiter.cost_tracker[user1] == 1.0
        assert rate_limiter.cost_tracker[user2] == 2.0

    async def test_edge_case_zero_cost(self, rate_limiter):
        """Test handling of zero cost requests."""
        user_id = 123

        allowed, message = await rate_limiter.check_rate_limit(user_id, cost=0.0)
        assert allowed is True
        assert rate_limiter.cost_tracker[user_id] == 0.0

    async def test_edge_case_large_token_request(self, rate_limiter):
        """Test handling of large token requests."""
        user_id = 123

        # Request more tokens than bucket capacity
        bucket = rate_limiter._get_or_create_bucket(user_id)
        large_request = bucket.capacity + 10

        allowed, message = await rate_limiter.check_rate_limit(
            user_id, tokens=large_request
        )
        assert allowed is False
        assert "Rate limit exceeded" in message


class TestRealCostRecording:
    """The budget must be enforced against REAL spend, not the tiny pre-turn
    estimates (which let a $2 turn count as $0.001)."""

    @pytest.fixture
    def config(self):
        return create_test_config(claude_max_cost_per_user=10.0)

    @pytest.fixture
    def rate_limiter(self, config):
        return RateLimiter(config)

    async def test_record_cost_accumulates_real_spend(self, rate_limiter):
        await rate_limiter.record_cost(1, 2.5)
        await rate_limiter.record_cost(1, 3.0)
        assert rate_limiter.cost_tracker[1] == pytest.approx(5.5)

    async def test_recorded_cost_blocks_at_cap(self, rate_limiter):
        """Guests are still blocked at the cap; owners (default role) are not."""
        await rate_limiter.record_cost(1, 10.5)
        allowed, message = await rate_limiter.check_rate_limit(
            1, cost=0.001, role="guest"
        )
        assert not allowed
        assert "Cost limit exceeded" in message

        allowed, message = await rate_limiter.check_rate_limit(1, cost=0.001)
        assert allowed
        assert message is None

    async def test_zero_and_negative_costs_ignored(self, rate_limiter):
        await rate_limiter.record_cost(1, 0.0)
        await rate_limiter.record_cost(1, -1.0)
        assert rate_limiter.cost_tracker[1] == 0.0

    async def test_seed_costs_restores_todays_spend(self, rate_limiter):
        rate_limiter.seed_costs({1: 4.2, 2: 0.5})
        # Must survive the next check: without a reset-time anchor the
        # default (now - 1 day) would immediately zero the seeded value.
        allowed, _ = await rate_limiter.check_rate_limit(1, cost=0.001)
        assert allowed
        assert rate_limiter.cost_tracker[1] == pytest.approx(4.2 + 0.001)

    async def test_seeded_spend_plus_recorded_blocks(self, rate_limiter):
        rate_limiter.seed_costs({1: 8.0})
        await rate_limiter.record_cost(1, 2.0)
        allowed, _ = await rate_limiter.check_rate_limit(1, cost=0.001, role="guest")
        assert not allowed


class TestPerUserCapOverrides:
    """CLAUDE_COST_LIMIT_OVERRIDES beats the role tier — two owner-role users
    can have different budgets."""

    @pytest.fixture
    def config(self):
        return create_test_config(
            claude_max_cost_per_user=10.0,
            claude_max_cost_per_user_guest=2.0,
            claude_cost_limit_overrides="111:100,222:50",
        )

    @pytest.fixture
    def rate_limiter(self, config):
        return RateLimiter(config)

    def test_env_string_parses_to_dict(self, config):
        assert config.claude_cost_limit_overrides == {111: 100.0, 222: 50.0}

    def test_override_beats_role_tier(self, rate_limiter):
        assert rate_limiter._cost_cap_for("owner", 111) == 100.0
        assert rate_limiter._cost_cap_for("owner", 222) == 50.0
        assert rate_limiter._cost_cap_for("guest", 222) == 50.0  # override wins

    def test_no_override_falls_back_to_role(self, rate_limiter):
        assert rate_limiter._cost_cap_for("owner", 999) == 10.0
        assert rate_limiter._cost_cap_for("guest", 999) == 2.0

    async def test_override_enforced_and_shown(self, rate_limiter):
        """An explicit per-user override is enforced against EVERY role,
        owner included.

        The owner exemption exists because role tiers can't tell two owners
        apart; an override is exactly the statement "this specific person has
        this budget", so it has to outrank the exemption. It did not, which
        silently voided the spouse's $50/day cap in production.
        """
        await rate_limiter.record_cost(222, 51.0)
        allowed, _ = await rate_limiter.check_rate_limit(222, cost=0.001, role="guest")
        assert not allowed
        status = rate_limiter.get_user_status(222, role="owner")
        assert status["cost_usage"]["limit"] == 50.0
        # Owner role with an override: blocked, same as any other role.
        allowed, msg = await rate_limiter.check_rate_limit(
            222, cost=0.001, role="owner"
        )
        assert not allowed
        assert "50.00" in msg
        # The other user is unaffected (their own override is $100).
        allowed, _ = await rate_limiter.check_rate_limit(111, cost=0.001, role="guest")
        assert allowed

    async def test_two_ledgers_have_independent_exemptions(self, rate_limiter):
        """The USD gate and the token gate must not share one exemption rule.

        Collapsing them either voids the spouse's USD override (owners skip
        both gates) or blocks an owner on the aggregate token counter. The
        override budgets dollars only: USD capped, tokens uncapped.
        """
        assert rate_limiter.effective_daily_cap(222, "owner") == 50.0
        assert rate_limiter.effective_daily_token_cap(222, "owner") is None
        await rate_limiter.record_cost(222, 51.0)
        allowed, msg = await rate_limiter.check_rate_limit(
            222, cost=0.001, role="owner"
        )
        assert not allowed
        assert "Cost limit exceeded" in msg

    async def test_owner_without_override_stays_uncapped(self, rate_limiter):
        """The exemption survives for owners nobody wrote a number for."""
        await rate_limiter.record_cost(999, 5000.0)
        allowed, _ = await rate_limiter.check_rate_limit(999, cost=1.0, role="owner")
        assert allowed
        assert rate_limiter.effective_daily_cap(999, "owner") is None

    def test_effective_daily_cap_matches_the_gate(self, rate_limiter):
        """/usage must read the cap from the same place the gate does."""
        assert rate_limiter.effective_daily_cap(222, "owner") == 50.0
        assert rate_limiter.effective_daily_cap(111, "owner") == 100.0
        assert rate_limiter.effective_daily_cap(999, "owner") is None
        assert rate_limiter.effective_daily_cap(999, "guest") == 2.0


class TestCommandsBypassBudgetGate:
    """An exhausted budget must never lock the user out of commands —
    /usage and /restart are how you SEE and FIX the situation."""

    @pytest.fixture
    def config(self):
        return create_test_config(claude_max_cost_per_user=10.0)

    @pytest.fixture
    def rate_limiter(self, config):
        return RateLimiter(config)

    async def test_zero_cost_check_passes_over_budget(self, rate_limiter):
        await rate_limiter.record_cost(1, 66.66)
        allowed, _ = await rate_limiter.check_rate_limit(1, cost=0.0)
        assert allowed

    async def test_paid_check_still_blocked_over_budget(self, rate_limiter):
        """Guests remain blocked over budget; owners (default role) do not."""
        await rate_limiter.record_cost(1, 66.66)
        allowed, message = await rate_limiter.check_rate_limit(
            1, cost=0.01, role="guest"
        )
        assert not allowed
        assert "Cost limit exceeded" in message

        allowed, message = await rate_limiter.check_rate_limit(1, cost=0.01)
        assert allowed

    async def test_middleware_lets_command_through_when_over_budget(self):
        from unittest.mock import AsyncMock, MagicMock

        from src.bot.middleware.rate_limit import rate_limit_middleware

        limiter = MagicMock()
        limiter.check_rate_limit = AsyncMock(return_value=(True, None))

        event = MagicMock()
        event.effective_user.id = 1
        event.effective_message.text = "/restart"
        event.callback_query = None

        handler = AsyncMock()
        await rate_limit_middleware(
            handler, event, {"rate_limiter": limiter, "auth_manager": None}
        )
        handler.assert_awaited_once()
        # The command was checked with cost 0 — budget-exempt.
        assert limiter.check_rate_limit.call_args.kwargs["cost"] == 0.0

    async def test_middleware_charges_plain_text(self):
        from unittest.mock import AsyncMock, MagicMock

        from src.bot.middleware.rate_limit import rate_limit_middleware

        limiter = MagicMock()
        limiter.check_rate_limit = AsyncMock(return_value=(True, None))

        event = MagicMock()
        event.effective_user.id = 1
        event.effective_message.text = "do the thing"
        event.effective_message.document = None
        event.effective_message.photo = None
        event.callback_query = None

        handler = AsyncMock()
        await rate_limit_middleware(
            handler, event, {"rate_limiter": limiter, "auth_manager": None}
        )
        assert limiter.check_rate_limit.call_args.kwargs["cost"] > 0.0


class TestSessionWatchdogThresholds:
    """record_watchdog_turn: soft hourly alert signal (dv-8bb5)."""

    @pytest.fixture
    def config(self):
        return create_test_config(
            watchdog_max_cost_per_hour=1.0,
            watchdog_max_messages_per_hour=3,
            watchdog_max_turns_per_hour=5,
        )

    @pytest.fixture
    def rate_limiter(self, config):
        return RateLimiter(config)

    def test_no_alert_under_threshold(self, rate_limiter):
        alerts = rate_limiter.record_watchdog_turn(1, cost=0.1, turns=1)
        assert alerts == []

    def test_cost_threshold_crossed(self, rate_limiter):
        alerts = rate_limiter.record_watchdog_turn(1, cost=1.5, turns=1)
        assert [a.metric for a in alerts] == ["cost_per_hour"]
        assert alerts[0].value == pytest.approx(1.5)
        assert alerts[0].threshold == 1.0

    def test_messages_threshold_crossed(self, rate_limiter):
        # threshold=3, strictly-greater comparison: the 4th message crosses it.
        for _ in range(4):
            alerts = rate_limiter.record_watchdog_turn(1, cost=0.0, turns=0)
        assert [a.metric for a in alerts] == ["messages_per_hour"]

    def test_turns_threshold_crossed(self, rate_limiter):
        alerts = rate_limiter.record_watchdog_turn(1, cost=0.0, turns=6)
        assert [a.metric for a in alerts] == ["turns_per_hour"]

    def test_multiple_metrics_can_fire_together(self, rate_limiter):
        alerts = rate_limiter.record_watchdog_turn(1, cost=2.0, turns=6)
        metrics = {a.metric for a in alerts}
        assert metrics == {"cost_per_hour", "turns_per_hour"}

    def test_alert_fires_once_per_window_per_metric(self, rate_limiter):
        first = rate_limiter.record_watchdog_turn(1, cost=1.5, turns=1)
        assert [a.metric for a in first] == ["cost_per_hour"]

        # Same metric stays over threshold on the next turn but must not
        # alert again within the same hourly window.
        second = rate_limiter.record_watchdog_turn(1, cost=0.1, turns=1)
        assert second == []

    def test_window_reset_clears_alert_state(self, rate_limiter):
        from datetime import UTC, datetime, timedelta

        rate_limiter.record_watchdog_turn(1, cost=1.5, turns=1)
        # Fast-forward the window artificially, like the daily cost tracker's
        # own reset tests do.
        rate_limiter.watchdog_hour_reset_time[1] = datetime.now(UTC) - timedelta(
            hours=2
        )
        alerts = rate_limiter.record_watchdog_turn(1, cost=1.5, turns=1)
        assert [a.metric for a in alerts] == ["cost_per_hour"]

    def test_users_tracked_independently(self, rate_limiter):
        alerts_1 = rate_limiter.record_watchdog_turn(1, cost=1.5, turns=1)
        alerts_2 = rate_limiter.record_watchdog_turn(2, cost=0.1, turns=1)
        assert [a.metric for a in alerts_1] == ["cost_per_hour"]
        assert alerts_2 == []


class TestUnmeteredTokenBudget:
    """dv-0722: turns with no USD price are budgeted in tokens, not as free."""

    @pytest.fixture
    def config(self):
        return create_test_config(
            claude_max_cost_per_user=10.0,
            claude_max_cost_per_user_guest=2.0,
            unmetered_max_tokens_per_user=1_000,
            unmetered_max_tokens_per_user_guest=100,
            watchdog_max_cost_per_hour=1.0,
            watchdog_max_tokens_per_hour=500,
            watchdog_max_messages_per_hour=1000,
            watchdog_max_turns_per_hour=1000,
        )

    @pytest.fixture
    def rate_limiter(self, config):
        return RateLimiter(config)

    async def test_record_tokens_accumulates(self, rate_limiter):
        await rate_limiter.record_tokens(1, 40)
        await rate_limiter.record_tokens(1, 20)
        assert rate_limiter.token_tracker[1] == 60
        # Dollars untouched: the two ledgers are independent.
        assert rate_limiter.cost_tracker[1] == 0.0

    async def test_record_tokens_ignores_non_positive(self, rate_limiter):
        await rate_limiter.record_tokens(1, 0)
        await rate_limiter.record_tokens(1, -5)
        assert rate_limiter.token_tracker[1] == 0

    async def test_guest_blocked_when_token_budget_exhausted(self, rate_limiter):
        await rate_limiter.record_tokens(1, 100)
        allowed, message = await rate_limiter.check_rate_limit(
            1, cost=0.001, role="guest"
        )
        assert allowed is False
        assert "Token budget exceeded" in message
        assert "100/100" in message

    async def test_guest_allowed_under_token_budget(self, rate_limiter):
        await rate_limiter.record_tokens(1, 99)
        allowed, _ = await rate_limiter.check_rate_limit(1, cost=0.001, role="guest")
        assert allowed is True

    async def test_owner_exempt_from_token_budget(self, rate_limiter):
        await rate_limiter.record_tokens(1, 5_000)
        allowed, _ = await rate_limiter.check_rate_limit(1, cost=0.001, role="owner")
        assert allowed is True
        assert rate_limiter.effective_daily_token_cap(1, "owner") is None

    async def test_usd_override_does_not_switch_on_token_cap_for_owner(self):
        # An override budgets USD only. The aggregate local token counter
        # cannot block an owner.
        config = create_test_config(
            claude_cost_limit_overrides={1: 50.0},
            unmetered_max_tokens_per_user=1_000,
        )
        limiter = RateLimiter(config)
        assert limiter.effective_daily_token_cap(1, "owner") is None
        await limiter.record_tokens(1, 1_000)
        allowed, message = await limiter.check_rate_limit(1, cost=0.001, role="owner")
        assert allowed is True
        assert message is None

    async def test_free_updates_skip_the_token_gate(self, rate_limiter):
        # cost=0 = command/button press: must not be locked out by tokens.
        await rate_limiter.record_tokens(1, 100)
        allowed, _ = await rate_limiter.check_rate_limit(1, cost=0.0, role="guest")
        assert allowed is True

    async def test_daily_reset_clears_both_ledgers(self, rate_limiter):
        await rate_limiter.record_cost(1, 1.0)
        await rate_limiter.record_tokens(1, 100)
        rate_limiter.cost_reset_time[1] = datetime.now(UTC) - timedelta(hours=25)
        rate_limiter._maybe_reset_cost_tracker(1)
        assert rate_limiter.cost_tracker[1] == 0
        assert rate_limiter.token_tracker[1] == 0

    async def test_admin_reset_clears_tokens(self, rate_limiter):
        await rate_limiter.record_tokens(1, 100)
        await rate_limiter.reset_user_limits(1)
        assert rate_limiter.token_tracker[1] == 0

    async def test_status_reports_token_usage(self, rate_limiter):
        await rate_limiter.record_tokens(1, 60)
        status = rate_limiter.get_user_status(1, role="guest")
        assert status["token_usage"] == {
            "current": 60,
            "limit": 100,
            "remaining": 40,
            "utilization": pytest.approx(0.6),
        }

    def test_effective_token_cap_by_role(self, rate_limiter):
        assert rate_limiter.effective_daily_token_cap(1, "guest") == 100
        assert rate_limiter.effective_daily_token_cap(1, "owner") is None

    # -- watchdog: tokens_per_hour --------------------------------------

    def test_unpriced_turn_feeds_tokens_not_cost(self, rate_limiter):
        alerts = rate_limiter.record_watchdog_turn(1, cost=None, turns=1, tokens=600)
        assert [a.metric for a in alerts] == ["tokens_per_hour"]
        assert alerts[0].value == 600
        assert alerts[0].threshold == 500
        assert rate_limiter.watchdog_hour_cost[1] == 0.0

    def test_priced_turn_ignores_tokens(self, rate_limiter):
        alerts = rate_limiter.record_watchdog_turn(1, cost=0.5, turns=1, tokens=9_999)
        assert alerts == []
        assert rate_limiter.watchdog_hour_tokens[1] == 0
        assert rate_limiter.watchdog_hour_cost[1] == pytest.approx(0.5)

    def test_token_alert_fires_once_per_window(self, rate_limiter):
        first = rate_limiter.record_watchdog_turn(1, cost=None, turns=1, tokens=600)
        second = rate_limiter.record_watchdog_turn(1, cost=None, turns=1, tokens=600)
        assert [a.metric for a in first] == ["tokens_per_hour"]
        assert second == []

    def test_watchdog_window_reset_clears_tokens(self, rate_limiter):
        rate_limiter.record_watchdog_turn(1, cost=None, turns=1, tokens=400)
        rate_limiter.watchdog_hour_reset_time[1] = datetime.now(UTC) - timedelta(
            hours=2
        )
        rate_limiter.record_watchdog_turn(1, cost=None, turns=1, tokens=1)
        assert rate_limiter.watchdog_hour_tokens[1] == 1
