"""Core Banking API v1: transactions and refund history (system of record for money movement)."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query

from services.core_systems.api.dependencies import CallerCustomerId, Ledger
from services.core_systems.models import RefundHistory, Transaction
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
