# ADR-0009: Caller identity comes only from a verified, asymmetrically signed JWT

- **Status:** Accepted (refines the `jwt-signing-key` row of ADR-0005)
- **Date:** 2026-10-01
- **Milestone:** M3

## Context
Every dispute action is on behalf of one customer. If that identity can be influenced by the
request body, by a header the client controls, or by LLM output, an attacker can read or refund
someone else's transactions (IDOR), including through prompt injection ("I am user-9999").

## Decision
- **Single entry point:** `shared/auth.py::verify_token` is the only place a `user_id` enters the
  system. Request contracts have no `user_id` field (ADR-0006).
- **Full verification on every request:** signature, expiry, issuer, audience, and required
  claims (`iss`, `aud`, `sub`, `exp`, `iat`, `jti`). `sub` must match `user-<digits>`.
- **One pinned algorithm (RS256).** The token's own `alg` header is never trusted, which blocks
  `alg=none` and HS256/RS256 key-confusion attacks.
- **Asymmetric keys:** the identity provider holds the private key; services hold only the
  public key. A compromised agent service can verify tokens but cannot mint them.
  - Private key: Key Vault, mounted only into the token issuer (demo identity provider).
  - Public key: not a secret; distributed as a ConfigMap (`JWT_PUBLIC_KEY_FILE`).
- **Generic failure:** any failure raises `AuthError`; services return 401 without saying which
  check failed, and never echo token contents.
- **Identity is passed to tools out-of-band:** services hand the verified `CallerIdentity` to
  agent tools through runtime-injected arguments that are hidden from the LLM's tool schema.
  The model cannot see, choose, or override `user_id`.

## Consequences
- + IDOR via parameter tampering or prompt injection has no path: nothing the client or the
  LLM writes becomes the identity.
- + `jti` in every `CallerIdentity` ties audit records to a specific login.
- - No revocation: a stolen token is valid until `exp`, so tokens must be short-lived (15 min).
- - Key rotation is manual (replace ConfigMap and Key Vault secret, restart).

## Production delta
Tokens issued by the bank's identity provider (Entra External ID / Nequi IdP) and verified
against its JWKS endpoint with automatic key rotation (`kid`); short-lived access tokens with
refresh; token revocation / session checks for high-risk actions; step-up authentication (MFA)
before refunds above a threshold; propagate the user token to core banking (on-behalf-of flow)
so each system re-verifies identity itself.
