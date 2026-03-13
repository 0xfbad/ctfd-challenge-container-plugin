from challenges import ContainerChallenge


def test_normal_value():
    assert ContainerChallenge.sanitize_value("hello") == "hello"


def test_numeric_value():
    assert ContainerChallenge.sanitize_value(42) == 42


def test_none_becomes_none():
    assert ContainerChallenge.sanitize_value(None) is None


def test_empty_string_becomes_none():
    assert ContainerChallenge.sanitize_value("") is None


def test_zero_becomes_none():
    # documented quirk: 0 is falsy, so it coalesces to None
    assert ContainerChallenge.sanitize_value(0) is None


def test_nonempty_string_preserved():
    assert ContainerChallenge.sanitize_value("0") == "0"


def test_false_becomes_none():
    assert ContainerChallenge.sanitize_value(False) is None
