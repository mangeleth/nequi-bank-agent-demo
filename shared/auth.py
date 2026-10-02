"""Caller identity: verify the customer's JWT and extract who they are (ADR-0009).

This is the ONLY place a `user_id` enters the system. Request bodies have no user_id field
(ADR-0006) and the LLM never sees or supplies one: services verify the token here, then pass
the resulting CallerIdentity to tools out-of-band.

Tokens are signed with an asymmetric key (RS256): the identity provider holds the private key,
services hold only the public key, so a compromised service can verify tokens but never mint them.
"""

import os
import re
from collections.abc import Sequence
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
    # Set only on tokens our own supervisor issued on the customer's behalf (ADR-0017):
    delegated_by: str | None = None  # who is acting for the customer, e.g. "supervisor"
    transaction_id: str | None = None  # the one transaction such a token may be used for


REVIEWER_ROLE = "dispute-reviewer"


class ReviewerIdentity(BaseModel):
    """A bank employee who reviews disputes the system sent to a person (ADR-0027). Proven by a
    verified token that carries the reviewer role; a customer's token never does."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reviewer_id: str = Field(pattern=r"^ops-[a-z0-9-]{2,40}$")
    token_id: str
    expires_at: datetime


@dataclass(frozen=True)
class AuthSettings:
    issuer: str
    audience: str
    public_key: str  # PEM. Not a secret: it can only verify, not sign
    algorithm: str = "RS256"
    leeway_seconds: int = 30  # tolerated clock drift between services
    # True for our internal issuer: its tokens must name who is acting and for which transaction.
    # False for the customer identity provider: such claims in a customer token are ignored.
    delegation: bool = False

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


def _named_issuer(token: str) -> str | None:
    """The issuer a token CLAIMS, read without verifying anything. Used only to choose which
    trusted key to verify with; the claim is not believed until that verification passes."""
    try:
        return jwt.decode(token, options={"verify_signature": False}).get("iss")
    except jwt.InvalidTokenError:
        return None


def verify_reviewer_token(token: str, settings: AuthSettings) -> ReviewerIdentity:
    """Verify a reviewer's token: the same checks as a customer's (signature with a pinned
    algorithm, issuer, audience, expiry), plus the reviewer role. Raises AuthError otherwise."""
    if _named_issuer(token) != settings.issuer:
        raise AuthError("token rejected: issuer is not trusted")
    try:
        claims = jwt.decode(token, settings.public_key, algorithms=[settings.algorithm], issuer=settings.issuer,
                            audience=settings.audience, leeway=settings.leeway_seconds,
                            options={"require": REQUIRED_CLAIMS})
        roles = claims.get("roles")
        if not isinstance(roles, list) or REVIEWER_ROLE not in roles:
            raise AuthError("token rejected: not a reviewer")
        return ReviewerIdentity(reviewer_id=claims["sub"], token_id=claims["jti"],
                                expires_at=datetime.fromtimestamp(claims["exp"], tz=UTC))
    except AuthError:
        raise
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError) as exc:
        raise AuthError(f"token rejected: {type(exc).__name__}") from exc


def verify_token(token: str, settings: AuthSettings | Sequence[AuthSettings]) -> CallerIdentity:
    """Fully verify a JWT against the trusted issuers and return the caller's identity.

    Raises AuthError on any failure. A service passes one issuer (the supervisor trusts only the
    customer identity provider) or several (an agent also trusts the supervisor's delegation).
    """
    trusted = [settings] if isinstance(settings, AuthSettings) else list(settings)
    issuer = next((s for s in trusted if s.issuer == _named_issuer(token)), None)
    if issuer is None:
        raise AuthError("token rejected: issuer is not trusted")
    try:
        claims = jwt.decode(
            token,
            issuer.public_key,
            # Pin ONE algorithm. Trusting the token's own "alg" header enables the classic
            # attacks: alg=none (no signature) and HS256 signed with our public key.
            algorithms=[issuer.algorithm],
            issuer=issuer.issuer,  # minted by this issuer, not another one
            audience=issuer.audience,  # minted for this system, not another app
            leeway=issuer.leeway_seconds,
            options={"require": REQUIRED_CLAIMS},  # also checks signature, exp, iat by default
        )
        delegation = {}
        if issuer.delegation:
            delegation = {"delegated_by": claims["act"]["sub"], "transaction_id": claims["transaction_id"]}
        return CallerIdentity(
            user_id=claims["sub"],
            token_id=claims["jti"],
            expires_at=datetime.fromtimestamp(claims["exp"], tz=UTC),
            **delegation,
        )
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError) as exc:
        # One generic error type: callers return 401 without revealing which check failed.
        raise AuthError(f"token rejected: {type(exc).__name__}") from exc


def trusted_issuers_from_env() -> list[AuthSettings]:
    """Issuers an agent trusts: the customer identity provider, and the supervisor's delegation
    when INTERNAL_JWT_ISSUER and INTERNAL_JWT_PUBLIC_KEY_FILE are set."""
    trusted = [AuthSettings.from_env()]
    issuer = os.environ.get("INTERNAL_JWT_ISSUER", "").strip()
    key_file = os.environ.get("INTERNAL_JWT_PUBLIC_KEY_FILE", "").strip()
    if issuer and key_file:
        trusted.append(AuthSettings(
            issuer=issuer,
            audience=os.environ.get("INTERNAL_JWT_AUDIENCE", "dispute-agents").strip(),
            public_key=Path(key_file).read_text(),
            delegation=True,
        ))
    return trusted
