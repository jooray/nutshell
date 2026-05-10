"""NUT-XX (cashubtc/nuts#365) onchain payment-method router.

Endpoints follow the unmerged onchain NUT spec verbatim, mounted under
`/v1/*/onchain`. The (`onchain`, `zec`) method-unit pair signals native
Zcash. (For native BTC the same endpoints would be exposed against
(`onchain`, `sat`).)

Behavioural deviations from NUT-XX still in place (tracked in
`ZCASH-CDK-COMPATIBILITY.md` §6):

* one deposit per quote (no multi-deposit address reuse)
* one mint per quote (the existing cashu ledger marks the quote ISSUED
  on the first successful mint and forbids further mints)

Both can be lifted without breaking the wire format once we are willing
to fork the cashu ledger's mint-quote state machine.
"""

import time
from typing import List

from fastapi import APIRouter, HTTPException, Request
from loguru import logger

from ..core.base import (
    Amount,
    Method,
    MintQuote,
    MintQuoteState,
    Unit,
)
from ..core.crypto.keys import random_hash
from ..core.errors import CashuError
from ..core.models import (
    OnchainFeeOption,
    PostMeltOnchainRequest,
    PostMeltQuoteOnchainRequest,
    PostMeltQuoteOnchainResponse,
    PostMeltQuoteRequest,
    PostMintQuoteOnchainRequest,
    PostMintQuoteOnchainResponse,
    PostMintRequest,
    PostMintResponse,
)
from ..core.settings import settings
from ..lightning.base import InvoiceResponse
from ..mint.startup import ledger
from .limit import limiter

router = APIRouter()

ONCHAIN_METHOD = "onchain"

# Zcash has a near-flat fee market (ZIP-317 conventional fee ~10000 zat) and
# a 75-second block target. We only ever return a single fee option; the field
# exists in NUT-XX so wallets can offer per-priority quotes on chains where
# that matters (BTC). For Zcash, `estimated_blocks` is just the configured
# minimum confirmation depth.
def _zec_estimated_blocks() -> int:
    return settings.mint_zcash_min_confirmations


def _build_mint_quote_response(
    quote: MintQuote, amount_paid: int, amount_issued: int
) -> PostMintQuoteOnchainResponse:
    return PostMintQuoteOnchainResponse(
        quote=quote.quote,
        request=quote.request,
        unit=quote.unit,
        expiry=quote.expiry,
        pubkey=quote.pubkey or "",
        amount_paid=amount_paid,
        amount_issued=amount_issued,
    )


# --------------- MINT (deposit ZEC → receive ecash) ---------------


@router.post(
    "/v1/mint/quote/onchain",
    name="Request onchain mint quote",
    summary="Request a quote for minting tokens via an onchain deposit",
    response_model=PostMintQuoteOnchainResponse,
    response_description="An onchain address to send funds to for minting tokens",
)
@limiter.limit(f"{settings.mint_transaction_rate_limit_per_minute}/minute")
async def onchain_mint_quote(
    request: Request, payload: PostMintQuoteOnchainRequest
) -> PostMintQuoteOnchainResponse:
    """NUT-XX mint quote: allocate a deposit address. No `amount` in the
    request — the wallet decides how much to send, and `amount_paid` /
    `amount_issued` track the running totals."""
    logger.trace(f"> POST /v1/mint/quote/onchain: payload={payload}")

    unit, method = ledger._verify_and_get_unit_method(payload.unit, ONCHAIN_METHOD)
    backend = ledger.backends[method][unit]

    # The backend ignores the `amount` argument for onchain (a fresh address
    # accepts any sum); we pass 0 as a sentinel meaning "any deposit".
    invoice_response: InvoiceResponse = await backend.create_invoice(
        amount=Amount(unit=unit, amount=0),
    )
    if not (invoice_response.payment_request and invoice_response.checking_id):
        raise HTTPException(status_code=502, detail="backend did not return an address")

    expiry = int(time.time()) + settings.mint_zcash_quote_expiry

    quote = MintQuote(
        quote=random_hash(),
        method=method.name,  # "onchain"
        request=invoice_response.payment_request,
        checking_id=invoice_response.checking_id,
        unit=payload.unit,
        amount=0,  # updated to total_confirmed once a deposit lands
        state=MintQuoteState.unpaid,
        created_time=int(time.time()),
        expiry=expiry,
        pubkey=payload.pubkey,
    )
    await ledger.crud.store_mint_quote(quote=quote, db=ledger.db)
    await ledger.events.submit(quote)

    resp = _build_mint_quote_response(quote, amount_paid=0, amount_issued=0)
    logger.trace(f"< POST /v1/mint/quote/onchain: {resp}")
    return resp


