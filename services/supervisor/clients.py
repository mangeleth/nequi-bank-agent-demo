"""What the supervisor needs from the rest of the system, and the HTTP implementation.

`Specialists` is the port (interface); `HttpSpecialists` calls the deployed services. Tests pass
a fake that satisfies the same port.

The customer's token is forwarded to each agent, and each agent verifies it again itself:
no service trusts another service's word about who the customer is.
"""

from decimal import Decimal
from typing import Protocol

import httpx
from pydantic import BaseModel, ValidationError

from shared.auth import CallerIdentity
from shared.refund_policy import CustomerRefundHistory
from shared.schemas import (
    DisputeRequest,
    FraudAssessment,
    KnownIncident,
    LedgerReconciliation,
    RefundPayment,
    RiskLevel,
)
from shared.tracing import TRACEPARENT_HEADER


class SpecialistUnavailable(Exception):
    """An agent or Core Systems did not give a usable answer."""


class RefundRefused(Exception):
    """The ledger gave a definite no (e.g. amount_mismatch). Retrying would get the same answer,
    so this goes to a person, never back to the queue."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _parse(contract: type[BaseModel], response: httpx.Response):
    """Validate another service's answer against our contract. A 200 with a body we cannot
    use is treated like any other failure: the supervisor may retry, then escalates."""
    try:
        return contract.model_validate_json(response.content)
    except ValidationError as exc:
        raise SpecialistUnavailable(f"malformed {contract.__name__} from {response.request.url.host}") from exc


def _paid(response: httpx.Response, transaction_id: str, amount: Decimal, idempotency_key: str) -> RefundPayment:
    """Read the ledger's confirmation. Its reply describes the whole refund (customer, transaction,
    ...); we keep what we record, and check it is the refund we asked for. A confirmation for a
    different transaction, amount, or key is not trusted as "paid"."""
    try:
        body = response.json()
        payment = RefundPayment.model_validate({field: body[field] for field in RefundPayment.model_fields})
        confirmed = (body["transaction_id"], Decimal(str(body["amount"])), body["idempotency_key"])
    except (ValueError, KeyError, TypeError, ArithmeticError, ValidationError) as exc:
        raise SpecialistUnavailable("malformed refund confirmation from the ledger") from exc
    if confirmed != (transaction_id, amount, idempotency_key):
        raise SpecialistUnavailable(f"the ledger confirmed a different refund: {confirmed}")
    return payment


class Specialists(Protocol):
    async def owns_transaction(self, caller: CallerIdentity, transaction_id: str) -> bool: ...

    async def assess_fraud(
        self, dispute: DisputeRequest, token: str, traceparent: str | None = None
    ) -> FraudAssessment: ...

    async def reconcile_ledger(
        self, dispute: DisputeRequest, token: str, traceparent: str | None = None
    ) -> LedgerReconciliation: ...

    async def refund_history(self, caller: CallerIdentity, window_days: int) -> CustomerRefundHistory: ...

    async def known_incident(self, caller: CallerIdentity, transaction_id: str) -> KnownIncident | None:
        """The confirmed incident covering this transaction, or None (ADR-0022)."""

    async def ledger_record(self, caller: CallerIdentity, transaction_id: str) -> LedgerReconciliation:
        """The ledger's figures, read directly by code: no agent, no model."""

    async def risk_engine(self, caller: CallerIdentity, transaction_id: str) -> FraudAssessment:
        """The risk engine's own score, read directly by code: no agent, no model."""

    async def pay_refund(
        self, user_id: str, transaction_id: str, amount: Decimal, idempotency_key: str
    ) -> RefundPayment:
        """Ask the ledger to pay. RefundRefused on a definite no; SpecialistUnavailable when the
        answer is unknown (safe to retry: the same key never pays twice)."""

    async def ready(self) -> bool: ...


