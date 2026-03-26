"""Zcash onchain API routes for the Cashu mint.

Mirrors the bolt11 router pattern (NUT-04 mint, NUT-05 melt) but for
the `zcash` payment method. Endpoints:

  POST /v1/mint/quote/zcash        - Request mint quote (get address)
  GET  /v1/mint/quote/zcash/{id}   - Check mint quote status
  POST /v1/mint/zcash              - Mint tokens (after deposit confirmed)
  POST /v1/melt/quote/zcash        - Request melt quote (provide address + amount)
  GET  /v1/melt/quote/zcash/{id}   - Check melt quote status
  POST /v1/melt/zcash              - Execute melt (send ZEC onchain)
"""

from fastapi import APIRouter, Request
from loguru import logger

from ..core.models import (
    PostMeltQuoteRequest,
    PostMeltQuoteResponse,
    PostMeltRequest,
    PostMintQuoteRequest,
    PostMintQuoteResponse,
    PostMintRequest,
    PostMintResponse,
)
from ..core.settings import settings
from ..mint.startup import ledger
from .limit import limiter

router = APIRouter()

ZCASH_METHOD = "zcash"


# --------------- MINT (deposit ZEC → receive ecash) ---------------


@router.post(
    "/v1/mint/quote/zcash",
    name="Request Zcash mint quote",
    summary="Request a quote for minting tokens via Zcash onchain deposit",
    response_model=PostMintQuoteResponse,
    response_description="A Zcash address to send ZEC to for minting tokens",
)
@limiter.limit(f"{settings.mint_transaction_rate_limit_per_minute}/minute")
async def zcash_mint_quote(
    request: Request, payload: PostMintQuoteRequest
) -> PostMintQuoteResponse:
    """
    Request minting of new tokens via Zcash onchain deposit.
    The mint responds with a unified Zcash address.

    Call `POST /v1/mint/zcash` after the deposit is confirmed.
    """
    logger.trace(f"> POST /v1/mint/quote/zcash: payload={payload}")
    quote = await ledger.mint_quote(payload, method_str=ZCASH_METHOD)
    resp = PostMintQuoteResponse(
        quote=quote.quote,
        request=quote.request,
        amount=quote.amount,
        unit=quote.unit,
        paid=quote.paid,  # deprecated
        state=quote.state.value,
        expiry=quote.expiry,
        pubkey=quote.pubkey,
    )
    logger.trace(f"< POST /v1/mint/quote/zcash: {resp}")
    return resp


@router.get(
    "/v1/mint/quote/zcash/{quote}",
    summary="Get Zcash mint quote",
    response_model=PostMintQuoteResponse,
    response_description="Get an existing Zcash mint quote to check its status.",
)
@limiter.limit(f"{settings.mint_transaction_rate_limit_per_minute}/minute")
async def zcash_get_mint_quote(request: Request, quote: str) -> PostMintQuoteResponse:
    """
    Get Zcash mint quote state.
    """
    logger.trace(f"> GET /v1/mint/quote/zcash/{quote}")
    mint_quote = await ledger.get_mint_quote(quote)
    resp = PostMintQuoteResponse(
        quote=mint_quote.quote,
        request=mint_quote.request,
        amount=mint_quote.amount,
        unit=mint_quote.unit,
        paid=mint_quote.paid,  # deprecated
        state=mint_quote.state.value,
        expiry=mint_quote.expiry,
        pubkey=mint_quote.pubkey,
    )
    logger.trace(f"< GET /v1/mint/quote/zcash/{quote}")
    return resp


