"""Known incidents (ADR-0022): platform failures that operations has confirmed.

    GET /v1/incidents/covering/{transaction_id}    customer-scoped: is my transaction covered?
    GET /v1/incidents/{incident_id}/transactions   operations: every covered transaction (batch job)

Neither is offered as an MCP tool: the fast path that uses them is code, not a model.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request

from services.core_systems.api.dependencies import CallerCustomerId
from services.core_systems.models import Incident, IncidentId, Transaction
from services.core_systems.ports import IncidentRegistry
from shared.schemas import TransactionId

router = APIRouter(prefix="/v1/incidents", tags=["incidents"])


def _registry(request: Request) -> IncidentRegistry:
    return request.app.state.incidents


Registry = Annotated[IncidentRegistry, Depends(_registry)]


@router.get("/covering/{transaction_id}", response_model=Incident)
async def incident_covering(
    transaction_id: Annotated[TransactionId, Path()], customer_id: CallerCustomerId, registry: Registry
) -> Incident:
    """The confirmed incident covering the customer's transaction. 404 if none covers it, or if the
    transaction is not the customer's: the two are indistinguishable on purpose."""
    incident = await registry.incident_for(customer_id, transaction_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="no confirmed incident covers this transaction")
    return incident


@router.get("/{incident_id}/transactions", response_model=list[Transaction])
async def incident_transactions(incident_id: Annotated[IncidentId, Path()], registry: Registry) -> list[Transaction]:
    """Every transaction the incident covers, refunded or not, for the batch refund job.

    An operations endpoint: it is not scoped to one customer. Inside the cluster it is reachable
    only through the ClusterIP Service; production puts it behind operations-only authentication.
    """
    transactions = await registry.affected_transactions(incident_id)
    if transactions is None:
        raise HTTPException(status_code=404, detail="no such incident")
    return transactions
