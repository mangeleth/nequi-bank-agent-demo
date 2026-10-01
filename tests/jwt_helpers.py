"""Test-only identity provider: a throwaway RSA key pair and a token signer."""

from datetime import UTC, datetime, timedelta

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from shared.auth import AuthSettings

ISSUER = "https://idp.nequi.demo"
AUDIENCE = "dispute-triage"


def keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return private, public


PRIVATE_KEY, PUBLIC_KEY = keypair()
SETTINGS = AuthSettings(issuer=ISSUER, audience=AUDIENCE, public_key=PUBLIC_KEY)


def claims(**overrides) -> dict:
    now = datetime.now(UTC)
    base = {"iss": ISSUER, "aud": AUDIENCE, "sub": "user-1001", "jti": "login-abc123",
            "iat": now, "exp": now + timedelta(minutes=15)}
    merged = base | overrides
    return {k: v for k, v in merged.items() if v is not None}  # None removes a claim


def sign(payload: dict, key: str = PRIVATE_KEY, algorithm: str = "RS256") -> str:
    return jwt.encode(payload, key, algorithm=algorithm)


def bearer(user_id: str = "user-1001") -> dict:
    return {"Authorization": f"Bearer {sign(claims(sub=user_id))}"}
