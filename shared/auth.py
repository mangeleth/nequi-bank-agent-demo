"""Caller identity: verify the customer's JWT and extract who they are (ADR-0009).

This is the ONLY place a `user_id` enters the system. Request bodies have no user_id field
(ADR-0006) and the LLM never sees or supplies one: services verify the token here, then pass
the resulting CallerIdentity to tools out-of-band.

Tokens are signed with an asymmetric key (RS256): the identity provider holds the private key,
services hold only the public key, so a compromised service can verify tokens but never mint them.
"""

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import jwt
from pydantic import BaseModel, ConfigDict, Field

REQUIRED_CLAIMS = ["iss", "aud", "sub", "exp", "iat", "jti"]
_BEARER = re.compile(r"^Bearer ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)$")


class AuthError(Exception):
    """The request is not authenticated. The message is safe to log, not to return to callers."""


class CallerIdentity(BaseModel):
    """Who is making the request, as proven by a verified token. Immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(pattern=r"^user-[0-9]{4,12}$")
    token_id: str  # jti: lets audit logs tie every action to one login
    expires_at: datetime


@dataclass(frozen=True)
class AuthSettings:
    issuer: str
    audience: str
    public_key: str  # PEM. Not a secret: it can only verify, not sign
    algorithm: str = "RS256"
    leeway_seconds: int = 30  # tolerated clock drift between services

    @classmethod
    def from_env(cls) -> "AuthSettings":
        """Read JWT_ISSUER, JWT_AUDIENCE, JWT_PUBLIC_KEY_FILE. Missing values fail at startup."""
        names = ["JWT_ISSUER", "JWT_AUDIENCE", "JWT_PUBLIC_KEY_FILE"]
        missing = [name for name in names if not os.environ.get(name, "").strip()]
        if missing:
            raise ValueError(f"missing environment variables: {', '.join(missing)}")
        return cls(
            issuer=os.environ["JWT_ISSUER"].strip(),
            audience=os.environ["JWT_AUDIENCE"].strip(),
            public_key=Path(os.environ["JWT_PUBLIC_KEY_FILE"].strip()).read_text(),
        )


def bearer_token(authorization_header: str | None) -> str:
    """Extract the token from an `Authorization: Bearer <token>` header."""
    match = _BEARER.match(authorization_header or "")
    if match is None:
        raise AuthError("missing or malformed Authorization header")
    return match.group(1)


def verify_token(token: str, settings: AuthSettings) -> CallerIdentity:
    """Fully verify a JWT and return the caller's identity. Raises AuthError on any failure."""
    try:
        claims = jwt.decode(
            token,
            settings.public_key,
            # Pin ONE algorithm. Trusting the token's own "alg" header enables the classic
            # attacks: alg=none (no signature) and HS256 signed with our public key.
            algorithms=[settings.algorithm],
            issuer=settings.issuer,  # minted by our identity provider, not another one
            audience=settings.audience,  # minted for this system, not another app
            leeway=settings.leeway_seconds,
            options={"require": REQUIRED_CLAIMS},  # also checks signature, exp, iat by default
        )
        return CallerIdentity(
            user_id=claims["sub"],
            token_id=claims["jti"],
            expires_at=datetime.fromtimestamp(claims["exp"], tz=UTC),
        )
    except (jwt.InvalidTokenError, ValueError, TypeError) as exc:
        # One generic error type: callers return 401 without revealing which check failed.
        raise AuthError(f"token rejected: {type(exc).__name__}") from exc
