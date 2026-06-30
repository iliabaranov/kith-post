from kith.core.tracking import RsvpStatus, is_engaged, new_token


def test_tokens_are_unique_and_urlsafe():
    tokens = {new_token() for _ in range(1000)}
    assert len(tokens) == 1000  # no collisions
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert all(set(t) <= allowed for t in tokens)


def test_token_has_enough_entropy():
    # 24 bytes -> ~32 url-safe chars; comfortably >= 128 bits
    assert len(new_token()) >= 24


def test_engaged_statuses():
    assert is_engaged(RsvpStatus.opened)
    assert is_engaged(RsvpStatus.accepted)
    assert is_engaged(RsvpStatus.declined)
    assert not is_engaged(RsvpStatus.sent)
    assert not is_engaged(RsvpStatus.queued)
