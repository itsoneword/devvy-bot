from src.profiles.routing import RouteResult, resolve_route

VALID = {"devops", "channel", "general"}


def test_plain_message_uses_active():
    r = resolve_route("hello there", active="devops", valid_slugs=VALID)
    assert r == RouteResult(
        target="devops", prompt="hello there", new_active=None, kind="plain"
    )


def test_sticky_switch_only():
    r = resolve_route("/channel", active="devops", valid_slugs=VALID)
    assert r.kind == "switch"
    assert r.target == "channel"
    assert r.new_active == "channel"
    assert r.prompt == ""


def test_sticky_switch_with_inline_text():
    r = resolve_route("/channel read comments", active="devops", valid_slugs=VALID)
    assert r.kind == "switch"
    assert r.target == "channel"
    assert r.new_active == "channel"
    assert r.prompt == "read comments"


def test_one_shot_prefix_does_not_change_active():
    r = resolve_route("@channel how many comments?", active="devops", valid_slugs=VALID)
    assert r.kind == "one_shot"
    assert r.target == "channel"
    assert r.new_active is None
    assert r.prompt == "how many comments?"


def test_unknown_slug_is_plain_to_active():
    r = resolve_route("@nope hi", active="devops", valid_slugs=VALID)
    assert r.kind == "plain"
    assert r.target == "devops"
    assert r.prompt == "@nope hi"
