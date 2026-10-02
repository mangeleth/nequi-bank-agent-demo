"""What the supervisor needs from the rest of the system, and the HTTP implementation.

`Specialists` is the port (interface); `HttpSpecialists` calls the deployed services. Tests pass
a fake that satisfies the same port.

The customer's token is forwarded to each agent, and each agent verifies it again itself:
no service trusts another service's word about who the customer is.
"""

from decimal import Decimal
from typing import Protocol

import httpx

from shared.auth import CallerIdentity
from shared.refund_policy import CustomerRefundHistory
from shared.schemas import DisputeRequest, FraudAssessment, LedgerReconciliation


class SpecialistUnavailable(Exception):
    """An agent or Core Systems did not give a usable answer."""


class Specialists(Protocol):
    async def owns_transaction(self, caller: CallerIdentity, transaction_id: str) -> bool: ...

    async def assess_fraud(self, dispute: DisputeRequest, token: str) -> FraudAssessment: ...

    async def reconcile_ledger(self, dispute: DisputeRequest, token: str) -> LedgerReconciliation: ...

    async def refund_history(self, caller: CallerIdentity, window_days: int) -> CustomerRefundHistory: ...

    async def ready(self) -> bool: ...


class HttpSpecialists:
    def __init__(self, http: httpx.AsyncClient, *, fraud_url: str, ledger_url: str, core_url: str) -> None:
        self._http = http
        self._fraud_url = fraud_url.rstrip("/")
        self._ledger_url = ledger_url.rstrip("/")
        self._core_url = core_url.rstrip("/")

    async def _ask_agent(self, url: str, dispute: DisputeRequest, token: str) -> dict:
        try:
            response = await self._http.post(
                url, json=dispute.model_dump(mode="json"), headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            raise SpecialistUnavailable(f"{type(exc).__name__} calling {url}") from exc
        if response.status_code != 200:
            raise SpecialistUnavailable(f"HTTP {response.status_code} from {url}")
        return response.json()

    async def assess_fraud(self, dispute: DisputeRequest, token: str) -> FraudAssessment:
        return FraudAssessment(**await self._ask_agent(f"{self._fraud_url}/v1/fraud/assessments", dispute, token))

    async def reconcile_ledger(self, dispute: DisputeRequest, token: str) -> LedgerReconciliation:
        body = await self._ask_agent(f"{self._ledger_url}/v1/ledger/reconciliations", dispute, token)
        return LedgerReconciliation(**body)

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
        body = response.json()
        return CustomerRefundHistory(
            auto_refund_count=body["auto_refund_count"], auto_refund_total=Decimal(body["auto_refund_total"])
        )

    async def ready(self) -> bool:
        """True when both agents report ready (each of them checks Core Systems)."""
        try:
            for url in (self._fraud_url, self._ledger_url):
                if (await self._http.get(f"{url}/readyz", timeout=3)).status_code != 200:
                    return False
        except httpx.HTTPError:
            return False
        return True