class HttpSpecialists:
    def __init__(self, http: httpx.AsyncClient, *, core_url: str, fraud_url: str = "", ledger_url: str = "") -> None:
        """The refund payer needs only Core Systems, so the agents' URLs are optional."""
        self._http = http
        self._fraud_url = fraud_url.rstrip("/")
        self._ledger_url = ledger_url.rstrip("/")
        self._core_url = core_url.rstrip("/")

    async def _ask_agent(
        self, url: str, dispute: DisputeRequest, token: str, traceparent: str | None
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"}
        if traceparent:
            headers[TRACEPARENT_HEADER] = traceparent  # the agent's steps join our trace
        try:
            response = await self._http.post(url, json=dispute.model_dump(mode="json"), headers=headers)
        except httpx.HTTPError as exc:
            raise SpecialistUnavailable(f"{type(exc).__name__} calling {url}") from exc
        if response.status_code != 200:
            raise SpecialistUnavailable(f"HTTP {response.status_code} from {url}")
        return response

    async def assess_fraud(
        self, dispute: DisputeRequest, token: str, traceparent: str | None = None
    ) -> FraudAssessment:
        url = f"{self._fraud_url}/v1/fraud/assessments"
        return _parse(FraudAssessment, await self._ask_agent(url, dispute, token, traceparent))

    async def reconcile_ledger(
        self, dispute: DisputeRequest, token: str, traceparent: str | None = None
    ) -> LedgerReconciliation:
        url = f"{self._ledger_url}/v1/ledger/reconciliations"
        return _parse(LedgerReconciliation, await self._ask_agent(url, dispute, token, traceparent))

    async def _core_get(self, caller: CallerIdentity, path: str, **params) -> httpx.Response:
        try:
            return await self._http.get(
                f"{self._core_url}{path}", params=params, headers={"X-Customer-Id": caller.user_id}
            )
        except httpx.HTTPError as exc:
            raise SpecialistUnavailable(f"{type(exc).__name__} calling core systems") from exc

    async def owns_transaction(self, caller: CallerIdentity, transaction_id: str) -> bool:
        response = await self._core_get(caller, f"/v1/core-banking/transactions/{transaction_id}")
        if response.status_code == 404:
            return False
        if response.status_code != 200:
            raise SpecialistUnavailable(f"HTTP {response.status_code} from core systems")
        return True

    async def refund_history(self, caller: CallerIdentity, window_days: int) -> CustomerRefundHistory:
        response = await self._core_get(caller, "/v1/core-banking/refund-history", window_days=window_days)
        if response.status_code != 200:
            raise SpecialistUnavailable(f"HTTP {response.status_code} from core systems")
        try:
            body = response.json()
            return CustomerRefundHistory(
                auto_refund_count=int(body["auto_refund_count"]), auto_refund_total=Decimal(body["auto_refund_total"])
            )
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            raise SpecialistUnavailable("malformed refund history from core systems") from exc

    async def _core_json(self, caller: CallerIdentity, path: str) -> dict | None:
        """GET a Core Systems resource: the body, None on 404, SpecialistUnavailable otherwise."""
        response = await self._core_get(caller, path)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SpecialistUnavailable(f"HTTP {response.status_code} from core systems")
        try:
            return response.json()
        except ValueError as exc:
            raise SpecialistUnavailable("malformed answer from core systems") from exc

    async def known_incident(self, caller: CallerIdentity, transaction_id: str) -> KnownIncident | None:
        body = await self._core_json(caller, f"/v1/incidents/covering/{transaction_id}")
        if body is None:
            return None
        try:
            return KnownIncident(incident_id=body["incident_id"], title=body["title"],
                                 confirmed_by=body["confirmed_by"])
        except (KeyError, TypeError, ValidationError) as exc:
            raise SpecialistUnavailable("malformed incident from core systems") from exc

    async def ledger_record(self, caller: CallerIdentity, transaction_id: str) -> LedgerReconciliation:
        tx = await self._core_json(caller, f"/v1/core-banking/transactions/{transaction_id}")
        if tx is None:
            raise SpecialistUnavailable("transaction not found in the ledger")
        try:
            return LedgerReconciliation(
                transaction_id=tx["transaction_id"], settlement_status=tx["settlement_status"],
                debited_amount=tx["debited_amount"], credited_amount=tx["credited_amount"], currency=tx["currency"],
                summary=(f"Read directly from Core Banking: {tx['settlement_status']}, "
                         f"{tx['debited_amount']} debited, {tx['credited_amount']} credited, "
                         f"failure code {tx.get('failure_code') or 'none'}."))
        except (KeyError, TypeError, ValidationError) as exc:
            raise SpecialistUnavailable("malformed transaction from core systems") from exc

    async def risk_engine(self, caller: CallerIdentity, transaction_id: str) -> FraudAssessment:
        signals = await self._core_json(caller, f"/v1/risk/transactions/{transaction_id}/signals")
        if signals is None:
            raise SpecialistUnavailable("no risk signals for the transaction")
        try:
            score = float(signals["engine_score"])
            level = RiskLevel.LOW if score < 0.4 else RiskLevel.MEDIUM if score < 0.7 else RiskLevel.HIGH
            return FraudAssessment(transaction_id=transaction_id, risk_score=score, risk_level=level,
                                   signals=[f"risk engine score {score}"],
                                   rationale="The risk engine's own score, read by code; no model interpreted it.")
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise SpecialistUnavailable("malformed risk signals from core systems") from exc

    async def pay_refund(
        self, user_id: str, transaction_id: str, amount: Decimal, idempotency_key: str
    ) -> RefundPayment:
        try:
            response = await self._http.post(
                f"{self._core_url}/v1/core-banking/refunds",
                json={"transaction_id": transaction_id, "amount": str(amount)},
                headers={"X-Customer-Id": user_id, "Idempotency-Key": idempotency_key},
            )
        except httpx.HTTPError as exc:
            # We do not know whether it was paid. Retrying with the same key is safe.
            raise SpecialistUnavailable(f"{type(exc).__name__} calling the ledger") from exc
        if response.status_code in (200, 201):  # 200 = a replay of a refund already paid
            return _paid(response, transaction_id, amount, idempotency_key)
        if response.status_code in (404, 409, 422):
            detail = response.json().get("detail") if response.headers.get("content-type", "").startswith("application/json") else None
            if isinstance(detail, dict):
                raise RefundRefused(str(detail.get("code", "refused")), str(detail.get("message", "")))
            raise RefundRefused(f"http_{response.status_code}", str(detail or response.text)[:200])
        raise SpecialistUnavailable(f"HTTP {response.status_code} from the ledger")

    async def ready(self) -> bool:
        """True when both agents report ready (each of them checks Core Systems)."""
        try:
            for url in (self._fraud_url, self._ledger_url):
                if (await self._http.get(f"{url}/readyz", timeout=3)).status_code != 200:
                    return False
        except httpx.HTTPError:
            return False
        return True
