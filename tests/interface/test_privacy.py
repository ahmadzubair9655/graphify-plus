from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from graphify_plus.interface.privacy import redact, redact_dict


def test_email():
    assert redact("hi alice@example.com bye") == "hi <EMAIL> bye"


def test_jwt():
    tok = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    assert "<JWT>" in redact(f"Authorization: Bearer {tok}")


def test_apikey_aws():
    assert redact("AKIAIOSFODNN7EXAMPLE in logs") == "<APIKEY> in logs"


def test_apikey_github_pat():
    assert redact("ghp_aBcDeF1234567890abcdef1234567890abcd") == "<APIKEY>"


def test_hex_long():
    h = "abcdef0123456789abcdef0123456789abcdef0123456789"
    assert redact(h) == "<HEX>"


def test_phone_uk_redacted():
    assert "<PHONE>" in redact("call +44 20 7946 0958 today")


def test_short_digits_not_redacted_as_phone():
    assert redact("123") == "123"


def test_idempotent():
    s = "alice@x.com and AKIA1234567890ABCDEF"
    once = redact(s)
    twice = redact(once)
    assert once == twice


def test_redact_dict_recursive():
    d = {"who": "alice@x.com", "nested": {"key": "AKIAIOSFODNN7EXAMPLE"}}
    out = redact_dict(d)
    assert out["who"] == "<EMAIL>"
    assert out["nested"]["key"] == "<APIKEY>"


@given(s=st.text(min_size=0, max_size=100))
def test_property_idempotent_under_random_input(s):
    once = redact(s)
    twice = redact(once)
    assert once == twice
