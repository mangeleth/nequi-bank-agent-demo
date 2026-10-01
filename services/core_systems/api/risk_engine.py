"""Risk Engine API v1: raw fraud signals per transaction (the Fraud Agent interprets them)."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path

from services.core_systems.api.dependencies import CallerCustomerId, Risk
from services.core_systems.models import RiskSignals
from shared.schemas import TransactionId

router = APIRouter(prefix="/v1/risk", tags=["risk-engine"])


@router.get("/transactions/{transaction_id}/signals", response_model=RiskSignals)
async def get_signals(
    transaction_id: Annotated[TransactionId, Path()], customer_id: CallerCustomerId, risk: Risk
) -> RiskSignals:
    signals = await risk.get_signals(customer_id, transaction_id)
    if signals is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    return signals
