"""ZcashBackend - Zcash onchain payment backend for Cashu mint.

Implements LightningBackend by communicating with zwalletd (a Zcash wallet
daemon) via REST API. Supports minting ZEC ecash from onchain deposits and
melting ZEC ecash to onchain sends.

Only supports Unit.zec. Cross-method fungible with ZEC tokens minted via
Lightning (they share the same keyset).
"""

import asyncio
from typing import AsyncGenerator, Optional

import httpx
from loguru import logger

from ..core.base import Amount, MeltQuote, Unit
from ..core.models import PostMeltQuoteRequest
from ..core.settings import settings
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

ZATOSHI_PER_ZEC = 100_000_000


class ZcashBackend(LightningBackend):
    """Zcash onchain backend via zwalletd REST API.

    Registered as backends[Method.zcash][Unit.zec] in the mint's backend dict.
    """

    supports_mpp = False
    supports_incoming_payment_stream = True
    supports_description = False

    def __init__(self, unit: Unit, **kwargs):
        if unit.name != "zec":
            raise Unsupported("ZcashBackend only supports Unit.zec")
        self.unit = unit
        self.supported_units = {unit}
        self._zwalletd_url = settings.mint_zcash_zwalletd_url.rstrip("/")
        self._zwalletd_secret = settings.mint_zcash_zwalletd_secret
        self._headers = {}
        if self._zwalletd_secret:
            self._headers["Authorization"] = f"Bearer {self._zwalletd_secret}"
        self._min_confirmations = settings.mint_zcash_min_confirmations
        self._quote_expiry = settings.mint_zcash_quote_expiry

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """GET request to zwalletd."""
        url = f"{self._zwalletd_url}{path}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url, headers=self._headers, params=params, timeout=30.0
            )
            resp.raise_for_status()
            return resp.json()

    async def _post(
        self,
        path: str,
        json_data: Optional[dict] = None,
        timeout: float = 30.0,
    ) -> dict:
        """POST request to zwalletd."""
        url = f"{self._zwalletd_url}{path}"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url, headers=self._headers, json=json_data, timeout=timeout
            )
            resp.raise_for_status()
            return resp.json()

    # ------------------------------------------------------------------
    # LightningBackend interface
    # ------------------------------------------------------------------

    async def status(self) -> StatusResponse:
        """Query zwalletd wallet status and balance."""
        try:
            data = await self._get("/api/v1/status")
            balance_zat = data.get("balance", {}).get("confirmed", 0)
            return StatusResponse(
                balance=Amount(self.unit, balance_zat),
                error_message=None,
            )
        except Exception as e:
            logger.error(f"ZcashBackend status error: {e}")
            return StatusResponse(
                balance=Amount(self.unit, 0),
                error_message=str(e),
            )

    async def create_invoice(
        self,
        amount: Amount,
        memo: Optional[str] = None,
        description_hash: Optional[bytes] = None,
        unhashed_description: Optional[bytes] = None,
        expiry: Optional[int] = None,
        payment_secret: Optional[bytes] = None,
    ) -> InvoiceResponse:
        """Generate a fresh Zcash address for receiving a deposit.

        For Zcash onchain, "creating an invoice" means generating a new unified
        address. The address is the payment_request, and the address_index is
        the checking_id (used to track deposit status later).
        """
        self.assert_unit_supported(amount.unit)
        try:
            data = await self._post("/api/v1/address/new")
            address = data["address"]
            address_index = str(data["address_index"])

            logger.info(
                f"ZcashBackend: created address (index={address_index}) "
                f"for {amount.amount} zat"
            )

            return InvoiceResponse(
                ok=True,
                checking_id=address_index,
                payment_request=address,
            )
        except Exception as e:
            logger.error(f"ZcashBackend create_invoice error: {e}")
            return InvoiceResponse(
                ok=False,
                error_message=str(e),
            )

    async def pay_invoice(
        self, quote: MeltQuote, fee_limit_msat: int
    ) -> PaymentResponse:
        """Send ZEC onchain to the address in the melt quote.

        For Zcash, fee_limit_msat is ignored (network fees are in zatoshi and
        are very low ~10000 zat). The fee was already reserved in the quote.
        """
        try:
            data = await self._post(
                "/api/v1/send",
                json_data={
                    "to_address": quote.request,
                    "amount": quote.amount,
                    "memo": f"cashu melt {quote.quote}",
                },
                timeout=300.0,
            )
            txid = data["txid"]
            fee_zat = data.get("fee", 0)

            logger.info(
                f"ZcashBackend: sent {quote.amount} zat to {quote.request}, "
                f"txid={txid}, fee={fee_zat}"
            )

            # Transaction is broadcast but not yet confirmed
            return PaymentResponse(
                result=PaymentResult.PENDING,
                checking_id=txid,
                fee=Amount(self.unit, fee_zat),
                preimage=txid,  # txid serves as proof of payment
            )
        except httpx.HTTPStatusError as e:
            response_data = {}
            try:
                response_data = e.response.json()
            except Exception:
                pass

            txid = response_data.get("txid")
            if txid:
                fee_zat = response_data.get("fee", 0)
                error_message = response_data.get("error") or str(e)
                logger.warning(
                    "ZcashBackend: send returned error after tx creation, "
                    f"using txid={txid} for status tracking: {error_message}"
                )
                return PaymentResponse(
                    result=PaymentResult.PENDING,
                    checking_id=txid,
                    fee=Amount(self.unit, fee_zat),
                    preimage=txid,
                    error_message=error_message,
                )
            logger.error(f"ZcashBackend pay_invoice error: {e}")
            return PaymentResponse(
                result=PaymentResult.FAILED,
                error_message=str(e),
            )
        except Exception as e:
            logger.error(f"ZcashBackend pay_invoice error: {e}")
            return PaymentResponse(
                result=PaymentResult.FAILED,
                error_message=str(e),
            )

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        """Check deposit status for a mint quote.

        checking_id is the address_index (as string).
        """
        try:
            data = await self._get(
                f"/api/v1/deposit/{checking_id}",
                params={"min_confirmations": self._min_confirmations},
            )
            total_confirmed = data.get("total_confirmed", 0)

            if total_confirmed > 0:
                return PaymentStatus(
                    result=PaymentResult.SETTLED,
                )
            else:
                return PaymentStatus(
                    result=PaymentResult.UNKNOWN,
                )
        except Exception as e:
            logger.error(f"ZcashBackend get_invoice_status error: {e}")
            return PaymentStatus(
                result=PaymentResult.UNKNOWN,
                error_message=str(e),
            )

    async def get_payment_status(self, checking_id: str) -> PaymentStatus:
        """Check send status for a melt quote.

        checking_id is the txid.
        """
        try:
            data = await self._get(f"/api/v1/send/{checking_id}/status")
            status_str = data.get("status", "unknown")
            fee_zat = data.get("fee", 0)

            if status_str == "confirmed":
                return PaymentStatus(
                    result=PaymentResult.SETTLED,
                    fee=Amount(self.unit, fee_zat),
                    preimage=checking_id,
                )
            elif status_str == "pending":
                return PaymentStatus(
                    result=PaymentResult.PENDING,
                )
            elif status_str == "failed" or status_str == "expired":
                return PaymentStatus(
                    result=PaymentResult.FAILED,
                )
            else:
                return PaymentStatus(
                    result=PaymentResult.UNKNOWN,
                )
        except Exception as e:
            logger.error(f"ZcashBackend get_payment_status error: {e}")
            return PaymentStatus(
                result=PaymentResult.UNKNOWN,
                error_message=str(e),
            )

    async def get_payment_quote(
        self, melt_quote: PostMeltQuoteRequest
    ) -> PaymentQuoteResponse:
        """Get a payment quote for melting (sending ZEC onchain).

        Unlike bolt11 where the amount is encoded in the invoice, Zcash
        addresses don't encode amounts. The amount must be provided in the
        melt quote request via the `amount` field.
        """
        if not melt_quote.amount:
            raise Exception(
                "Amount is required for zcash melt quotes "
                "(Zcash addresses do not encode amounts)"
            )

        amount_zat = melt_quote.amount

        # Estimate network fee from zwalletd, or use a reasonable default
        try:
            # TODO: Add a fee estimation endpoint to zwalletd
            # For now, use the standard Zcash network fee
            fee_zat = 10_000  # 0.0001 ZEC standard fee
        except Exception:
            fee_zat = 10_000

        return PaymentQuoteResponse(
            checking_id="pending",  # will be replaced with txid after send
            amount=Amount(self.unit, amount_zat),
            fee=Amount(self.unit, fee_zat),
        )

    async def paid_invoices_stream(self) -> AsyncGenerator[str, None]:
        """SSE stream from zwalletd for deposit notifications.

        Yields checking_ids (address_index as string) when deposits are
        confirmed with sufficient confirmations.
        """
        url = f"{self._zwalletd_url}/api/v1/deposits/stream"
        while True:
            try:
                async with httpx.AsyncClient(timeout=None) as client:
                    async with client.stream(
                        "GET", url, headers=self._headers
                    ) as response:
                        async for line in response.aiter_lines():
                            if not line or not line.startswith("data: "):
                                continue
                            try:
                                import json

                                event_data = json.loads(line[6:])
                                confirmations = event_data.get("confirmations", 0)
                                if confirmations >= self._min_confirmations:
                                    address_index = str(
                                        event_data.get("address_index", "")
                                    )
                                    if address_index:
                                        logger.info(
                                            f"ZcashBackend: deposit confirmed "
                                            f"at address_index={address_index}, "
                                            f"amount={event_data.get('amount', '?')} zat, "
                                            f"confirmations={confirmations}"
                                        )
                                        yield address_index
                            except Exception as e:
                                logger.warning(
                                    f"ZcashBackend: failed to parse SSE event: {e}"
                                )
            except Exception as e:
                logger.error(
                    f"ZcashBackend: SSE stream disconnected: {e}. "
                    "Reconnecting in 5 seconds..."
                )
                await asyncio.sleep(5)
