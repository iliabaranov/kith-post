from kith.core.recipients import parse_recipients


def test_splits_on_commas_and_newlines():
    valid, invalid = parse_recipients("a@example.com, b@example.com\nc@example.com")
    assert [p.email for p in valid] == ["a@example.com", "b@example.com", "c@example.com"]
    assert invalid == []


def test_named_lowercased_deduped_and_invalid_flagged():
    valid, invalid = parse_recipients("Mara <Mara@Example.com>\nmara@example.com\nnope")
    assert len(valid) == 1
    assert valid[0].name == "Mara"
    assert valid[0].email == "mara@example.com"  # lowercased + deduped
    assert invalid == ["nope"]


def test_empty():
    valid, invalid = parse_recipients("")
    assert valid == [] and invalid == []
