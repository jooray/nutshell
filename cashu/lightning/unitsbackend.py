from __future__ import annotations

import asyncio
from typing import AsyncGenerator, Dict, List, Optional

import httpx
from loguru import logger

from cashu.core.base import Amount, MeltQuote, Unit
from cashu.core.models import PostMeltQuoteRequest
from cashu.core.settings import settings
from .base import (
    Amount,
    InvoiceResponse,
    LightningBackend,
    PaymentQuoteResponse,
    PaymentResponse,
    PaymentResult,
    PaymentStatus,
    StatusResponse,
    Unit,
    Unsupported,
)


class UnitsBackend(LightningBackend):
    """A wrapper for any LightningBackend that adds support for custom units via unitsd."""

    supported_units: set[Unit]

    def __init__(
        self,
        backend: LightningBackend,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        if Unit.sat not in backend.supported_units:
            raise Unsupported("The wrapped Lightning backend must support 'sat' unit")

        self.backend = backend
        self._client = http_client or httpx.AsyncClient(timeout=10)

        # ─── Unitsd configuration ────────────────────────────────────────
        if not settings.unitsd_url:
            raise Unsupported("UNITSD_URL not configured")
        if not settings.unitsd_api_secret:
            raise Unsupported("UNITSD_API_SECRET not configured")

        self._unitsd_url = settings.unitsd_url.rstrip("/")
        self._unitsd_headers = {
            "Authorization": f"Bearer {settings.unitsd_api_secret}",
            "Content-Type": "application/json",
        }

        # ─── Cached units from unitsd ────────────────────────────────────
        self._unitsd_units: List[dict] = []

        # ─── Initialize fiat units (can be populated later via fetch_units_from_unitsd)
        self._fiat_units: set[Unit] = set()
        self.supported_units = self.backend.supported_units.copy()

        # ─── Pending mint hedging callbacks ──────────────────────────────
        # Maps a deposit invoice's checking_id -> conversion details captured at
        # create_invoice time. The hedging callback is deferred until the invoice
        # is actually paid (see _maybe_send_mint_callback), not sent at quote time.
        self._pending_mints: Dict[str, dict] = {}

    # ────────────────────────── Unitsd Unit Discovery ──────────────────────────

    async def fetch_units_from_unitsd(self) -> List[dict]:
        """Fetch enabled units with derivation paths from unitsd.

        Returns:
            List of unit info dicts containing code, name, decimals,
            mint_fee_percent, melt_fee_percent, path_index, derivation_path
        """
        data = await self._call_unitsd("/api/v1/units")
        self._unitsd_units = data.get("units", [])

        # Update fiat units and supported units based on fetched data
        for unit_info in self._unitsd_units:
            try:
                unit = Unit(unit_info["code"].lower())
                self._fiat_units.add(unit)

                decimals = unit_info.get("decimals")
                if isinstance(decimals, int):
                    settings.mint_unit_decimals[unit.name] = decimals
            except (KeyError, ValueError):
                logger.warning(f"Unknown unit from unitsd: {unit_info.get('code')}")

        self.supported_units = self.backend.supported_units.union(self._fiat_units)
        return self._unitsd_units

    def get_derivation_paths(self) -> List[str]:
        """Get derivation paths for all unitsd-managed units.

        Must call fetch_units_from_unitsd() first.
        """
        return [u["derivation_path"] for u in self._unitsd_units]

    def get_supported_unit_codes(self) -> List[str]:
        """Get unit codes for all unitsd-managed units.

        Must call fetch_units_from_unitsd() first.
        """
        return [u["code"].lower() for u in self._unitsd_units]

    # ────────────────────────── Unitsd API helpers ───────────────────────────────
    async def _call_unitsd(
        self, endpoint: str, params: Optional[Dict] = None, json: Optional[Dict] = None
    ) -> Dict:
        """Make a request to unitsd API."""
        url = f"{self._unitsd_url}{endpoint}"
        try:
            if json is not None:
                response = await self._client.post(
                    url, headers=self._unitsd_headers, json=json
                )
            else:
                response = await self._client.get(
                    url, headers=self._unitsd_headers, params=params
                )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"Unitsd API call failed: {endpoint} - {e}")
            raise RuntimeError(f"Failed to communicate with unitsd: {e}")

    # ─────────────────── LightningBackend interface ─────────────────────
    async def status(self) -> StatusResponse:  # pragma: no cover
        return await self.backend.status()

    async def create_invoice(
        self,
        amount: Amount,
        memo: str | None = None,
        description_hash: bytes | None = None,
        unhashed_description: str | None = None,
        **kwargs,
    ) -> InvoiceResponse:
        if amount.unit in self._fiat_units:
            try:
                logger.info(f"Creating invoice for {amount.str()}")

                # Get quote from unitsd
                quote_data = await self._call_unitsd(
                    "/api/v1/quote/mint",
                    params={"amount": amount.amount, "unit": amount.unit.name},
                )

                msats = quote_data["amount_msat"]
                btc_price = quote_data.get("btc_price", 0.0)
                logger.info(
                    f"Unitsd quote: {amount.amount} {amount.unit.name} = {msats} msats"
                    f" (BTC price: {btc_price})"
                )

                resp = await self.backend.create_invoice(
                    Amount(Unit.msat, msats),
                    memo=memo,
                    description_hash=description_hash,
                    unhashed_description=unhashed_description,
                    **kwargs,
                )

                # Defer the hedging callback until the invoice is actually paid.
                # Recording a position here (at quote/invoice-creation time) would
                # hedge against mints that may never happen. Cache the conversion
                # details keyed by checking_id; the callback is sent from
                # get_invoice_status / paid_invoices_stream once the wrapped backend
                # reports the invoice settled (see _maybe_send_mint_callback).
                if resp.ok and resp.checking_id:
                    self._pending_mints[resp.checking_id] = {
                        "unit": amount.unit.name,
                        "amount": amount.amount,
                        "msat_amount": msats,
                        "btc_price": btc_price,
                    }

                return resp
            except RuntimeError as e:
                return InvoiceResponse(
                    ok=False, error_message=f"Failed to create invoice: {str(e)}"
                )

        return await self.backend.create_invoice(
            amount,
            memo=memo,
            description_hash=description_hash,
            unhashed_description=unhashed_description,
            **kwargs,
        )

    async def pay_invoice(
        self, quote: MeltQuote, fee_limit_msat: int, **kwargs
    ) -> PaymentResponse:
        unit = (
            Unit(quote.unit)
            if isinstance(quote.unit, str)
            else (quote.unit or Unit.sat)
        )

        if unit in self._fiat_units:
            try:
                logger.info(f"Paying invoice for {quote.amount} {unit.name}")

                # Get current LN parameters from backend to execute the payment
                current_ln_params_quote = await self.backend.get_payment_quote(
                    PostMeltQuoteRequest(request=quote.request, unit="msat")
                )

                if (
                    hasattr(current_ln_params_quote, "error_message")
                    and current_ln_params_quote.error_message
                ):
                    logger.error(
                        f"Failed to get current LN parameters: {current_ln_params_quote.error_message}"
                    )
                    return PaymentResponse(
                        result=PaymentResult.FAILED,
                        error_message=f"Failed to get current LN parameters: {current_ln_params_quote.error_message}",
                    )

                current_ln_invoice_amount_msat = current_ln_params_quote.amount.to(
                    Unit.msat
                ).amount
                current_ln_fee_msat = current_ln_params_quote.fee.to(Unit.msat).amount

                # Create msat quote for backend execution
                msat_quote = MeltQuote(
                    quote=quote.quote,
                    method=quote.method,
                    request=quote.request,
                    checking_id=quote.checking_id,
                    unit="msat",
                    amount=current_ln_invoice_amount_msat,
                    fee_reserve=current_ln_fee_msat,
                    state=quote.state,
                    created_time=quote.created_time,
                    paid_time=quote.paid_time,
                    fee_paid=quote.fee_paid,
                    payment_preimage=quote.payment_preimage,
                    expiry=quote.expiry,
                    outputs=quote.outputs,
                    change=quote.change,
                    mint=quote.mint,
                )

                # Execute payment via backend
                resp = await self.backend.pay_invoice(
                    msat_quote, fee_limit_msat, **kwargs
                )

                # If payment succeeded, send melt callback to unitsd
                if resp.result == PaymentResult.SETTLED:
                    try:
                        actual_fee_msat = (
                            resp.fee.to(Unit.msat).amount if resp.fee else 0
                        )
                        total_msat = current_ln_invoice_amount_msat + actual_fee_msat

                        # Get BTC price for the callback - use the melt quote endpoint
                        btc_price = 0.0
                        try:
                            price_data = await self._call_unitsd(
                                "/api/v1/quote/melt",
                                params={
                                    "unit": unit.name,
                                    "invoice_msat": current_ln_invoice_amount_msat,
                                    "ln_fee_msat": actual_fee_msat,
                                },
                            )
                            btc_price = price_data.get("btc_price", 0.0)
                        except Exception:
                            pass

                        await self._call_unitsd(
                            "/api/v1/callback/melt",
                            json={
                                "quote_id": quote.quote,
                                "unit": unit.name,
                                "amount": quote.amount,
                                "msat_amount": total_msat,
                                "btc_price": btc_price,
                            },
                        )
                        logger.info(
                            f"Sent melt callback to unitsd: {quote.amount} {unit.name}"
                        )
                    except Exception as e:
                        logger.error(f"Failed to send melt callback to unitsd: {e}")
                        # Don't fail the payment if callback fails

                # Set fee to 0 in fiat (Lightning fees are absorbed into the conversion)
                if resp.fee:
                    resp.fee = Amount(unit, 0)

            except Exception as e:
                logger.error(f"Could not process fiat payment: {e}")
                return PaymentResponse(
                    result=PaymentResult.FAILED,
                    error_message=f"Payment processing error: {str(e)}",
                )
        else:
            resp = await self.backend.pay_invoice(quote, fee_limit_msat, **kwargs)

        return resp

    async def _maybe_send_mint_callback(self, checking_id: str) -> None:
        """Send the mint hedging callback exactly once, when the deposit invoice
        is settled.

        Metadata was cached at create_invoice time and is popped here, so a
        payment observed by both the poller (get_invoice_status) and the
        invoice-listener stream only fires one callback. unitsd also dedups on
        quote_id as a backstop.

        Non-fiat (sat) invoices were never cached, so this is a no-op for them.

        NOTE: the cache is in-memory, so a mint restart between quote creation
        and payment drops the pending entry and the callback for that mint is
        skipped. That under-counts the mint position (conservative), versus the
        previous behaviour of over-counting by firing at quote time.
        """
        info = self._pending_mints.pop(checking_id, None)
        if not info:
            return
        try:
            await self._call_unitsd(
                "/api/v1/callback/mint",
                json={
                    "quote_id": checking_id,
                    "unit": info["unit"],
                    "amount": info["amount"],
                    "msat_amount": info["msat_amount"],
                    "btc_price": info["btc_price"],
                },
            )
            logger.info(
                f"Sent mint callback to unitsd: {info['amount']} {info['unit']}"
            )
        except Exception as e:
            # Re-cache so a later poll/stream observation can retry the callback.
            self._pending_mints[checking_id] = info
            logger.error(f"Failed to send mint callback to unitsd: {e}")

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        status = await self.backend.get_invoice_status(checking_id)
        if status.settled:
            await self._maybe_send_mint_callback(checking_id)
        return status

    async def get_payment_status(self, checking_id: str) -> PaymentStatus:
        return await self.backend.get_payment_status(checking_id)

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        async for p in self.backend.paid_invoices_stream():
            await self._maybe_send_mint_callback(p)
            yield p

    async def get_payment_quote(
        self, melt_quote: PostMeltQuoteRequest
    ) -> PaymentQuoteResponse:
        ln_quote = await self.backend.get_payment_quote(
            PostMeltQuoteRequest(request=melt_quote.request, unit="sat")
        )

        unit = (
            Unit(melt_quote.unit)
            if isinstance(melt_quote.unit, str)
            else (melt_quote.unit or Unit.sat)
        )

        if unit not in self._fiat_units:
            return ln_quote

        try:
            # Get the invoice amount in msat from the LN quote
            invoice_msat = ln_quote.amount.to(Unit.msat).amount
            ln_fee_msat = ln_quote.fee.to(Unit.msat).amount

            # Get quote from unitsd for melt operation
            quote_data = await self._call_unitsd(
                "/api/v1/quote/melt",
                params={
                    "unit": unit.name,
                    "invoice_msat": invoice_msat,
                    "ln_fee_msat": ln_fee_msat,
                },
            )

            fiat_amount = quote_data["amount_fiat"]
            logger.info(
                f"Unitsd melt quote: {fiat_amount} {unit.name} for invoice"
                f" {melt_quote.request[:20]}... ({invoice_msat} msat)"
            )

            response = PaymentQuoteResponse(
                checking_id=ln_quote.checking_id,
                amount=Amount(unit, fiat_amount),
                fee=Amount(unit, 0),  # Fee is already included in the amount
            )

            return response
        except RuntimeError as e:
            logger.error(f"Error in get_payment_quote: {e}")
            raise RuntimeError(f"Failed to calculate quote: {str(e)}")
