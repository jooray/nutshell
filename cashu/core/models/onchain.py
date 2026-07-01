# ------- API: ONCHAIN (NUT-30) -------
#
# Schemas mirror NUT-30 "Payment Method: Onchain" (cashubtc/nuts#365, merged
# 2026-05-20 as 30.md). Cross-checked against the canonical cdk struct
# `MeltQuoteOnchainResponse` (crates/cashu/src/nuts/nut30.rs) and cashu-ts
# (src/model/types/NUT30.ts). When these land in upstream cashu they should
# move into cashu/core/models proper.

from typing import List, Optional, Union

from pydantic import BaseModel, Field

from cashu.core.base import BlindedMessage, BlindedSignature, Proof
from cashu.core.constants import (
    MAX_PAYMENT_REQUEST_LEN,
    MAX_PUBKEY_LEN,
    MAX_QUOTE_ID_LEN,
    MAX_UNIT_LEN,
)
from cashu.core.settings import settings


class PostMintQuoteOnchainRequest(BaseModel):
    """NUT-30 mint quote request. No `amount` (the wallet may deposit any sum
    to the returned address). `pubkey` is REQUIRED per spec."""

    unit: str = Field(..., max_length=MAX_UNIT_LEN)
    pubkey: str = Field(..., max_length=MAX_PUBKEY_LEN)


class PostMintQuoteOnchainResponse(BaseModel):
    """NUT-30 mint quote response. No `state` field — wallet derives status
    from `amount_paid` vs `amount_issued`."""

    quote: str
    request: str  # onchain address
    unit: str
    expiry: Optional[int] = None
    pubkey: str
    amount_paid: int  # total confirmed deposits
    amount_issued: int  # ecash already issued against this quote


class OnchainFeeOption(BaseModel):
    """One row in the NUT-30 `fee_options` array. `fee_index` is the
    server-assigned selector the wallet echoes back in the melt request."""

    fee_index: int
    fee_reserve: int
    estimated_blocks: int


class PostMeltQuoteOnchainRequest(BaseModel):
    """NUT-30 melt quote request."""

    request: str = Field(..., max_length=MAX_PAYMENT_REQUEST_LEN)  # onchain address
    unit: str = Field(..., max_length=MAX_UNIT_LEN)
    amount: int = Field(..., gt=0)


class PostMeltQuoteOnchainResponse(BaseModel):
    """NUT-30 melt quote response. No top-level `fee_reserve` (it lives inside
    `fee_options`), no `payment_preimage` (the txid is surfaced as `outpoint`).
    `change` carries NUT-08 blind signatures for overpaid fee reserve when the
    melt request included `outputs`."""

    quote: str
    amount: int
    unit: str
    state: str  # UNPAID / PENDING / PAID
    expiry: Optional[int] = None
    request: str
    fee_options: List[OnchainFeeOption]
    selected_fee_index: Optional[int] = None
    outpoint: Optional[str] = None  # txid[:vout] proof of payment
    change: Union[List[BlindedSignature], None] = None  # NUT-08 change


class PostMeltOnchainRequest(BaseModel):
    """NUT-30 melt request. The wallet picks one of the quote's `fee_options`
    by echoing its `fee_index`. Optional `outputs` receive NUT-08 change for
    overpaid onchain fee reserve."""

    quote: str = Field(..., max_length=MAX_QUOTE_ID_LEN)
    fee_index: int = Field(..., ge=0)
    inputs: List[Proof] = Field(..., max_length=settings.mint_max_request_length)
    outputs: Union[List[BlindedMessage], None] = Field(
        None, max_length=settings.mint_max_request_length
    )
