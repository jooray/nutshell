# ------- API: ONCHAIN (NUT-XX, cashubtc/nuts#365) -------
#
# Schemas mirror PR #365 (XX.md, branch thesimplekid/onchain) as of 2026-05-10.
# The PR is unmerged and still mutating; if/when it lands these may need to
# move into upstream cashu/core/models and adopt any final tweaks.

from typing import List, Optional, Union

from pydantic import BaseModel, Field

from cashu.core.base import BlindedMessage, Proof
from cashu.core.constants import (
    MAX_PAYMENT_REQUEST_LEN,
    MAX_PUBKEY_LEN,
    MAX_QUOTE_ID_LEN,
    MAX_UNIT_LEN,
)
from cashu.core.settings import settings


class PostMintQuoteOnchainRequest(BaseModel):
    """NUT-XX mint quote request. No `amount` (the wallet may deposit any sum
    to the returned address). `pubkey` is REQUIRED per spec."""

    unit: str = Field(..., max_length=MAX_UNIT_LEN)
    pubkey: str = Field(..., max_length=MAX_PUBKEY_LEN)


class PostMintQuoteOnchainResponse(BaseModel):
    """NUT-XX mint quote response. No `state` field — wallet derives status
    from `amount_paid` vs `amount_issued`."""

    quote: str
    request: str  # onchain address
    unit: str
    expiry: Optional[int] = None
    pubkey: str
    amount_paid: int  # total confirmed deposits
    amount_issued: int  # ecash already issued against this quote


class OnchainFeeOption(BaseModel):
    """One row in the NUT-XX `fee_options` array."""

    fee_reserve: int
    estimated_blocks: int


class PostMeltQuoteOnchainRequest(BaseModel):
    """NUT-XX melt quote request."""

    request: str = Field(..., max_length=MAX_PAYMENT_REQUEST_LEN)  # onchain address
    unit: str = Field(..., max_length=MAX_UNIT_LEN)
    amount: int = Field(..., gt=0)


class PostMeltQuoteOnchainResponse(BaseModel):
    """NUT-XX melt quote response. No `fee_reserve` (use `fee_options` instead),
    no `payment_preimage` (use `outpoint` instead), no NUT-08 `change`."""

    quote: str
    amount: int
    unit: str
    state: str  # UNPAID / PENDING / PAID
    expiry: Optional[int] = None
    request: str
    fee_options: List[OnchainFeeOption]
    selected_estimated_blocks: Optional[int] = None
    outpoint: Optional[str] = None  # txid[:vout] proof of payment


class PostMeltOnchainRequest(BaseModel):
    """NUT-XX melt request. The wallet picks one of the `estimated_blocks`
    values returned in the quote's `fee_options`."""

    quote: str = Field(..., max_length=MAX_QUOTE_ID_LEN)
    estimated_blocks: int = Field(..., gt=0)
    inputs: List[Proof] = Field(..., max_length=settings.mint_max_request_length)
    outputs: Union[List[BlindedMessage], None] = Field(
        None, max_length=settings.mint_max_request_length
    )