@router.post(
    "/v1/mint/zcash",
    name="Mint tokens with Zcash onchain deposit",
    summary="Mint tokens after a Zcash onchain deposit is confirmed.",
    response_model=PostMintResponse,
    response_description=(
        "A list of blinded signatures that can be used to create proofs."
    ),
)
@limiter.limit(f"{settings.mint_transaction_rate_limit_per_minute}/minute")
async def zcash_mint(
    request: Request,
    payload: PostMintRequest,
) -> PostMintResponse:
    """
    Requests the minting of tokens belonging to a paid Zcash deposit.

    Call this endpoint after `POST /v1/mint/quote/zcash` and after the
    onchain deposit has been confirmed.
    """
    logger.trace(f"> POST /v1/mint/zcash: {payload}")
    # ledger.mint() is method-agnostic: it looks up the quote (which stores
    # method="zcash") and dispatches to the correct backend
    promises = await ledger.mint(
        outputs=payload.outputs, quote_id=payload.quote, signature=payload.signature
    )
    blinded_signatures = PostMintResponse(signatures=promises)
    logger.trace(f"< POST /v1/mint/zcash: {blinded_signatures}")
    return blinded_signatures


# --------------- MELT (redeem ecash → send ZEC onchain) ---------------


@router.post(
    "/v1/melt/quote/zcash",
    summary="Request a quote for melting tokens via Zcash onchain send",
    response_model=PostMeltQuoteResponse,
    response_description="Melt tokens for a Zcash onchain payment.",
)
@limiter.limit(f"{settings.mint_transaction_rate_limit_per_minute}/minute")
async def zcash_melt_quote(
    request: Request, payload: PostMeltQuoteRequest
) -> PostMeltQuoteResponse:
    """
    Request a quote for melting tokens via Zcash onchain send.

    The `request` field should contain a Zcash address (unified, sapling,
    or transparent). The `amount` field is required (in zatoshi) since
    Zcash addresses do not encode amounts.
    """
    logger.trace(f"> POST /v1/melt/quote/zcash: {payload}")
    quote = await ledger.melt_quote(payload, method_str=ZCASH_METHOD)
    logger.trace(f"< POST /v1/melt/quote/zcash: {quote}")
    return quote


@router.get(
    "/v1/melt/quote/zcash/{quote}",
    summary="Get Zcash melt quote",
    response_model=PostMeltQuoteResponse,
    response_description="Get an existing Zcash melt quote to check its status.",
)
@limiter.limit(f"{settings.mint_transaction_rate_limit_per_minute}/minute")
async def zcash_get_melt_quote(request: Request, quote: str) -> PostMeltQuoteResponse:
    """
    Get Zcash melt quote state.
    """
    logger.trace(f"> GET /v1/melt/quote/zcash/{quote}")
    melt_quote = await ledger.get_melt_quote(quote)
    resp = PostMeltQuoteResponse(
        quote=melt_quote.quote,
        amount=melt_quote.amount,
        unit=melt_quote.unit,
        request=melt_quote.request,
        fee_reserve=melt_quote.fee_reserve,
        paid=melt_quote.paid,
        state=melt_quote.state.value,
        expiry=melt_quote.expiry,
        payment_preimage=melt_quote.payment_preimage,
        change=melt_quote.change,
    )
    logger.trace(f"< GET /v1/melt/quote/zcash/{quote}")
    return resp


@router.post(
    "/v1/melt/zcash",
    name="Melt tokens via Zcash onchain",
    summary="Melt tokens for a Zcash onchain payment",
    response_model=PostMeltQuoteResponse,
    response_description=(
        "The state of the payment, a txid as proof of payment, and a list of "
        "promises for change."
    ),
)
@limiter.limit(f"{settings.mint_transaction_rate_limit_per_minute}/minute")
async def zcash_melt(
    request: Request, payload: PostMeltRequest
) -> PostMeltQuoteResponse:
    """
    Requests tokens to be destroyed and sent out via Zcash onchain.
    """
    logger.trace(f"> POST /v1/melt/zcash: {payload}")
    # ledger.melt() is method-agnostic: it looks up the quote (which stores
    # method="zcash") and dispatches to the correct backend
    resp = await ledger.melt(
        proofs=payload.inputs, quote=payload.quote, outputs=payload.outputs
    )
    logger.trace(f"< POST /v1/melt/zcash: {resp}")
    return resp