@router.get(
    "/v1/mint/quote/onchain/{quote}",
    summary="Get onchain mint quote",
    response_model=PostMintQuoteOnchainResponse,
    response_description="Current accounting (amount_paid / amount_issued) for the quote",
)
@limiter.limit(f"{settings.mint_transaction_rate_limit_per_minute}/minute")
async def onchain_get_mint_quote(
    request: Request, quote: str
) -> PostMintQuoteOnchainResponse:
    logger.trace(f"> GET /v1/mint/quote/onchain/{quote}")

    mint_quote = await ledger.crud.get_mint_quote(quote_id=quote, db=ledger.db)
    if not mint_quote:
        raise HTTPException(status_code=404, detail="quote not found")
    if mint_quote.method != ONCHAIN_METHOD:
        raise HTTPException(status_code=400, detail="not an onchain quote")

    unit, method = ledger._verify_and_get_unit_method(mint_quote.unit, ONCHAIN_METHOD)
    backend = ledger.backends[method][unit]

    # Refresh deposit total from the backend on every poll. If new funds have
    # arrived, persist the running amount_paid and flip UNPAID → PAID.
    total_paid = await backend.get_deposit_total(mint_quote.checking_id)
    if total_paid > mint_quote.amount and not mint_quote.issued:
        async with ledger.db.get_connection(
            lock_table="mint_quotes",
            lock_select_statement="quote = :quote",
            lock_parameters={"quote": quote},
        ) as conn:
            mint_quote = await ledger.crud.get_mint_quote(
                quote_id=quote, db=ledger.db, conn=conn
            )
            if mint_quote and not mint_quote.issued and total_paid > mint_quote.amount:
                mint_quote.amount = total_paid
                if mint_quote.state == MintQuoteState.unpaid:
                    mint_quote.state = MintQuoteState.paid
                    mint_quote.paid_time = int(time.time())
                await ledger.crud.update_mint_quote(
                    quote=mint_quote, db=ledger.db, conn=conn
                )
                await ledger.events.submit(mint_quote)

    if not mint_quote:
        raise HTTPException(status_code=404, detail="quote not found")

    if mint_quote.state == MintQuoteState.issued:
        amount_paid = mint_quote.amount
        amount_issued = mint_quote.amount
    elif mint_quote.state == MintQuoteState.paid or mint_quote.state == MintQuoteState.pending:
        amount_paid = mint_quote.amount
        amount_issued = 0
    else:
        amount_paid = 0
        amount_issued = 0

    resp = _build_mint_quote_response(mint_quote, amount_paid, amount_issued)
    logger.trace(f"< GET /v1/mint/quote/onchain/{quote}: {resp}")
    return resp


@router.post(
    "/v1/mint/onchain",
    name="Mint tokens with an onchain deposit",
    summary="Mint tokens after an onchain deposit is confirmed.",
    response_model=PostMintResponse,
    response_description=(
        "A list of blinded signatures that can be used to create proofs."
    ),
)
@limiter.limit(f"{settings.mint_transaction_rate_limit_per_minute}/minute")
async def onchain_mint(
    request: Request,
    payload: PostMintRequest,
) -> PostMintResponse:
    """NUT-XX mint. The wallet's `outputs` MUST sum to no more than
    (`amount_paid` - `amount_issued`); today that means the full deposit, since
    we don't yet support partial mints (see file docstring).

    NUT-20 `signature` is required by NUT-XX and enforced by the underlying
    `ledger.mint()` based on the quote's `pubkey`."""
    logger.trace(f"> POST /v1/mint/onchain: {payload}")
    promises = await ledger.mint(
        outputs=payload.outputs, quote_id=payload.quote, signature=payload.signature
    )
    blinded_signatures = PostMintResponse(signatures=promises)
    logger.trace(f"< POST /v1/mint/onchain: {blinded_signatures}")
    return blinded_signatures


# --------------- MELT (redeem ecash → send funds onchain) ---------------


def _build_melt_quote_response(
    quote, fee_options: List[OnchainFeeOption], outpoint=None, selected=None
) -> PostMeltQuoteOnchainResponse:
    return PostMeltQuoteOnchainResponse(
        quote=quote.quote,
        amount=quote.amount,
        unit=quote.unit,
        state=quote.state.value,
        expiry=quote.expiry,
        request=quote.request,
        fee_options=fee_options,
        selected_estimated_blocks=selected,
        outpoint=outpoint,
    )


def _melt_fee_options(quote_fee_reserve: int) -> List[OnchainFeeOption]:
    return [
        OnchainFeeOption(
            fee_reserve=quote_fee_reserve,
            estimated_blocks=_zec_estimated_blocks(),
        )
    ]


def _outpoint_for(quote) -> str | None:
    """Return the NUT-XX `outpoint` for a settled melt.

    For transparent Zcash this would be `txid:vout`; we don't currently track
    `vout` so we surface the bare txid. For shielded outputs (sapling/orchard)
    there is no public vout at all — txid is the strongest proof we can give.
    """
    if not quote.payment_preimage:
        return None
    return quote.payment_preimage  # populated from txid by ZcashBackend


