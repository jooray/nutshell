from __future__ import annotations

import asyncio
import math
import time
from typing import AsyncGenerator, Dict, Optional

import httpx
from loguru import logger

from cashu.core.base import Amount, MeltQuote, Unit
from cashu.core.db import Database
from cashu.core.models import PostMeltQuoteRequest, PostMeltRequestOptions, PostMeltRequestOptionMpp
from cashu.core.settings import settings
from cashu.mint.crud import LedgerCrud
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


class FiatBackend(LightningBackend):
    """A wrapper for any LightningBackend that adds support for fiat units."""

    supported_units: set[Unit]

    def __init__(
        self,
        backend: LightningBackend,
        crud: LedgerCrud,
        db: Optional[Database] = None,
        *,
        cache_seconds: int = 300,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        if Unit.sat not in backend.supported_units:
            raise Unsupported("The wrapped Lightning backend must support 'sat' unit")

        self.backend = backend
        self.crud = crud
        self.db = db
        self._client = http_client or httpx.AsyncClient(timeout=10)
        self._cache_seconds = cache_seconds

        self.supports_mpp = backend.supports_mpp

        # ─── Which units does THIS backend cover? ───────────────────────
        fiat_units: set[Unit] = {
            Unit[c.lower()]
            for c in settings.mint_fiat_backend_units
            if c.lower() in Unit.__members__
        }
        if not fiat_units:
            raise Unsupported("No valid fiat units configured. Please check MINT_FIAT_BACKEND_UNITS setting")

        self._fiat_units = fiat_units
        self.supported_units = self.backend.supported_units.union(self._fiat_units)

        # ─── Precision & fee tables ─────────────────────────────────────
        self._decimals = {u: u.decimals for u in self._fiat_units}

        self._mint_fee = {
            u: settings.fiat_backend_mint_fee.get(u.name, 0.0)
            for u in self._fiat_units
        }
        self._melt_fee = {
            u: settings.fiat_backend_melt_fee.get(u.name, 0.0)
            for u in self._fiat_units
        }

        # ─── FX cache state ─────────────────────────────────────────────
        self._msat_per_unit: Dict[Unit, float] = {}
        self._rates_ts = 0.0
        self._rates_lock = asyncio.Lock()

        # ─── accounting (optional introspection) ────────────────────────
        self._minted = {u: 0 for u in self._fiat_units}
        self._melted = {u: 0 for u in self._fiat_units}

    # ────────────────────────── FX helpers ───────────────────────────────
    async def _ensure_rates(self) -> None:
        async with self._rates_lock:
            if time.time() - self._rates_ts < self._cache_seconds:
                return

            symbols = ",".join(u.name for u in self._fiat_units).lower()
            url = (
                "https://api.coingecko.com/api/v3/simple/price"
                f"?ids=bitcoin&vs_currencies={symbols}"
            )

            try:
                r = await self._client.get(url)
                r.raise_for_status()
                data = r.json()["bitcoin"]
                for u in self._fiat_units:
                    if (v := data.get(u.name)) not in (None, 0):
                        msats_per_btc = 1e11  # 100 billion msats per bitcoin
                        main_units_per_btc = float(v)
                        smallest_units_per_btc = main_units_per_btc * (10**self._decimals[u])
                        self._msat_per_unit[u] = msats_per_btc / smallest_units_per_btc

                if Unit.usd not in self._msat_per_unit:
                    raise RuntimeError("Failed to get USD/BTC exchange rate from API")

                self._rates_ts = time.time()
            except Exception as exc:
                logger.error(f"FX fetch failed: {exc}")
                raise RuntimeError(f"Unable to fetch exchange rates: {exc}")

    async def _fiat_to_msat(self, amount: Amount) -> int:
        await self._ensure_rates()
        # Always round up when converting fiat to msats (mint receives more)
        return int(math.ceil(amount.amount * self._msat_per_unit[amount.unit]))

    async def _msat_to_fiat(self, msat: int, unit: Unit) -> Amount:
        await self._ensure_rates()
        # Always round up when converting msats to fiat (user pays more)
        sub = int(math.ceil(msat / self._msat_per_unit[unit]))
        return Amount(unit, sub)

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
                logger.info(f"Creating invoice for {amount.str()} with mint fee {self._mint_fee[amount.unit]}%")
                fee_percent = self._mint_fee[amount.unit]
                gross = int(math.ceil(amount.amount * (1 + fee_percent / 100)))
                fee_amount = gross - amount.amount
                msats = await self._fiat_to_msat(Amount(amount.unit, gross))

                await self._ensure_rates()
                exchange_rate = self._msat_per_unit[amount.unit] / 1000

                resp = await self.backend.create_invoice(
                    Amount(Unit.msat, msats),
                    memo=memo,
                    description_hash=description_hash,
                    unhashed_description=unhashed_description,
                    **kwargs,
                )
                if resp.ok:
                    self._minted[amount.unit] += amount.amount

                    # Record in database if available
                    if self.db and self.crud:
                        try:
                            await self.crud.store_unit_accounting_entry(
                                db=self.db,
                                unit=amount.unit.name,
                                amount=amount.amount,
                                operation="mint",
                                exchange_rate=exchange_rate,
                                sat_amount=msats // 1000,
                                fee_percent=fee_percent,
                                fee_amount=fee_amount,
                            )
                        except Exception as e:
                            logger.error(f"Failed to record mint accounting: {e}")

                return resp
            except RuntimeError as e:
                return InvoiceResponse(ok=False, error_message=f"Failed to create invoice: {str(e)}")

        return await self.backend.create_invoice(
            amount,
            memo=memo,
            description_hash=description_hash,
            unhashed_description=unhashed_description,
            **kwargs,
        )

    async def pay_invoice(self, quote: MeltQuote, fee_limit_msat: int, **kwargs) -> PaymentResponse:
        unit = Unit(quote.unit) if isinstance(quote.unit, str) else (quote.unit or Unit.sat)

        if unit in self._fiat_units:
            try:
                logger.info(f"Paying invoice for {quote.amount} {unit.name} with fee reserve {quote.fee_reserve}")

                # TODO FOR MPP
                # Melt quote by itself does not have a flag for MPP payments.
                # Other backends figure it out by parsing the request (e.g. Bolt11) and
                # checking if the amount is larger than the amount in the melt quote.
                # Maybe adding the flag to MeltQuote would be better?
                # TODO FOR MPP

                # Re-calculate the current quote to verify the total amount is still sufficient
                fresh_fiat_quote_request = PostMeltQuoteRequest(request=quote.request, unit=unit.name)
                fresh_fiat_quote = await self.get_payment_quote(fresh_fiat_quote_request)

                if hasattr(fresh_fiat_quote, 'error_message') and fresh_fiat_quote.error_message:
                    logger.error(f"Failed to get fresh fiat quote: {fresh_fiat_quote.error_message}")
                    return PaymentResponse(
                        result=PaymentResult.FAILED,
                        error_message=f"Payment validation failed: {fresh_fiat_quote.error_message}"
                    )

                # Check if the total required amount has increased (due to exchange rate or fee changes)
                if quote.amount < fresh_fiat_quote.amount.amount:
                    logger.error(
                        f"Amount change: original quote amount {quote.amount} {unit.name} "
                        f"but current conditions require {fresh_fiat_quote.amount.amount} {unit.name}"
                    )
                    return PaymentResponse(
                        result=PaymentResult.FAILED,
                        error_message=(
                            "Amount, exchange rate, or fees have changed. "
                            f"Original total: {quote.amount} {unit.name}, "
                            f"Current total: {fresh_fiat_quote.amount.amount} {unit.name}. "
                            "Please request a new quote."
                        )
                    )

                # Get current LN parameters to execute the payment
                current_ln_params_quote = await self.backend.get_payment_quote(
                    PostMeltQuoteRequest(request=quote.request, unit="msat"))

                if hasattr(current_ln_params_quote, 'error_message') and current_ln_params_quote.error_message:
                    logger.error(f"Failed to get current LN parameters: {current_ln_params_quote.error_message}")
                    return PaymentResponse(
                        result=PaymentResult.FAILED,
                        error_message=f"Failed to get current LN parameters: {current_ln_params_quote.error_message}"
                    )

                current_ln_invoice_amount_msat = current_ln_params_quote.amount.to(Unit.msat).amount
                current_ln_fee_msat = current_ln_params_quote.fee.to(Unit.msat).amount

                # For fiat payments, the user has already paid the full amount including all fees
                # We don't need to check fee_reserve as the fiat amount already covers the maximum possible fee
                # Any difference in actual vs estimated fee is absorbed by the mint

                await self._ensure_rates()
                exchange_rate = self._msat_per_unit[unit] / 1000
                cashu_melt_fee_percent = self._melt_fee[unit]

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

                resp = await self.backend.pay_invoice(msat_quote, fee_limit_msat, **kwargs)

                if resp.fee:
                    paid_ln_fee_msat = resp.fee.to(Unit.msat).amount
                    resp.fee = await self._msat_to_fiat(paid_ln_fee_msat, unit)

                if resp.result == PaymentResult.SETTLED and self.db and self.crud:
                    try:
                        # Principal amount is the fiat equivalent of the LN invoice amount
                        principal_fiat_amount = await self._msat_to_fiat(current_ln_invoice_amount_msat, unit)

                        # Cashu melt fee in fiat
                        cashu_melt_fee_msat = int(math.ceil(current_ln_invoice_amount_msat * cashu_melt_fee_percent / 100))
                        cashu_melt_fee_fiat = await self._msat_to_fiat(cashu_melt_fee_msat, unit)

                        await self.crud.store_unit_accounting_entry(
                            db=self.db,
                            unit=unit.name,
                            amount=principal_fiat_amount.amount,
                            operation="melt",
                            exchange_rate=exchange_rate,
                            sat_amount=current_ln_invoice_amount_msat // 1000,
                            fee_percent=cashu_melt_fee_percent,
                            fee_amount=cashu_melt_fee_fiat.amount,
                        )
                        self._melted[unit] += principal_fiat_amount.amount
                    except Exception as e:
                        logger.error(f"Failed to record melt accounting for fiat unit {unit.name}: {e}")

            except Exception as e:
                logger.error(f"Could not process fiat payment: {e}")
                return PaymentResponse(
                    result=PaymentResult.FAILED,
                    error_message=f"Payment processing error: {str(e)}"
                )
        else:
            resp = await self.backend.pay_invoice(quote, fee_limit_msat, **kwargs)

        return resp

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        return await self.backend.get_invoice_status(checking_id)

    async def get_payment_status(self, checking_id: str) -> PaymentStatus:
        return await self.backend.get_payment_status(checking_id)

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        async for p in self.backend.paid_invoices_stream():
            yield p

    async def get_payment_quote(self, melt_quote: PostMeltQuoteRequest) -> PaymentQuoteResponse:

        if self.supports_mpp and melt_quote.is_mpp:
            backend_request = PostMeltQuoteRequest(
                request=melt_quote.request,
                unit="msat",
                options=PostMeltRequestOptions(
                    mpp=PostMeltRequestOptionMpp(amount=melt_quote.mpp_amount)
                )
            )
        else:
            backend_request = PostMeltQuoteRequest(
               request=melt_quote.request,
               unit="msat"
            )

        ln_quote = await self.backend.get_payment_quote(backend_request)

        unit = Unit(melt_quote.unit) if isinstance(melt_quote.unit, str) else (melt_quote.unit or Unit.sat)

        if unit not in self._fiat_units:
                return ln_quote

        try:
            ln_amount_msat = ln_quote.amount.to(Unit.msat).amount
            ln_fee_msat = ln_quote.fee.to(Unit.msat).amount

            melt_fee_percent = self._melt_fee[unit]
            melt_fee_msat = int(math.ceil(ln_amount_msat * melt_fee_percent / 100))

            # Include all fees in the total amount
            total_amount_msat = ln_amount_msat + ln_fee_msat + melt_fee_msat

            await self._ensure_rates()

            total_amount_fiat = await self._msat_to_fiat(total_amount_msat, unit)

            response = PaymentQuoteResponse(
                checking_id=ln_quote.checking_id,
                amount=total_amount_fiat,
                fee=Amount(unit, 0),  # Fee is already included in the amount
                fee_reserve_msat=0 # No separate fee reserve for fiat payments
            )

            return response
        except RuntimeError as e:
            logger.error(f"Error in get_payment_quote: {e}")
            return PaymentQuoteResponse(
                checking_id=ln_quote.checking_id,
                amount=Amount(unit, 0),
                fee=Amount(unit, 0),
                error_message=f"Failed to calculate quote: {str(e)}",
            )

    # ───────────────────────── introspection ────────────────────────────
    @property
    def minted_totals(self) -> Dict[Unit, int]:
        return self._minted.copy()

    @property
    def melted_totals(self) -> Dict[Unit, int]:
        return self._melted.copy()
