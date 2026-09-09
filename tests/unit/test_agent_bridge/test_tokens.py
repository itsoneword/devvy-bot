"""Per-turn token lifecycle: mint -> use -> revoke, and the TTL backstop."""

from src.agent_bridge.tokens import TokenRegistry
from src.claude.tool_context import TurnContext

CTX_A = TurnContext(profile_slug="coach", user_id=111, memory_agent_id="coach#u111")
CTX_B = TurnContext(profile_slug="coach", user_id=222, memory_agent_id="coach#u222")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TestLifecycle:
    def test_mint_then_resolve_returns_the_same_context(self) -> None:
        registry = TokenRegistry()
        token = registry.mint(CTX_A)
        assert registry.resolve(token) == CTX_A

    def test_revoked_token_resolves_to_nothing(self) -> None:
        registry = TokenRegistry()
        token = registry.mint(CTX_A)
        assert registry.revoke(token) is True
        assert registry.resolve(token) is None
        # idempotent: a second revoke is not an error
        assert registry.revoke(token) is False

    def test_unknown_token_resolves_to_nothing(self) -> None:
        registry = TokenRegistry()
        registry.mint(CTX_A)
        assert registry.resolve("not-a-real-token") is None
        assert registry.resolve("") is None
        assert registry.resolve(None) is None

    def test_expired_token_resolves_to_nothing(self) -> None:
        clock = FakeClock()
        registry = TokenRegistry(ttl_seconds=60, clock=clock)
        token = registry.mint(CTX_A)
        clock.now += 59
        assert registry.resolve(token) == CTX_A
        clock.now += 2
        assert registry.resolve(token) is None
        assert len(registry) == 0  # and it is dropped, not merely refused

    def test_purge_expired_drops_only_the_stale_ones(self) -> None:
        clock = FakeClock()
        registry = TokenRegistry(ttl_seconds=60, clock=clock)
        old = registry.mint(CTX_A)
        clock.now += 50
        fresh = registry.mint(CTX_B)
        clock.now += 20  # old is 70s in, fresh is 20s in
        assert registry.purge_expired() == 1
        assert registry.resolve(old) is None
        assert registry.resolve(fresh) == CTX_B

    def test_tokens_do_not_collide_between_turns(self) -> None:
        registry = TokenRegistry()
        tokens = {registry.mint(CTX_A) for _ in range(50)}
        assert len(tokens) == 50
        assert len(registry) == 50

    def test_two_live_turns_keep_separate_contexts(self) -> None:
        registry = TokenRegistry()
        a, b = registry.mint(CTX_A), registry.mint(CTX_B)
        assert registry.resolve(a) == CTX_A
        assert registry.resolve(b) == CTX_B
        registry.revoke(a)
        assert registry.resolve(b) == CTX_B  # one turn ending is not the other


class TestSecrecy:
    def test_the_raw_token_is_never_stored(self) -> None:
        """A dump of the registry must not yield a working credential."""
        registry = TokenRegistry()
        token = registry.mint(CTX_A)
        blob = repr(registry.__dict__)
        assert token not in blob
        assert token not in repr(registry)

    def test_lease_id_is_not_the_token(self) -> None:
        registry = TokenRegistry()
        token = registry.mint(CTX_A)
        lease_id = TokenRegistry.lease_id(token)
        assert lease_id and lease_id not in token
        assert len(lease_id) == 8

    def test_minted_tokens_are_long_enough_to_be_unguessable(self) -> None:
        registry = TokenRegistry()
        assert len(registry.mint(CTX_A)) >= 32
