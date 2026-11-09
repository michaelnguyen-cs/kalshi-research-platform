"""
RSA-PSS request signer for Kalshi API v2.

Auth protocol
─────────────
  message  = f"{timestamp_ms}{METHOD_UPPER}{path}"
  path     includes /trade-api/v2, excludes the query string
  sig      = RSA-PSS(message, SHA-256, MGF1-SHA256, salt=DIGEST_LENGTH=32 bytes)
  headers  = KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE

If a correct key + valid timestamp still returns 401, swap DIGEST_LENGTH for
PSS.MAX_LENGTH — documentation sources differ; the official SDK is the source
of truth and should be preferred over this standalone signer where possible.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

if TYPE_CHECKING:
    from kbot.config import Settings


# ── Key loading ───────────────────────────────────────────────────────────────

def load_private_key(pem_path: str | Path) -> RSAPrivateKey:
    """Load an RSA private key from a PEM file (no passphrase)."""
    with open(pem_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


# ── Standalone signer (used for WS handshake and raw httpx fallback) ─────────

def kalshi_headers(
    method: str,
    path: str,
    key_id: str,
    private_key: RSAPrivateKey,
) -> dict[str, str]:
    """
    Return the three Kalshi auth headers plus Content-Type.

    `path` must include /trade-api/v2 and must NOT include the query string.
    Example: "/trade-api/v2/portfolio/balance"
    """
    ts = str(int(time.time() * 1000))
    msg = f"{ts}{method.upper()}{path}".encode()
    sig = private_key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,  # 32 bytes; fallback: MAX_LENGTH
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "Content-Type": "application/json",
    }


# ── SDK client factory ────────────────────────────────────────────────────────

def build_sdk_client(settings: Settings):
    """
    Return a configured Kalshi SDK ApiClient with RSA-PSS auth wired up.

    Use kalshi_client.py rather than the returned object directly —
    that wrapper is the single swap-point if the SDK changes.
    """
    try:
        import kalshi_python  # type: ignore[import]
    except ImportError as exc:
        raise ImportError(
            "kalshi-python SDK is not installed.\n"
            "  pip install kalshi-python\n"
            "If the package name differs, install from Kalshi's GitHub and update "
            "pyproject.toml accordingly."
        ) from exc

    settings.require_credentials()

    configuration = kalshi_python.Configuration(host=settings.base_url)
    api_client = kalshi_python.ApiClient(configuration)
    # set_kalshi_auth wires up RSA-PSS signing for all subsequent requests.
    api_client.set_kalshi_auth(
        key_id=settings.kalshi_key_id,
        private_key_path=str(settings.private_key_path),
    )
    return api_client
