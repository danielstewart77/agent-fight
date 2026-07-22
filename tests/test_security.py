"""Password hashing."""

from __future__ import annotations

from app.security import MAX_PASSWORD_BYTES, hash_password, verify_password


def test_hash_verify_roundtrip():
    h = hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"  # not plaintext
    assert verify_password("correct horse battery staple", h) is True


def test_wrong_password_fails():
    h = hash_password("s3cret-password")
    assert verify_password("s3cret-passwerd", h) is False


def test_same_password_distinct_hashes():
    # Per-hash salt -> two hashes of the same password differ, both verify.
    a = hash_password("samepass123")
    b = hash_password("samepass123")
    assert a != b
    assert verify_password("samepass123", a)
    assert verify_password("samepass123", b)


def test_over_long_password_rejected_on_hash():
    import pytest
    from app.security import PasswordTooLong

    with pytest.raises(PasswordTooLong):
        hash_password("x" * (MAX_PASSWORD_BYTES + 1))


def test_over_long_password_verifies_false():
    h = hash_password("x" * MAX_PASSWORD_BYTES)
    assert verify_password("x" * (MAX_PASSWORD_BYTES + 1), h) is False


def test_verify_false_on_garbage_hash():
    assert verify_password("whatever", "not-a-bcrypt-hash") is False
