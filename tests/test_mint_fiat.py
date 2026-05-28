"""Tests for UnitsBackend - the wrapper that adds fiat currency support via unitsd."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from cashu.core.base import Unit, Amount, MeltQuote, MeltQuoteState
from cashu.core.models import PostMeltQuoteRequest
from cashu.lightning.unitsbackend import UnitsBackend
from cashu.lightning.base import (
    InvoiceResponse,
    PaymentQuoteResponse,
    PaymentResponse,
    PaymentResult,
    PaymentStatus,
    StatusResponse,
)


# ─── Mock Lightning Backend ──────────────────────────────────────────────────


class MockLightningBackend:
    """Minimal mock lightning backend for testing UnitsBackend wrapper."""

    supported_units = {Unit.sat}
    supports_mpp = False
    supports_incoming_payment_stream = False
    supports_description = True
    unit = Unit.sat

    async def status(self) -> StatusResponse:
        return StatusResponse(balance=Amount(Unit.sat, 1_000_000))

    async def create_invoice(self, amount: Amount, **kwargs) -> InvoiceResponse:
        return InvoiceResponse(
            ok=True,
            checking_id="test_checking_id_123",
            payment_request="lnbc14230n1test...",
        )

    async def get_payment_quote(
        self, melt_quote: PostMeltQuoteRequest
    ) -> PaymentQuoteResponse:
        return PaymentQuoteResponse(
            checking_id="test_melt_checking_id",
            amount=Amount(Unit.sat, 1000),
            fee=Amount(Unit.sat, 10),
        )

    async def pay_invoice(
        self, quote: MeltQuote, fee_limit_msat: int, **kwargs
    ) -> PaymentResponse:
        return PaymentResponse(
            result=PaymentResult.SETTLED,
            checking_id="test_pay_id",
            fee=Amount(Unit.msat, 1000),
            preimage="0" * 64,
        )

    async def get_invoice_status(self, checking_id: str) -> PaymentStatus:
        return PaymentStatus(result=PaymentResult.SETTLED)

    async def get_payment_status(self, checking_id: str) -> PaymentStatus:
        return PaymentStatus(result=PaymentResult.SETTLED)

    async def paid_invoices_stream(self):
        return
        yield  # make it an async generator


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_settings():
    """Patch settings to provide unitsd configuration."""
    with patch("cashu.lightning.unitsbackend.settings") as mock:
        mock.unitsd_url = "http://localhost:3339"
        mock.unitsd_api_secret = "test-api-secret"
        yield mock


@pytest.fixture
def mock_backend():
    return MockLightningBackend()


@pytest.fixture
def units_backend(mock_settings, mock_backend):
    """Create a UnitsBackend with mocked settings."""
    return UnitsBackend(mock_backend)


# ─── Helper: mock _call_unitsd ───────────────────────────────────────────────


def make_unitsd_mock(responses: dict) -> AsyncMock:
    """Create a mock for _call_unitsd that returns different responses per endpoint.

    Args:
        responses: dict mapping endpoint paths to return values.
    """

    async def side_effect(endpoint, params=None, json=None):
        return responses.get(endpoint, {})

    return AsyncMock(side_effect=side_effect)


# ─── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_init_requires_sat_backend(mock_settings):
    """UnitsBackend requires the wrapped backend to support sat."""

    class NoSatBackend(MockLightningBackend):
        supported_units = {Unit.msat}

    with pytest.raises(Exception, match="must support 'sat'"):
        UnitsBackend(NoSatBackend())


@pytest.mark.asyncio
async def test_init_requires_unitsd_url(mock_backend):
    """UnitsBackend raises if UNITSD_URL is not set."""
    with patch("cashu.lightning.unitsbackend.settings") as mock:
        mock.unitsd_url = ""
        mock.unitsd_api_secret = "secret"
        with pytest.raises(Exception, match="UNITSD_URL"):
            UnitsBackend(mock_backend)


@pytest.mark.asyncio
async def test_init_requires_unitsd_secret(mock_backend):
    """UnitsBackend raises if UNITSD_API_SECRET is not set."""
    with patch("cashu.lightning.unitsbackend.settings") as mock:
        mock.unitsd_url = "http://localhost:3339"
        mock.unitsd_api_secret = None
        with pytest.raises(Exception, match="UNITSD_API_SECRET"):
            UnitsBackend(mock_backend)


@pytest.mark.asyncio
async def test_fetch_units_from_unitsd(units_backend):
    """fetch_units_from_unitsd populates supported_units and fiat_units."""
    Unit.init_custom_units()

    units_backend._call_unitsd = make_unitsd_mock(
        {
            "/api/v1/units": {
                "units": [
                    {
                        "code": "usd",
                        "name": "US Dollar",
                        "decimals": 2,
                        "mint_fee_percent": 1.0,
                        "melt_fee_percent": 1.0,
                        "path_index": 2,
                        "derivation_path": "m/0'/0'/2'",
                    },
                    {
                        "code": "eur",
                        "name": "Euro",
                        "decimals": 2,
                        "mint_fee_percent": 1.0,
                        "melt_fee_percent": 1.0,
                        "path_index": 3,
                        "derivation_path": "m/0'/0'/3'",
                    },
                ]
            }
        }
    )

    result = await units_backend.fetch_units_from_unitsd()

    assert len(result) == 2
    assert Unit.usd in units_backend.supported_units
    assert Unit.eur in units_backend.supported_units
    assert Unit.sat in units_backend.supported_units
    assert Unit.usd in units_backend._fiat_units
    assert Unit.eur in units_backend._fiat_units


@pytest.mark.asyncio
async def test_get_derivation_paths(units_backend):
    """get_derivation_paths returns paths after fetch."""
    units_backend._unitsd_units = [
        {"code": "usd", "derivation_path": "m/0'/0'/2'"},
        {"code": "eur", "derivation_path": "m/0'/0'/3'"},
    ]

    paths = units_backend.get_derivation_paths()
    assert paths == ["m/0'/0'/2'", "m/0'/0'/3'"]


@pytest.mark.asyncio
async def test_create_invoice_sat_passthrough(units_backend):
    """Sat invoices pass directly through to the wrapped backend."""
    amount = Amount(Unit.sat, 1000)
    resp = await units_backend.create_invoice(amount, memo="test")

    assert resp.ok
    assert resp.checking_id == "test_checking_id_123"


@pytest.mark.asyncio
async def test_create_invoice_fiat(units_backend):
    """Fiat invoices query unitsd for conversion and create a sat invoice, but
    DEFER the hedging callback until the invoice is paid (it is only cached)."""
    units_backend._fiat_units = {Unit.usd}
    units_backend.supported_units = {Unit.sat, Unit.usd}

    units_backend._call_unitsd = make_unitsd_mock(
        {
            "/api/v1/quote/mint": {
                "amount_fiat": 101,
                "amount_msat": 1_423_237,
                "unit": "usd",
                "btc_price": 70965.0,
                "fee_percent": 1.0,
            },
        }
    )

    amount = Amount(Unit.usd, 100)  # 100 USD cents = $1.00
    resp = await units_backend.create_invoice(amount, memo="test fiat")

    assert resp.ok
    assert resp.checking_id == "test_checking_id_123"

    # Only the mint quote is fetched at creation time; NO callback yet.
    calls = units_backend._call_unitsd.call_args_list
    assert len(calls) == 1
    assert calls[0].args[0] == "/api/v1/quote/mint"
    assert calls[0].kwargs["params"] == {"amount": 100, "unit": "usd"}

    # The conversion details are cached, keyed by checking_id, for the callback
    # that fires once the invoice is observed as paid.
    pending = units_backend._pending_mints["test_checking_id_123"]
    assert pending["unit"] == "usd"
    assert pending["amount"] == 100
    assert pending["msat_amount"] == 1_423_237
    assert pending["btc_price"] == 70965.0


@pytest.mark.asyncio
async def test_mint_callback_fires_once_on_settlement(units_backend):
    """The mint hedging callback is sent when the deposit invoice settles, and
    only once even if settlement is observed multiple times."""
    units_backend._fiat_units = {Unit.usd}
    units_backend.supported_units = {Unit.sat, Unit.usd}

    units_backend._call_unitsd = make_unitsd_mock(
        {
            "/api/v1/quote/mint": {
                "amount_fiat": 101,
                "amount_msat": 1_423_237,
                "unit": "usd",
                "btc_price": 70965.0,
                "fee_percent": 1.0,
            },
            "/api/v1/callback/mint": {"status": "ok"},
        }
    )

    amount = Amount(Unit.usd, 100)
    await units_backend.create_invoice(amount, memo="test fiat")

    # First settlement observation: callback fires.
    status = await units_backend.get_invoice_status("test_checking_id_123")
    assert status.result == PaymentResult.SETTLED

    callback_calls = [
        c
        for c in units_backend._call_unitsd.call_args_list
        if c.args[0] == "/api/v1/callback/mint"
    ]
    assert len(callback_calls) == 1
    callback_json = callback_calls[0].kwargs["json"]
    assert callback_json["quote_id"] == "test_checking_id_123"
    assert callback_json["unit"] == "usd"
    assert callback_json["amount"] == 100
    assert callback_json["msat_amount"] == 1_423_237
    assert callback_json["btc_price"] == 70965.0

    # Second observation (e.g. poller + stream): no duplicate callback.
    await units_backend.get_invoice_status("test_checking_id_123")
    callback_calls = [
        c
        for c in units_backend._call_unitsd.call_args_list
        if c.args[0] == "/api/v1/callback/mint"
    ]
    assert len(callback_calls) == 1
    assert "test_checking_id_123" not in units_backend._pending_mints


@pytest.mark.asyncio
async def test_create_invoice_fiat_unitsd_error(units_backend):
    """If unitsd fails during mint quote, invoice creation fails gracefully."""
    units_backend._fiat_units = {Unit.usd}
    units_backend.supported_units = {Unit.sat, Unit.usd}

    units_backend._call_unitsd = AsyncMock(
        side_effect=RuntimeError("unitsd unreachable")
    )

    amount = Amount(Unit.usd, 100)
    resp = await units_backend.create_invoice(amount, memo="should fail")

    assert not resp.ok
    assert "Failed to create invoice" in resp.error_message


@pytest.mark.asyncio
async def test_get_payment_quote_sat_passthrough(units_backend):
    """Sat melt quotes pass directly through to the wrapped backend."""
    quote = PostMeltQuoteRequest(request="lnbc1000n1test...", unit="sat")
    resp = await units_backend.get_payment_quote(quote)

    assert resp.checking_id == "test_melt_checking_id"
    assert resp.amount.unit == Unit.sat


@pytest.mark.asyncio
async def test_get_payment_quote_fiat(units_backend):
    """Fiat melt quotes query unitsd for fiat conversion."""
    units_backend._fiat_units = {Unit.eur}
    units_backend.supported_units = {Unit.sat, Unit.eur}

    units_backend._call_unitsd = make_unitsd_mock(
        {
            "/api/v1/quote/melt": {
                "amount_fiat": 42,
                "invoice_amount_msat": 50000,
                "unit": "eur",
                "btc_price": 65000.0,
                "fee_percent": 1.0,
            },
        }
    )

    quote = PostMeltQuoteRequest(request="lnbc1000n1test...", unit="eur")
    resp = await units_backend.get_payment_quote(quote)

    assert resp.checking_id == "test_melt_checking_id"
    assert resp.amount.unit == Unit.eur
    assert resp.amount.amount == 42
    assert resp.fee.unit == Unit.eur
    assert resp.fee.amount == 0  # fee included in amount

    # Verify the unitsd call had correct params
    call = units_backend._call_unitsd.call_args_list[0]
    assert call.args[0] == "/api/v1/quote/melt"
    params = call.kwargs["params"]
    assert params["unit"] == "eur"
    assert "invoice_msat" in params
    assert "ln_fee_msat" in params


@pytest.mark.asyncio
async def test_status_passthrough(units_backend):
    """Status passes through to the wrapped backend."""
    resp = await units_backend.status()
    assert resp.balance.amount == 1_000_000
    assert resp.balance.unit == Unit.sat


@pytest.mark.asyncio
async def test_get_invoice_status_passthrough(units_backend):
    """Invoice status passes through to the wrapped backend."""
    resp = await units_backend.get_invoice_status("test_id")
    assert resp.result == PaymentResult.SETTLED


@pytest.mark.asyncio
async def test_get_payment_status_passthrough(units_backend):
    """Payment status passes through to the wrapped backend."""
    resp = await units_backend.get_payment_status("test_id")
    assert resp.result == PaymentResult.SETTLED