@router.post(
    "/v1/melt/quote/onchain",
    summary="Request a quote for melting tokens via an onchain send",
    response_model=PostMeltQuoteOnchainResponse,
    response_description="Melt tokens for an onchain payment.",
)
@limiter.limit(f"{settings.mint_transaction_rate_limit_per_minute}/minute")
async def onchain_melt_quote(
    request: Request, payload: PostMeltQuoteOnchainRequest
) -> PostMeltQuoteOnchainResponse:
    """NUT-XX melt quote. `request` is an onchain address; `amount` is required
    because addresses do not encode amounts."""
    logger.trace(f"> POST /v1/melt/quote/onchain: {payload}")

    # Reuse the existing ledger.melt_quote() path. PostMeltQuoteOnchainRequest
    # carries the same fields as PostMeltQuoteRequest except for the missing
    # `options` block — we adapt by constructing one explicitly.
    internal = PostMeltQuoteRequest(
        unit=payload.unit,
        request=payload.request,
        amount=payload.amount,
        options=None,
    )
    quote = await ledger.melt_quote(internal, method_str=ONCHAIN_METHOD)
    resp = _build_melt_quote_response(
        quote=quote,
        fee_options=_melt_fee_options(quote.fee_reserve),
        outpoint=_outpoint_for(quote),
        selected=None,
    )
    logger.trace(f"< POST /v1/melt/quote/onchain: {resp}")
    return resp


@router.get(
    "/v1/melt/quote/onchain/{quote}",
    summary="Get onchain melt quote",
    response_model=PostMeltQuoteOnchainResponse,
)
@limiter.limit(f"{settings.mint_transaction_rate_limit_per_minute}/minute")
async def onchain_get_melt_quote(
    request: Request, quote: str
) -> PostMeltQuoteOnchainResponse:
    logger.trace(f"> GET /v1/melt/quote/onchain/{quote}")
    melt_quote = await ledger.get_melt_quote(quote)
    if melt_quote.method != ONCHAIN_METHOD:
        raise HTTPException(status_code=400, detail="not an onchain quote")
    resp = _build_melt_quote_response(
        quote=melt_quote,
        fee_options=_melt_fee_options(melt_quote.fee_reserve),
        outpoint=_outpoint_for(melt_quote),
        selected=_zec_estimated_blocks() if melt_quote.payment_preimage else None,
    )
    logger.trace(f"< GET /v1/melt/quote/onchain/{quote}: {resp}")
    return resp


@router.post(
    "/v1/melt/onchain",
    name="Melt tokens via an onchain send",
    summary="Melt tokens for an onchain payment",
    response_model=PostMeltQuoteOnchainResponse,
    response_description=(
        "The melt quote with state, outpoint (txid for Zcash), and the chosen "
        "fee tier."
    ),
)
@limiter.limit(f"{settings.mint_transaction_rate_limit_per_minute}/minute")
async def onchain_melt(
    request: Request, payload: PostMeltOnchainRequest
) -> PostMeltQuoteOnchainResponse:
    """NUT-XX melt. The wallet's `estimated_blocks` MUST match one of the
    `fee_options[].estimated_blocks` returned in the quote."""
    logger.trace(f"> POST /v1/melt/onchain: {payload}")

    # Look up the quote first so we can validate the wallet's selected fee tier
    # against the options we returned.
    melt_quote = await ledger.get_melt_quote(payload.quote)
    if melt_quote.method != ONCHAIN_METHOD:
        raise HTTPException(status_code=400, detail="not an onchain quote")

    valid_estimated_blocks = {
        opt.estimated_blocks for opt in _melt_fee_options(melt_quote.fee_reserve)
    }
    if payload.estimated_blocks not in valid_estimated_blocks:
        raise CashuError(
            detail=(
                f"estimated_blocks={payload.estimated_blocks} is not in the quote's "
                f"fee_options ({sorted(valid_estimated_blocks)})"
            ),
            code=2000,
        )

    melt_quote_after = await ledger.melt(
        proofs=payload.inputs, quote=payload.quote, outputs=payload.outputs
    )
    # ledger.melt returns a PostMeltQuoteResponse; we re-fetch the underlying
    # MeltQuote for the NUT-XX shape (we need state + payment_preimage).
    melt_quote = await ledger.get_melt_quote(payload.quote)
    resp = _build_melt_quote_response(
        quote=melt_quote,
        fee_options=_melt_fee_options(melt_quote.fee_reserve),
        outpoint=_outpoint_for(melt_quote),
        selected=payload.estimated_blocks,
    )
    logger.trace(f"< POST /v1/melt/onchain: {resp}")
    return resp
