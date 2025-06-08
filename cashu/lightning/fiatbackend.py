from __future__ import annotations

import asyncio
import math
import time
from typing import AsyncGenerator, Dict, Optional

import httpx
from loguru import logger

from cashu.core.base import Amount, MeltQuote, Unit
from cashu.core.db import Database
from cashu.core.models import PostMeltQuoteRequest
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
            raise Unsupported("wrapped backend must at least support sat")

        self.backend = backend
        self.crud = crud
        self.db = db
        self._client = http_client or httpx.AsyncClient(timeout=10)
        self._cache_seconds = cache_seconds

        # ─── Which units does THIS backend cover? ───────────────────────
        fiat_units: set[Unit] = {
            Unit[c.lower()]
            for c in settings.mint_fiat_backend_units
            if c.lower() in Unit.__members__
        }
        if not fiat_units:
            raise Unsupported("FIAT_BACKEND_UNITS empty or unknown codes")

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
        self._sat_per_unit: Dict[Unit, float] = {}
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
                        sats_per_btc = 1e8
                        main_units_per_btc = float(v)
                        smallest_units_per_btc = main_units_per_btc * (10**self._decimals[u])
                        self._sat_per_unit[u] = sats_per_btc / smallest_units_per_btc

                if Unit.usd not in self._sat_per_unit:
                    raise RuntimeError("USD/BTC quote missing")

                self._rates_ts = time.time()
            except Exception as exc:
                logger.error(f"FX fetch failed: {exc}")
                raise RuntimeError(f"Unable to fetch exchange rates: {exc}")

    async def _fiat_to_sat(self, amount: Amount) -> int:
        await self._ensure_rates()
        # Always round up when converting fiat to sats (mint receives more)
        return int(math.ceil(amount.amount * self._sat_per_unit[amount.unit]))

    async def _sat_to_fiat(self, sat: int, unit: Unit) -> Amount:
        await self._ensure_rates()
        # Always round up when converting sats to fiat (user pays more)
        sub = int(math.ceil(sat / self._sat_per_unit[unit]))
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
                sats = await self._fiat_to_sat(Amount(amount.unit, gross))

                await self._ensure_rates()
                exchange_rate = self._sat_per_unit[amount.unit]

                resp = await self.backend.create_invoice(
                    Amount(Unit.sat, sats),
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
                                sat_amount=sats,
                                fee_percent=fee_percent,
                                fee_amount=fee_amount,
                            )
                        except Exception as e:
                            logger.error(f"Failed to record mint accounting: {e}")

                return resp
            except RuntimeError as e:
                return InvoiceResponse(ok=False, error_message=str(e))

        return await self.backend.create_invoice(
            amount,
            memo=memo,
            description_hash=description_hash,
            unhashed_description=unhashed_description,
            **kwargs,
        )

    async def pay_invoice(self, quote: MeltQuote, fee_limit_msat: int, **kwargs) -> PaymentResponse:
        if quote.unit in self._fiat_units:
            # Get a fresh quote to validate exchange rate hasn't changed significantly
            try:
                fresh_quote_request = PostMeltQuoteRequest(request=quote.request, unit=quote.unit)
                fresh_quote = await self.get_payment_quote(fresh_quote_request)

                if not fresh_quote.ok:
                    return PaymentResponse(
                        result=PaymentResult.FAILED,
                        error_message=f"Failed to validate payment: {fresh_quote.error_message}"
                    )

                # Check if the user provided enough fiat for current exchange rate
                if quote.amount < fresh_quote.amount.amount:
                    logger.error(
                        f"Exchange rate mismatch: original quote amount {quote.amount} {quote.unit} "
                        f"but current rate requires {fresh_quote.amount.amount} {quote.unit}"
                    )
                    return PaymentResponse(
                        result=PaymentResult.FAILED,
                        error_message="Exchange rate has changed. Please get a new quote."
                    )

                # Convert the original fee_reserve from sats to fiat for comparison
                original_fee_fiat = await self._sat_to_fiat(quote.fee_reserve, quote.unit)
                if original_fee_fiat.amount < fresh_quote.fee.amount:
                    logger.error(
                        f"Fee increase: original fee reserve {original_fee_fiat.amount} {quote.unit} "
                        f"but current fee is {fresh_quote.fee.amount} {quote.unit}"
                    )
                    return PaymentResponse(
                        result=PaymentResult.FAILED,
                        error_message="Lightning fees have increased. Please get a new quote."
                    )

                # Store the exchange rate and fee info for accounting
                exchange_rate = self._sat_per_unit[quote.unit]
                fee_percent = self._melt_fee[quote.unit]
            except Exception as e:
                logger.error(f"Could not validate payment amount: {e}")
                return PaymentResponse(
                    result=PaymentResult.FAILED,
                    error_message=f"Failed to validate payment: {str(e)}"
                )

            sat_amount = await self._fiat_to_sat(Amount(quote.unit, quote.amount))

            sat_quote = MeltQuote(
                quote=quote.quote,
                method=quote.method,
                request=quote.request,
                checking_id=quote.checking_id,
                unit="sat",
                amount=sat_amount,
                fee_reserve=quote.fee_reserve,
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

            resp = await self.backend.pay_invoice(sat_quote, fee_limit_msat, **kwargs)

            if resp.fee:
                fee_sat = resp.fee.to(Unit.sat).amount
                fiat_fee = await self._sat_to_fiat(fee_sat, quote.unit)
                resp.fee = fiat_fee
        else:
            resp = await self.backend.pay_invoice(quote, fee_limit_msat, **kwargs)

        if resp.result == PaymentResult.SETTLED and quote.unit in self._fiat_units and self.db and self.crud:
            try:
                # Use the values we already calculated above
                fee_amount = int(math.ceil(quote.amount * fee_percent / 100))

                await self.crud.store_unit_accounting_entry(
                    db=self.db,
                    unit=quote.unit.name,
                    amount=quote.amount,
                    operation="melt",
                    exchange_rate=exchange_rate,
                    sat_amount=sat_amount,
                    fee_percent=fee_percent,
                    fee_amount=fee_amount,
                )
                self._melted[quote.unit] += quote.amount
            except Exception as e:
                logger.error(f"Failed to record melt accounting: {e}")

        return resp

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        return await self.backend.get_invoice_status(checking_id)

    async def get_payment_status(self, checking_id: str) -> PaymentStatus:
        return await self.backend.get_payment_status(checking_id)

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        async for p in self.backend.paid_invoices_stream():
            yield p

    async def get_payment_quote(self, melt_quote: PostMeltQuoteRequest) -> PaymentQuoteResponse:
        ln_quote = await self.backend.get_payment_quote(melt_quote)
        unit = melt_quote.unit or Unit.sat

        if unit not in self._fiat_units:
            return ln_quote

        try:
            # Get amounts in sats
            ln_amount_sat = ln_quote.amount.to(Unit.sat).amount
            ln_fee_sat = ln_quote.fee.to(Unit.sat).amount

            melt_fee_percent = self._melt_fee[unit]
            melt_fee_sat = int(math.ceil(ln_amount_sat * melt_fee_percent / 100))

            total_fee_sat = ln_fee_sat + melt_fee_sat

            total_amount_fiat = await self._sat_to_fiat(ln_amount_sat, unit)
            total_fee_fiat = await self._sat_to_fiat(total_fee_sat, unit)

            return PaymentQuoteResponse(
                ok=ln_quote.ok,
                checking_id=ln_quote.checking_id,
                amount=total_amount_fiat,
                fee=total_fee_fiat,
            )
        except RuntimeError as e:
            return PaymentQuoteResponse(
                ok=False,
                checking_id=ln_quote.checking_id,
                amount=Amount(unit, 0),
                fee=Amount(unit, 0),
                error_message=str(e),
            )

    # ───────────────────────── introspection ────────────────────────────
    @property
    def minted_totals(self) -> Dict[Unit, int]:
        return self._minted.copy()

    @property
    def melted_totals(self) -> Dict[Unit, int]:
        return self._melted.copy()
