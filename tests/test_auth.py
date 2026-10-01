import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from shared.auth import AuthError, AuthSettings, bearer_token, verify_token

ISSUER = "https://idp.nequi.demo"
AUDIENCE = "dispute-triage"


def _keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return private, public


PRIVATE_KEY, PUBLIC_KEY = _keypair()
ATTACKER_PRIVATE_KEY, _ = _keypair()
SETTINGS = AuthSettings(issuer=ISSUER, audience=AUDIENCE, public_key=PUBLIC_KEY)


def claims(**overrides) -> dict:
    now = datetime.now(UTC)
    base = {"iss": ISSUER, "aud": AUDIENCE, "sub": "user-1001", "jti": "login-abc123",
            "iat": now, "exp": now + timedelta(minutes=15)}
    merged = base | overrides
    return {k: v for k, v in merged.items() if v is not None}  # None removes a claim


def sign(payload: dict, key: str = PRIVATE_KEY, algorithm: str = "RS256") -> str:
    return jwt.encode(payload, key, algorithm=algorithm)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def forge(header: dict, payload: dict, signature: bytes = b"") -> str:
    """Hand-build a token, bypassing the library's safety checks (as an attacker would)."""
    body = dict(payload, iat=int(payload["iat"].timestamp()), exp=int(payload["exp"].timestamp()))
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(body).encode())}"
    if header["alg"] == "HS256":
        signature = hmac.new(PUBLIC_KEY.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


# --- Happy path -------------------------------------------------------------------------------


def test_valid_token_yields_identity():
    identity = verify_token(sign(claims()), SETTINGS)
    assert identity.user_id == "user-1001"
    assert identity.token_id == "login-abc123"
    assert identity.expires_at > datetime.now(UTC)


# --- Forgery ----------------------------------------------------------------------------------


def test_token_signed_by_another_key_is_rejected():
    with pytest.raises(AuthError):
        verify_token(sign(claims(sub="user-9999"), key=ATTACKER_PRIVATE_KEY), SETTINGS)


def test_alg_none_is_rejected():
    with pytest.raises(AuthError):
        verify_token(forge({"alg": "none", "typ": "JWT"}, claims(sub="user-9999")), SETTINGS)


def test_hs256_signed_with_our_public_key_is_rejected():
    # Algorithm confusion: the public key is public, so an attacker can use it as an HMAC secret.
    with pytest.raises(AuthError):
        verify_token(forge({"alg": "HS256", "typ": "JWT"}, claims(sub="user-9999")), SETTINGS)


def test_tampered_payload_is_rejected():
    header, _, signature = sign(claims()).split(".")
    other_payload = sign(claims(sub="user-9999"), key=ATTACKER_PRIVATE_KEY).split(".")[1]
    with pytest.raises(AuthError):
        verify_token(f"{header}.{other_payload}.{signature}", SETTINGS)


# --- Claims -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"exp": datetime.now(UTC) - timedelta(minutes=5)},  # expired
        {"iss": "https://evil.example"},  # another identity provider
        {"aud": "some-other-app"},  # token meant for a different system
        {"sub": None},
        {"exp": None},
        {"jti": None},
        {"sub": "admin"},
        {"sub": "user-1001' OR '1'='1"},
        {"sub": 1001},
    ],
)
def test_bad_claims_are_rejected(overrides):
    with pytest.raises(AuthError):
        verify_token(sign(claims(**overrides)), SETTINGS)


def test_error_does_not_reveal_token_contents():
    with pytest.raises(AuthError) as excinfo:
        verify_token(sign(claims(aud="some-other-app")), SETTINGS)
    assert "some-other-app" not in str(excinfo.value)


# --- Authorization header ---------------------------------------------------------------------


def test_bearer_token_is_extracted():
    token = sign(claims())
    assert bearer_token(f"Bearer {token}") == token


@pytest.mark.parametrize("header", [None, "", "Bearer", "Bearer ", "Basic dXNlcjpwYXNz", "bearer a.b.c",
                                    "Bearer a.b", "Bearer a.b.c extra", "Bearer a.b.c\nX-Customer-Id: user-9999"])
def test_malformed_authorization_header_is_rejected(header):
    with pytest.raises(AuthError):
        bearer_token(header)


# --- Settings ---------------------------------------------------------------------------------


def test_settings_from_env(monkeypatch, tmp_path):
    key_file = tmp_path / "jwt-public.pem"
    key_file.write_text(PUBLIC_KEY)
    monkeypatch.setenv("JWT_ISSUER", ISSUER)
    monkeypatch.setenv("JWT_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("JWT_PUBLIC_KEY_FILE", str(key_file))
    assert verify_token(sign(claims()), AuthSettings.from_env()).user_id == "user-1001"


def test_missing_settings_fail_fast(monkeypatch):
    for name in ["JWT_ISSUER", "JWT_AUDIENCE", "JWT_PUBLIC_KEY_FILE"]:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="JWT_ISSUER, JWT_AUDIENCE, JWT_PUBLIC_KEY_FILE"):
        AuthSettings.from_env()
