"""
Tests for kbot.auth — standalone RSA-PSS signer.

We generate a fresh in-memory RSA key for each test; no .pem file needed.
"""

import base64
import re

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.exceptions import InvalidSignature

from kbot.auth import kalshi_headers, load_private_key


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def rsa_key():
    """2048-bit RSA key generated once per test module."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


# ── Header structure tests ────────────────────────────────────────────────────

def test_all_three_headers_present(rsa_key):
    headers = kalshi_headers("GET", "/trade-api/v2/exchange/status", "key-123", rsa_key)
    assert "KALSHI-ACCESS-KEY" in headers
    assert "KALSHI-ACCESS-TIMESTAMP" in headers
    assert "KALSHI-ACCESS-SIGNATURE" in headers


def test_key_id_passthrough(rsa_key):
    key_id = "d438b709-92d4-48de-b123-97d21b00d584"
    headers = kalshi_headers("GET", "/trade-api/v2/markets", key_id, rsa_key)
    assert headers["KALSHI-ACCESS-KEY"] == key_id


def test_timestamp_is_millisecond_epoch(rsa_key):
    """Timestamp must be an all-digit string in milliseconds (13 digits for 2020+)."""
    headers = kalshi_headers("GET", "/trade-api/v2/markets", "k", rsa_key)
    ts = headers["KALSHI-ACCESS-TIMESTAMP"]
    assert re.fullmatch(r"\d{13,}", ts), f"Expected 13+ digit ms timestamp, got: {ts!r}"


def test_signature_is_valid_base64(rsa_key):
    headers = kalshi_headers("GET", "/trade-api/v2/markets", "k", rsa_key)
    raw = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    # RSA-2048 produces a 256-byte signature
    assert len(raw) == 256


# ── Message format tests ──────────────────────────────────────────────────────

def test_message_format_get(rsa_key):
    """Message = ts + 'GET' + path; signature must verify against that exact bytes."""
    path = "/trade-api/v2/markets"
    headers = kalshi_headers("GET", path, "k", rsa_key)
    _assert_sig_matches_message(rsa_key, headers, "GET", path)


def test_message_format_post(rsa_key):
    """POST path with portfolio prefix."""
    path = "/trade-api/v2/portfolio/orders"
    headers = kalshi_headers("POST", path, "k", rsa_key)
    _assert_sig_matches_message(rsa_key, headers, "POST", path)


def test_method_is_uppercased(rsa_key):
    """Lowercase method input must produce the same result as uppercase."""
    path = "/trade-api/v2/exchange/status"
    h_lower = kalshi_headers("get", path, "k", rsa_key)
    # Independently verify: signature must match uppercase-method message
    _assert_sig_matches_message(rsa_key, h_lower, "GET", path)


def test_path_must_not_include_query_string(rsa_key):
    """
    Passing a path WITH a query string will sign the wrong message (Kalshi strips
    it server-side). This test documents that the caller is responsible for stripping
    the query string before calling kalshi_headers().
    """
    clean_path = "/trade-api/v2/markets"
    dirty_path = "/trade-api/v2/markets?status=open&limit=10"

    headers_clean = kalshi_headers("GET", clean_path, "k", rsa_key)
    headers_dirty = kalshi_headers("GET", dirty_path, "k", rsa_key)

    # Verify clean signature against the clean path message (must pass)
    _assert_sig_matches_message(rsa_key, headers_clean, "GET", clean_path)

    # Verify dirty signature against the CLEAN path message (must fail — different msg)
    with pytest.raises(InvalidSignature):
        _verify_sig(rsa_key, headers_dirty, "GET", clean_path)


# ── load_private_key ──────────────────────────────────────────────────────────

def test_load_private_key_round_trip(tmp_path, rsa_key):
    """Write key to a temp PEM file and verify load_private_key returns the same key."""
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, NoEncryption,
    )
    pem_bytes = rsa_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    pem_file = tmp_path / "test_key.pem"
    pem_file.write_bytes(pem_bytes)

    loaded = load_private_key(pem_file)

    # Sign with original, verify with loaded public key
    path = "/trade-api/v2/exchange/status"
    headers = kalshi_headers("GET", path, "k", rsa_key)
    _assert_sig_matches_message(loaded, headers, "GET", path)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _verify_sig(key, headers: dict, method: str, path: str) -> None:
    ts = headers["KALSHI-ACCESS-TIMESTAMP"]
    sig = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    msg = f"{ts}{method.upper()}{path}".encode()
    key.public_key().verify(
        sig,
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )


def _assert_sig_matches_message(key, headers: dict, method: str, path: str) -> None:
    """Assert the signature in headers verifies against ts+METHOD+path."""
    try:
        _verify_sig(key, headers, method, path)
    except InvalidSignature as exc:
        pytest.fail(f"Signature did not verify for message ts+{method}+{path}: {exc}")
