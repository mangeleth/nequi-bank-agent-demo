"""Tokens the supervisor issues to act on a customer's behalf (token exchange, ADR-0017).

Why: an agent accepts work only with a valid token. A worker may run a dispute long after the
customer submitted it, when their 15-minute login token has expired, and a login token must not
be kept in a queue or a database. So the supervisor, which verified the customer at submission,
issues its own token when the work starts:

    "the supervisor is acting for user-1001, for transaction TX-..., for the next 2 minutes"

The token is bound to one transaction, so even if it leaked it could do nothing else.

The private key never leaves Key Vault in the cluster: `KeyVaultSigner` asks Key Vault to sign.
`LocalKeySigner` signs with a key file, for tests and local runs. Both produce the same token.
"""

import base64
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

ACTING_PARTY = "supervisor"


@dataclass(frozen=True)
class DelegationSettings:
    issuer: str
    audience: str
    lifetime_seconds: int = 120  # long enough for one triage, short enough to be useless later


class TokenSigner(Protocol):
    async def sign(self, signing_input: bytes) -> bytes:
        """RS256 signature (RSA PKCS#1 v1.5 over SHA-256) of `signing_input`."""


class LocalKeySigner:
    """Signs with a private key held in this process. Tests and local runs only."""

    def __init__(self, private_key_pem: str) -> None:
        from cryptography.hazmat.primitives import serialization

        self._key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)

    async def sign(self, signing_input: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        return self._key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())


class KeyVaultSigner:
    """Asks Key Vault to sign. The key is created there and cannot be read out, so no pod ever
    holds it; this service's identity is only allowed to request signatures."""

    def __init__(self, crypto_client) -> None:
        self._client = crypto_client  # azure.keyvault.keys.crypto.aio.CryptographyClient

    async def sign(self, signing_input: bytes) -> bytes:
        from azure.keyvault.keys.crypto import SignatureAlgorithm

        result = await self._client.sign(SignatureAlgorithm.rs256, hashlib.sha256(signing_input).digest())
        return result.signature


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


async def issue_delegated_token(
    signer: TokenSigner, settings: DelegationSettings, *, user_id: str, transaction_id: str, dispute_id: str
) -> str:
    """A short-lived token: the supervisor acting for one customer, for one transaction."""
    now = datetime.now(UTC)
    claims = {
        "iss": settings.issuer,
        "aud": settings.audience,
        "sub": user_id,  # the customer, taken from the token verified at submission
        "act": {"sub": ACTING_PARTY},  # who is acting for them (RFC 8693)
        "transaction_id": transaction_id,  # the only transaction this token is valid for
        "dispute_id": dispute_id,  # for audit
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.lifetime_seconds)).timestamp()),
    }
    header = {"alg": "RS256", "typ": "JWT"}
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    signature = await signer.sign(signing_input.encode())
    return f"{signing_input}.{_b64(signature)}"


def build_signer() -> tuple[TokenSigner, DelegationSettings]:
    """The signer from configuration: INTERNAL_JWT_KEY_ID (a Key Vault key) in the cluster, or
    INTERNAL_JWT_PRIVATE_KEY_FILE for local runs. Missing configuration fails at startup."""
    settings = DelegationSettings(
        issuer=os.environ["INTERNAL_JWT_ISSUER"].strip(),
        audience=os.environ.get("INTERNAL_JWT_AUDIENCE", "dispute-agents").strip(),
    )
    if key_id := os.environ.get("INTERNAL_JWT_KEY_ID", "").strip():
        from azure.identity.aio import DefaultAzureCredential
        from azure.keyvault.keys.crypto.aio import CryptographyClient

        return KeyVaultSigner(CryptographyClient(key_id, DefaultAzureCredential())), settings
    if key_file := os.environ.get("INTERNAL_JWT_PRIVATE_KEY_FILE", "").strip():
        return LocalKeySigner(Path(key_file).read_text()), settings
    raise ValueError("set INTERNAL_JWT_KEY_ID (Key Vault) or INTERNAL_JWT_PRIVATE_KEY_FILE (local)")
