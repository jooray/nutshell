import pytest
from unittest.mock import AsyncMock, MagicMock

from cashu.core.base import Amount, Unit
from cashu.core.db import Database
from cashu.lightning.fiatbackend import FiatBackend
from cashu.lightning.base import InvoiceResponse, PaymentResponse, PaymentResult
from cashu.mint.crud import get_fiat_accounting_summary, store_fiat_accounting_entry


@pytest.mark.asyncio
async def test_store_and_retrieve_accounting_entry():
    """Test storing and retrieving fiat accounting entries"""
    db = Database("test", ":memory:")

    # Run migration
    from cashu.mint import migrations
    await migrations.m028_add_fiat_accounting_table(db)

    # Store entry
    await store_fiat_accounting_entry(
        db=db,
        unit="usd",
        amount=10000,  # $100.00
        operation="mint",
        exchange_rate=0.00002,  # 1 sat = $0.00002
        sat_amount=5000000,
        fee_percent=1.0,
        fee_amount=100,  # $1.00
    )

    # Retrieve summary
    summary = await get_fiat_accounting_summary(db)

    assert "usd" in summary
    assert summary["usd"]["minted"] == 10000
    assert summary["usd"]["mint_fees"] == 100
    assert summary["usd"]["mint_count"] == 1


@pytest.mark.asyncio
async def test_fiat_backend_records_mint_operation():
    """Test that FiatBackend records mint operations"""
    # Setup mocks
    mock_ln_backend = AsyncMock()
    mock_ln_backend.supported_units = {Unit.sat}
    mock_ln_backend.create_invoice.return_value = InvoiceResponse(
        ok=True,
        checking_id="test123",
        payment_request="lnbc...",
    )

    db = Database("test", ":memory:")
    from cashu.mint import migrations
    await migrations.m028_add_fiat_accounting_table(db)

    # Create FiatBackend with mocked exchange rate
    backend = FiatBackend(mock_ln_backend, db=db)
    backend._sat_per_unit = {Unit.usd: 0.00002}  # 1 sat = $0.00002
    backend._rates_ts = 9999999999  # Far future to skip rate fetch
    backend._fiat_units = {Unit.usd}
    backend._mint_fee = {Unit.usd: 1.0}  # 1% fee
    backend._decimals = {Unit.usd: 2}

    # Create invoice
    amount = Amount(Unit.usd, 10000)  # $100.00
    await backend.create_invoice(amount)

    # Check accounting
    summary = await get_fiat_accounting_summary(db)
    assert summary["usd"]["minted"] == 10000
    assert summary["usd"]["mint_fees"] == 100  # 1% of $100


@pytest.mark.asyncio
async def test_fiat_backend_records_melt_operation():
    """Test that FiatBackend records melt operations"""
    # Setup mocks
    mock_ln_backend = AsyncMock()
    mock_ln_backend.supported_units = {Unit.sat}
    mock_ln_backend.pay_invoice.return_value = PaymentResponse(
        result=PaymentResult.SETTLED,
        checking_id="test456",
        fee=Amount(Unit.msat, 1000),
        preimage="preimage123",
    )

    db = Database("test", ":memory:")
    from cashu.mint import migrations
    await migrations.m028_add_fiat_accounting_table(db)

    # Create FiatBackend
    backend = FiatBackend(mock_ln_backend, db=db)
    backend._sat_per_unit = {Unit.usd: 0.00002}
    backend._rates_ts = 9999999999
    backend._fiat_units = {Unit.usd}
    backend._melt_fee = {Unit.usd: 1.0}
    backend._decimals = {Unit.usd: 2}

    # Create mock quote
    from cashu.core.base import MeltQuote
    quote = MeltQuote(
        quote="test_quote",
        method="bolt11",
        request="lnbc...",
        checking_id="test456",
        unit=Unit.usd,
        amount=10000,  # $100.00
        fee_reserve=100,
        paid=False,
        state="UNPAID",
        created_time=0,
        paid_time=None,
        fee_paid=0,
        proof="",
        outputs="",
        expiry=None,
        change="",
    )

    # Pay invoice
    await backend.pay_invoice(quote, 2000000)  # fee limit in msat

    # Check accounting
    summary = await get_fiat_accounting_summary(db)
    assert summary["usd"]["melted"] == 10000
    assert summary["usd"]["melt_fees"] == 100
