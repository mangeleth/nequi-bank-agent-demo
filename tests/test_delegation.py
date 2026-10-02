"""Token exchange: the supervisor acts for a customer with its own short-lived token, valid for
one transaction. The agents trust that issuer in addition to the customer identity provider."""

import base64
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi.testclient import TestClient

from services.core_systems.app import _build_adapters
from services.core_systems.app import app as core_app
from services.fraud_agent.main import create_app as create_fraud_app
from services.supervisor.main import create_app as create_supervisor_app
from shared.auth import AuthError, verify_token
from shared.delegation import DelegationSettings, KeyVaultSigner, LocalKeySigner, issue_delegated_token
from shared.tracing import Tracing
from tests.fakes import ScriptedChatModel, ai
from tests.fakes import tool_call as call
from tests.jwt_helpers import (
    DELEGATION,
    INTERNAL_AUDIENCE,
    INTERNAL_ISSUER,
    INTERNAL_PRIVATE_KEY,
    INTERNAL_SETTINGS,
    PRIVATE_KEY,
    SETTINGS,
    SIGNER,
    TRUSTED_BY_AGENTS,
    bearer,
    claims,
    keypair,
    sign,
)

MY_TX = "TX-20261001000001"  # user-1001
MY_OTHER_TX = "TX-20261001000002"  # also user-1001
DISPUTE_ID = "7d0c3c1e-0c58-4c0c-9a1e-2f0f6b1a9a11"


async def delegated(user="user-1001", tx=MY_TX, signer=SIGNER, settings=DELEGATION) -> str:
    return await issue_delegated_token(signer, settings, user_id=user, transaction_id=tx, dispute_id=DISPUTE_ID)


# --- Issuing and verifying ----------------------------------------------------------------------


async def test_delegated_token_names_the_customer_the_actor_and_the_transaction():
    identity = verify_token(await delegated(), TRUSTED_BY_AGENTS)
    assert (identity.user_id, identity.delegated_by, identity.transaction_id) == ("user-1001", "supervisor", MY_TX)


async def test_delegated_token_is_short_lived():
    identity = verify_token(await delegated(), INTERNAL_SETTINGS)
    assert 0 < (identity.expires_at - datetime.now(UTC)).total_seconds() <= 120


async def test_customer_tokens_still_work_and_are_not_delegated():
    identity = verify_token(sign(claims()), TRUSTED_BY_AGENTS)
    assert (identity.user_id, identity.delegated_by, identity.transaction_id) == ("user-1001", None, None)


def test_delegation_claims_in_a_customer_token_are_ignored():
    # A customer cannot grant themselves anything by adding these claims to their own token.
    forged = sign(claims(act={"sub": "supervisor"}, transaction_id="TX-20261001000004"))
    identity = verify_token(forged, TRUSTED_BY_AGENTS)
    assert (identity.delegated_by, identity.transaction_id) == (None, None)


async def test_the_supervisor_itself_does_not_accept_delegated_tokens():
    # The supervisor trusts only the customer identity provider: its own tokens cannot open disputes.
    with pytest.raises(AuthError, match="issuer is not trusted"):
        verify_token(await delegated(), SETTINGS)


async def test_token_signed_with_the_wrong_key_is_rejected():
    impostor = LocalKeySigner(keypair()[0])
    with pytest.raises(AuthError):
        verify_token(await delegated(signer=impostor), TRUSTED_BY_AGENTS)


def test_customer_key_cannot_mint_internal_tokens():
    # Signed by the customer identity provider's key but claiming to be from the supervisor.
    forged = jwt.encode(claims(iss=INTERNAL_ISSUER, aud=INTERNAL_AUDIENCE, act={"sub": "supervisor"},
                               transaction_id=MY_TX), PRIVATE_KEY, algorithm="RS256")
    with pytest.raises(AuthError):
        verify_token(forged, TRUSTED_BY_AGENTS)


