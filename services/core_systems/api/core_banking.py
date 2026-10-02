"""Core Banking API v1: transactions and refund history (system of record for money movement)."""

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Path, Query, Response

from services.core_systems.api.dependencies import CallerCustomerId, Ledger
from services.core_systems.models import Refund, RefundHistory, RefundRequest, Transaction
from services.core_systems.ports import IdempotencyConflict, RefundRejected
from shared.schemas import TransactionId

router = APIRouter(prefix="/v1/core-banking", tags=["core-banking"])


@router.get("/transactions/{transaction_id}", response_model=Transaction)
async def get_transaction(
    transaction_id: Annotated[TransactionId, Path()], customer_id: CallerCustomerId, ledger: Ledger
) -> Transaction:
    """Settlement state of one of the caller's transactions.

    404 (not 403) when the transaction belongs to someone else, so callers cannot probe which
    transaction IDs exist (anti-enumeration).
    """
    tx = await ledger.get_transaction(customer_id, transaction_id)
    if tx is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    return tx


@router.get("/refund-history", response_model=RefundHistory)
async def get_refund_history(
    customer_id: CallerCustomerId, ledger: Ledger, window_days: Annotated[int, Query(ge=1, le=365)] = 30
) -> RefundHistory:
    """Automatic refunds already granted to the caller within the window (feeds the refund policy)."""
    return await ledger.get_refund_history(customer_id, window_days)


IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", pattern=r"^[A-Za-z0-9:_-]{16,128}$")]


@router.post("/refunds", response_model=Refund, status_code=201)
async def execute_refund(
    request: RefundRequest, response: Response, customer_id: CallerCustomerId, key: IdempotencyKey, ledger: Ledger
) -> Refund:
    """Pay a refund: the only endpoint in the system that moves money.

    `Idempotency-Key` is required. Sending the same key again returns the refund already made
    (200, `Idempotent-Replay: true`) instead of paying twice. The ledger applies its own rules
    whatever the caller decided. This endpoint is NOT offered as an MCP tool: no model can call it.
    """
    try:
        refund, replayed = await ledger.execute_refund(customer_id, request.transaction_id, request.amount, key)
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RefundRejected as exc:
        status = 404 if exc.code == "transaction_not_found" else 422
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)}) from exc
    if replayed:
        response.status_code = 200
        response.headers["Idempotent-Replay"] = "true"
    return refund
