# ADR-0017: The supervisor issues its own short-lived token to act for a customer

- **Status:** Accepted
- **Date:** 2026-10-02
- **Milestone:** M6 (Step 10b)

## Context
The agents accept work only with a valid token (ADR-0009), and the supervisor used to forward
the customer's own login token to them. That stops working once disputes are processed from a
queue: a worker may start a run after the customer's 15-minute token has expired, and a login
token must not be stored in a queue or a database, where anyone who can read the store could
replay it as the customer.

## Decision
- **Token exchange.** The supervisor verifies the customer's token once, at submission, and
  records who they are. When a run starts it issues a new token of its own:
  `sub` = the customer, `act.sub` = `supervisor` (who is acting for them, as in RFC 8693),
  `transaction_id` = the one transaction it is valid for, lifetime 2 minutes. The customer's
  login token is never forwarded or stored.
- **A second trusted issuer at the agents.** Each agent trusts the customer identity provider
  and the supervisor's issuer, each with its own public key. The issuer named in a token is
  used only to choose which key to verify with.
- **Claims are believed according to who signed.** `act` and `transaction_id` are read only from
  tokens signed by the supervisor's key. The same claims inside a customer's token are ignored,
  so a customer cannot grant themselves anything.
- **Bound to one transaction.** An agent refuses (403) a supervisor-issued token for any
  transaction other than the one it names, before any model call.
- **The supervisor does not accept its own tokens.** Its endpoints trust only the customer
  identity provider, so a delegated token cannot open or read a dispute.
- **The signing key is a Key Vault key, not a secret.** It is generated inside Key Vault, the key
  itself permits only sign and verify, and it is not exportable. The supervisor sends Key Vault
  a digest and receives a signature, so no pod holds the private key. The public half is
  published to the agents as a ConfigMap.
- **Role:** the supervisor's identity holds the built-in `Key Vault Crypto User` on that one key.
  Azure has no built-in sign-only role, and this one is broader than needed: besides `sign` it
  allows `update` (change the key's settings) and `backup` (download an encrypted backup that
  can be restored into another vault in the same subscription). See Consequences and issue #14.
- **Two signers behind one interface:** `KeyVaultSigner` in the cluster, `LocalKeySigner` with a
  key file for tests and local runs.

## Consequences
- + Work can start at any time after submission without keeping customer credentials.
- + A leaked delegated token is useful for one transaction for at most two minutes.
- + Audit can tell a customer's own request from one made on their behalf.
- + A compromised agent can verify tokens but cannot mint one for another customer.
- - Each run makes one signing call to Key Vault (tens of milliseconds); if Key Vault is
  unreachable the run fails and the dispute goes to a person.
- - A compromised supervisor could request signatures for any customer while the compromise
  lasts; every signature is in Key Vault's audit log. Because of the broad built-in role it
  could also disable or reconfigure the key, or take an encrypted backup of it. It cannot read
  the private key. A custom role with only `keys/read` and `keys/sign/action` removes the
  extra permissions (issue #14).
- - Rotating the key means publishing the new public half before the supervisor uses the new
  version; there is no automatic rotation yet.
- - Core Systems still trusts the `X-Customer-Id` header from the agents (ADR-0008).

## Production delta
A JWKS endpoint with key IDs so agents fetch public keys and rotation needs no redeploy; a
separate audience per agent, so a token for the Fraud Agent is useless at the Ledger Agent;
Key Vault Premium (HSM-backed keys) and alerts on unusual signing volume; the same delegated
token presented to Core Systems instead of a trusted header; sender-constrained tokens (mTLS or
DPoP) so a stolen token cannot be used from another pod.
