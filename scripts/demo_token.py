"""Demo identity provider: issue a short-lived login token for a synthetic customer.

Stands in for the bank's real identity provider (ADR-0009). On first use it creates an RSA key
pair under .local/ (git-ignored): the private key signs tokens and stays here; services only
ever receive the public key.

Usage: make demo-token USER_ID=user-1001
"""

import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

KEY_DIR = Path(".local")
PRIVATE_KEY_FILE = KEY_DIR / "jwt-private.pem"
PUBLIC_KEY_FILE = KEY_DIR / "jwt-public.pem"
TOKEN_LIFETIME = timedelta(minutes=15)


def ensure_keys() -> str:
    """Return the private key PEM, creating the key pair on first use."""
    if not PRIVATE_KEY_FILE.exists():
        KEY_DIR.mkdir(exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        PRIVATE_KEY_FILE.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ))
        PRIVATE_KEY_FILE.chmod(0o600)
        PUBLIC_KEY_FILE.write_bytes(key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ))
    return PRIVATE_KEY_FILE.read_text()


def issue(user_id: str) -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": os.environ["JWT_ISSUER"],
        "aud": os.environ["JWT_AUDIENCE"],
        "sub": user_id,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + TOKEN_LIFETIME,
    }
    return jwt.encode(claims, ensure_keys(), algorithm="RS256")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: demo_token.py <user-id>   e.g. user-1001")
    print(issue(sys.argv[1]))
