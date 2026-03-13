from freshness import generate_secret, compute_token, render_flag, extract_token


def test_generate_secret_length():
    secret = generate_secret()
    assert len(secret) == 64


def test_generate_secret_is_hex():
    secret = generate_secret()
    int(secret, 16)


def test_generate_secret_unique():
    assert generate_secret() != generate_secret()


def test_compute_token_deterministic():
    secret = "abc123"
    t1 = compute_token(secret, 1, 42)
    t2 = compute_token(secret, 1, 42)
    assert t1 == t2


def test_compute_token_length():
    token = compute_token("secret", 1, 1)
    assert len(token) == 4


def test_compute_token_custom_length():
    token = compute_token("secret", 1, 1, length=8)
    assert len(token) == 8


def test_compute_token_alphabet():
    allowed = set("0123456789abcdefghijklmnopqrstuvwxyz")
    for i in range(50):
        token = compute_token("secret", i, i + 100)
        assert set(token).issubset(allowed)


def test_compute_token_differs_by_user():
    secret = "same_secret"
    t1 = compute_token(secret, 1, 10)
    t2 = compute_token(secret, 1, 20)
    assert t1 != t2


def test_compute_token_differs_by_challenge():
    secret = "same_secret"
    t1 = compute_token(secret, 1, 10)
    t2 = compute_token(secret, 2, 10)
    assert t1 != t2


def test_compute_token_differs_by_secret():
    t1 = compute_token("secret_a", 1, 10)
    t2 = compute_token("secret_b", 1, 10)
    assert t1 != t2


def test_render_flag():
    result = render_flag("ctf{hello_%TOKEN%}", "ab12")
    assert result == "ctf{hello_ab12}"


def test_render_flag_no_placeholder():
    result = render_flag("static_flag", "ab12")
    assert result == "static_flag"


def test_extract_token_basic():
    token = extract_token("ctf{test_%TOKEN%}", "ctf{test_ab12}")
    assert token == "ab12"


def test_extract_token_no_match():
    token = extract_token("ctf{test_%TOKEN%}", "totally_wrong")
    assert token is None


def test_extract_token_no_placeholder():
    token = extract_token("static_flag", "static_flag")
    assert token is None


def test_extract_token_preserves_case():
    token = extract_token("flag{%TOKEN%}", "flag{AbCd}")
    assert token == "AbCd"


def test_extract_token_with_regex_chars():
    token = extract_token("flag{test.%TOKEN%}", "flag{test.xyz1}")
    assert token == "xyz1"
