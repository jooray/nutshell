from __future__ import annotations

import asyncio
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
        return int(round(amount.amount * self._sat_per_unit[amount.unit]))

    async def _sat_to_fiat(self, sat: int, unit: Unit) -> Amount:
        await self._ensure_rates()
        sub = int(round(sat / self._sat_per_unit[unit]))
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
                gross = int(round(amount.amount * (1 + fee_percent / 100)))
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
            fee_limit_sat = await self._fiat_to_sat(Amount(quote.unit, fee_limit_msat // 1000))
            fee_limit_msat = fee_limit_sat * 1000

        resp = await self.backend.pay_invoice(quote, fee_limit_msat, **kwargs)

        if resp.result == PaymentResult.SETTLED and quote.unit in self._fiat_units and self.db and self.crud:
            try:
                await self._ensure_rates()
                exchange_rate = self._sat_per_unit[quote.unit]
                fee_percent = self._melt_fee[quote.unit]
                fee_amount = int(round(quote.amount * fee_percent / 100))
                sat_amount = await self._fiat_to_sat(quote.amount)

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

    async def pay_invoice_with_quote(self, quote: MeltQuote, **kwargs) -> PaymentResult:
        resp = await self.backend.pay_invoice_with_quote(quote, **kwargs)
        if resp.settled and quote.unit in self._fiat_units:
            self._melted[quote.unit] += quote.amount.amount
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
            amt = await self._sat_to_fiat(ln_quote.amount.to(Unit.sat).amount, unit)
            fee = await self._sat_to_fiat(ln_quote.fee.to(Unit.sat).amount, unit)
            gross = int(round(amt.amount * (1 + self._melt_fee[unit] / 100)))

            return PaymentQuoteResponse(
                ok=ln_quote.ok,
                checking_id=ln_quote.checking_id,
                amount=Amount(unit, gross),
                fee=fee,
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
