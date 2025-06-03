import pytest
from unittest.mock import AsyncMock, patch
from cashu.core.base import Unit, Amount
from cashu.core.models import PostMeltQuoteRequest
from cashu.lightning.fiatbackend import FiatBackend
from cashu.lightning.base import (
    LightningBackend,
    InvoiceResponse,
    PaymentQuoteResponse,
    StatusResponse
)


class MockLightningBackend(LightningBackend):
    """Mock lightning backend for testing"""

    supported_units = {Unit.sat}

    def __init__(self, unit: Unit):
        self.unit = unit

    async def status(self) -> StatusResponse:
        return StatusResponse(error_message=None, balance=Amount(Unit.sat, 1000000))

    async def create_invoice(self, amount: Amount, **kwargs) -> InvoiceResponse:
        return InvoiceResponse(
            ok=True,
            checking_id="test_checking_id",
            payment_request="lnbc1000n1..."
        )

    async def get_payment_quote(self, melt_quote: PostMeltQuoteRequest) -> PaymentQuoteResponse:
        return PaymentQuoteResponse(
            ok=True,
            checking_id="test_checking_id",
            amount=Amount(Unit.sat, 1000),
            fee=Amount(Unit.sat, 10)
        )


@pytest.fixture
def mock_settings():
    with patch('cashu.lightning.fiatbackend.settings') as mock:
        mock.mint_fiat_backend_units = ['USD', 'EUR', 'CZK']
        mock.fiat_backend_mint_fee = {'usd': 1.0, 'eur': 1.0, 'czk': 0.8}
        mock.fiat_backend_melt_fee = {'usd': 1.0, 'eur': 1.0, 'czk': 0.8}
        yield mock


@pytest.mark.asyncio
async def test_fiat_backend_init(mock_settings):
    """Test FiatBackend initialization"""
    mock_backend = MockLightningBackend(Unit.sat)

    fiat_backend = FiatBackend(mock_backend)

    assert {Unit.sat, Unit.usd, Unit.eur}.issubset(fiat_backend.supported_units)
    assert fiat_backend._mint_fee[Unit.usd] == 1.0
    assert fiat_backend._mint_fee[Unit.eur] == 1.0
    assert fiat_backend._melt_fee[Unit.usd] == 1.0
    assert fiat_backend._melt_fee[Unit.eur] == 1.0


@pytest.mark.asyncio
async def test_fiat_create_invoice(mock_settings):
    """Test creating invoice with fiat amount"""
    mock_backend = MockLightningBackend(Unit.sat)

    fiat_backend = FiatBackend(mock_backend)

    # Mock exchange rate: 1 USD = 50000 sats
    fiat_backend._sat_per_unit = {Unit.usd: 50000}
    fiat_backend._rates_ts = 1000000

    # Create invoice for $10
    amount = Amount(Unit.usd, 1000)  # $10.00

    with patch.object(fiat_backend, '_ensure_rates', new_callable=AsyncMock):
        response = await fiat_backend.create_invoice(amount, memo="Test invoice")

    assert response.ok
    assert response.checking_id == "test_checking_id"

    # Check that fee was applied: $10 + 1% = $10.10 = 505000 sats
    # But we need to spy on the actual call to verify