@pytest.mark.parametrize("missing", ["act", "transaction_id"])
def test_internal_token_must_say_who_acts_and_for_which_transaction(missing):
    payload = claims(iss=INTERNAL_ISSUER, aud=INTERNAL_AUDIENCE, act={"sub": "supervisor"}, transaction_id=MY_TX)
    del payload[missing]
    with pytest.raises(AuthError):
        verify_token(jwt.encode(payload, INTERNAL_PRIVATE_KEY, algorithm="RS256"), TRUSTED_BY_AGENTS)


def test_unknown_issuer_and_garbage_are_rejected():
    for token in [sign(claims(iss="https://evil.example")), "not.a.token", ""]:
        with pytest.raises(AuthError):
            verify_token(token, TRUSTED_BY_AGENTS)


async def test_key_vault_signer_produces_a_token_the_agents_accept():
    key = serialization.load_pem_private_key(INTERNAL_PRIVATE_KEY.encode(), password=None)
    calls = []

    class FakeKeyVault:  # stands in for Key Vault: receives a digest, returns a signature
        async def sign(self, algorithm, digest):
            calls.append((str(algorithm.value), digest))
            from cryptography.hazmat.primitives.asymmetric.utils import Prehashed
            return SimpleNamespace(signature=key.sign(digest, padding.PKCS1v15(), Prehashed(hashes.SHA256())))

    token = await delegated(signer=KeyVaultSigner(FakeKeyVault()))
    assert verify_token(token, TRUSTED_BY_AGENTS).user_id == "user-1001"
    signing_input = token.rsplit(".", 1)[0].encode()
    assert calls == [("RS256", hashlib.sha256(signing_input).digest())]  # only a digest left the service
    assert base64.urlsafe_b64decode(token.split(".")[0] + "==") == b'{"alg": "RS256", "typ": "JWT"}'


# --- At an agent --------------------------------------------------------------------------------------


@pytest.fixture
def fraud_agent():
    core_app.state.ledger, core_app.state.risk = _build_adapters("in_memory")
    answer = ai(call("FraudAssessment", "c9", transaction_id=MY_TX, risk_score=0.08, risk_level="low", rationale="ok"))
    model = ScriptedChatModel(script=[answer])
    core = httpx.AsyncClient(transport=httpx.ASGITransport(app=core_app), base_url="http://core-systems")
    app = create_fraud_app(auth=TRUSTED_BY_AGENTS, core=core, model=model, tracing=Tracing())
    with TestClient(app) as client:
        yield client, model


def body(tx: str) -> dict:
    return {"transaction_id": tx, "reason": "failed_transfer", "claimed_amount": "50000.00"}


async def test_agent_accepts_a_delegated_token_for_its_transaction(fraud_agent):
    client, _ = fraud_agent
    response = client.post("/v1/fraud/assessments", json=body(MY_TX),
                           headers={"Authorization": f"Bearer {await delegated()}"})
    assert response.status_code == 200


async def test_delegated_token_cannot_be_used_for_another_transaction(fraud_agent):
    # Same customer, their own other transaction: still refused, because the token names one.
    client, model = fraud_agent
    response = client.post("/v1/fraud/assessments", json=body(MY_OTHER_TX),
                           headers={"Authorization": f"Bearer {await delegated(tx=MY_TX)}"})
    assert response.status_code == 403
    assert model.seen == []


def test_agent_still_accepts_a_customer_token(fraud_agent):
    client, _ = fraud_agent
    assert client.post("/v1/fraud/assessments", json=body(MY_TX), headers=bearer("user-1001")).status_code == 200


# --- At the supervisor -------------------------------------------------------------------------------


async def test_a_delegated_token_cannot_open_a_dispute():
    app = create_supervisor_app(auth=SETTINGS, model=ScriptedChatModel(script=[]), specialists=SimpleNamespace(),
                                tracing=Tracing(), signer=SIGNER, delegation=DELEGATION)
    with TestClient(app) as client:
        response = client.post("/v1/disputes", json=body(MY_TX), headers={"Authorization": f"Bearer {await delegated()}"})
    assert response.status_code == 401


def test_settings_default_lifetime_is_two_minutes():
    assert DelegationSettings(issuer="x", audience="y").lifetime_seconds == 120
