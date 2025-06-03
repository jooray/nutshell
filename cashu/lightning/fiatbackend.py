from __future__ import annotations

import asyncio
import time
from typing import AsyncGenerator, Dict, Optional

import httpx
from loguru import logger

from cashu.core.base import Amount, MeltQuote, Unit
from cashu.core.models import PostMeltQuoteRequest
from cashu.core.settings import env, FIAT_BACKEND_UNIT_CODES
from cashu.helpers.units import UNIT_SET, DECIMALS
from .base import (
    InvoiceResponse,
    LightningBackend,
    PaymentQuoteResponse,
    PaymentResponse,
    PaymentResult,
    PaymentStatus,
    StatusResponse,
    Unsupported,
)


class FiatBackend(LightningBackend):
    """
    Wrap `LightningBackend` so the mint can speak any fiat / alt-unit that is
    listed in ``FIAT_BACKEND_UNITS``.

    Per-unit fee knobs (*percent*):

        FIAT_BACKEND_MINT_FEE_<CODE>
        FIAT_BACKEND_MELT_FEE_<CODE>
    """

    supported_units: set[Unit]

    def __init__(
        self,
        lightning_backend: LightningBackend,
        *,
        cache_seconds: int = 300,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        if Unit.sat not in lightning_backend.supported_units:
            raise Unsupported("wrapped backend must at least support sat")

        self.ln = lightning_backend
        self._client = http_client or httpx.AsyncClient(timeout=10)
        self._cache_seconds = cache_seconds

        # ─── Which units does THIS backend cover? ───────────────────────
        fiat_units = {
            getattr(Unit, c.lower())
            for c in FIAT_BACKEND_UNIT_CODES
            if getattr(Unit, c.lower(), None) is not None
        }
        if not fiat_units:
            raise Unsupported("FIAT_BACKEND_UNITS empty or unknown codes")

        self._fiat_units = fiat_units
        self.supported_units = self.ln.supported_units.union(self._fiat_units)

        # ─── Precision & fee tables ─────────────────────────────────────
        self._decimals = {u: DECIMALS[u] for u in self._fiat_units}

        self._mint_fee = {
            u: env.float(f"FIAT_BACKEND_MINT_FEE_{u.name.upper()}", default=0.0)
            for u in self._fiat_units
        }
        self._melt_fee = {
            u: env.float(f"FIAT_BACKEND_MELT_FEE_{u.name.upper()}", default=0.0)
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
                        self._sat_per_unit[u] = 1e8 / float(v)
            except Exception as exc:
                logger.error(f"FX fetch failed: {exc}")

            if Unit.usd not in self._sat_per_unit:
                raise RuntimeError("USD/BTC quote missing – abort fiat backend start")

            self._rates_ts = time.time()

    async def _fiat_to_sat(self, amount: Amount) -> int:
        await self._ensure_rates()
        sat_per_unit = self._sat_per_unit[amount.unit]
        return int(round((amount.amount / 10**self._decimals[amount.unit]) * sat_per_unit))

    async def _sat_to_fiat(self, sat: int, unit: Unit) -> Amount:
        await self._ensure_rates()
        sat_per_unit = self._sat_per_unit[unit]
        sub = int(round((sat / sat_per_unit) * 10**self._decimals[unit]))
        return Amount(unit, sub)

    # ─────────────────── LightningBackend interface ─────────────────────
    async def status(self) -> StatusResponse:  # pragma: no cover
        return await self.ln.status()

    async def create_invoice(
        self,
        amount: Amount,
        memo: str | None = None,
        description_hash: bytes | None = None,
        unhashed_description: str | None = None,
        **kwargs,
    ) -> InvoiceResponse:
        if amount.unit in self._fiat_units:
            gross = int(round(amount.amount * (1 + self._mint_fee[amount.unit] / 100)))
            sats = await self._fiat_to_sat(Amount(amount.unit, gross))
            resp = await self.ln.create_invoice(
                Amount(Unit.sat, sats),
                memo=memo,
                description_hash=description_hash,
                unhashed_description=unhashed_description,
                **kwargs,
            )
            if resp.ok:
                self._minted[amount.unit] += amount.amount
            return resp

        return await self.ln.create_invoice(
            amount,
            memo=memo,
            description_hash=description_hash,
            unhashed_description=unhashed_description,
            **kwargs,
        )

    async def pay_invoice(self, quote: MeltQuote, fee_limit_msat: int, **kwargs) -> PaymentResponse:
        return await self.ln.pay_invoice(quote, fee_limit_msat, **kwargs)

    async def pay_invoice_with_quote(self, quote: MeltQuote, **kwargs) -> PaymentResult:
        resp = await self.ln.pay_invoice_with_quote(quote, **kwargs)
        if resp.settled and quote.unit in self._fiat_units:
            self._melted[quote.unit] += quote.amount.amount
        return resp

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        return await self.ln.get_invoice_status(checking_id)

    async def get_payment_status(self, checking_id: str) -> PaymentStatus:
        return await self.ln.get_payment_status(checking_id)

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        async for p in self.ln.paid_invoices_stream():
            yield p

    async def get_payment_quote(self, melt_quote: PostMeltQuoteRequest) -> PaymentQuoteResponse:
        ln_quote = await self.ln.get_payment_quote(melt_quote)
        unit = getattr(melt_quote, "unit", Unit.sat)

        if unit not in self._fiat_units:
            return ln_quote

        amt = await self._sat_to_fiat(ln_quote.amount.to(Unit.sat).amount, unit)
        fee = await self._sat_to_fiat(ln_quote.fee.to(Unit.sat).amount, unit)
        gross = int(round(amt.amount * (1 + self._melt_fee[unit] / 100)))

        return PaymentQuoteResponse(
            ok=ln_quote.ok,
            unit=unit,
            amount=Amount(unit, gross),
            fee=fee,
        )

    # ───────────────────────── introspection ────────────────────────────
    @property
    def minted_totals(self) -> Dict[Unit, int]:
        return self._minted.copy()

    @property
    def melted_totals(self) -> Dict[Unit, int]:
        return self._melted.copy()
