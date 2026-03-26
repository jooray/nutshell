"""Unit tests for ZcashBackend.

Tests the ZcashBackend lightning implementation using respx to mock
the zwalletd HTTP API responses. Follows the same pattern as
test_mint_lightning_blink.py.
"""

import pytest
import respx
from httpx import Response

from cashu.core.base import Amount, MeltQuote, MeltQuoteState, Unit
from cashu.core.models import PostMeltQuoteRequest
from cashu.core.settings import settings
from cashu.lightning.base import (
    PaymentResult,
    Unsupported,
)

# Configure settings before instantiating the backend
settings.mint_zcash_enabled = True
settings.mint_zcash_zwalletd_url = "http://localhost:3340"
settings.mint_zcash_zwalletd_secret = "test-secret-token"
settings.mint_zcash_min_confirmations = 2
settings.mint_zcash_quote_expiry = 86400
# Ensure "zec" is a valid unit
if "zec" not in (settings.mint_units or []):
    if not settings.mint_units:
        settings.mint_units = ["sat", "zec"]
    else:
        settings.mint_units.append("zec")

from cashu.lightning.zcash_backend import ZcashBackend  # noqa: E402

ZWALLETD_URL = "http://localhost:3340"
zec_unit = Unit("zec")
backend = ZcashBackend(unit=zec_unit)

# Sample Zcash unified address (truncated for tests)
SAMPLE_ADDRESS = "u1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzmt2e6t3dey5xw8gnzm0kfg0egpgz"


# ------------------------------------------------------------------
# Constructor tests
# ------------------------------------------------------------------


def test_zcash_backend_init():
    """ZcashBackend should initialize with Unit.zec."""
    assert backend.unit == zec_unit
    assert zec_unit in backend.supported_units
    assert backend.supports_mpp is False
    assert backend.supports_incoming_payment_stream is True
    assert backend.supports_description is False


def test_zcash_backend_rejects_non_zec_unit():
    """ZcashBackend should reject non-zec units."""
    with pytest.raises(Unsupported):
        ZcashBackend(unit=Unit.sat)


def test_zcash_backend_auth_header():
    """ZcashBackend should include bearer token in headers."""
    assert "Authorization" in backend._headers
    assert backend._headers["Authorization"] == "Bearer test-secret-token"


def test_zcash_backend_no_auth_header():
    """ZcashBackend without secret should have no auth header."""
    old_secret = settings.mint_zcash_zwalletd_secret
    settings.mint_zcash_zwalletd_secret = None
    try:
        b = ZcashBackend(unit=zec_unit)
        assert "Authorization" not in b._headers
    finally:
        settings.mint_zcash_zwalletd_secret = old_secret


# ------------------------------------------------------------------
# status()
# ------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_zcash_status_success():
    """status() should return balance from zwalletd."""
    mock_response = {
        "balance": {
            "confirmed": 500000,
            "unconfirmed": 100000,
            "pending_change": 0,
        },
        "synced": True,
        "chain_tip": 2000000,
    }
    respx.get(f"{ZWALLETD_URL}/api/v1/status").mock(
        return_value=Response(200, json=mock_response)
    )
    status = await backend.status()
    assert status.error_message is None
    assert status.balance == Amount(zec_unit, 500000)


@respx.mock
@pytest.mark.asyncio
async def test_zcash_status_error():
    """status() should handle errors gracefully."""
    respx.get(f"{ZWALLETD_URL}/api/v1/status").mock(
        return_value=Response(500, json={"error": "Internal error"})
    )
    status = await backend.status()
    assert status.error_message is not None
    assert status.balance == Amount(zec_unit, 0)


# ------------------------------------------------------------------
# create_invoice()
# ------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_zcash_create_invoice_success():
    """create_invoice() should return address and address_index."""
    mock_response = {
        "address": SAMPLE_ADDRESS,
        "address_index": 42,
    }
    respx.post(f"{ZWALLETD_URL}/api/v1/address/new").mock(
        return_value=Response(200, json=mock_response)
    )
    invoice = await backend.create_invoice(Amount(zec_unit, 100000))
    assert invoice.ok is True
    assert invoice.payment_request == SAMPLE_ADDRESS
    assert invoice.checking_id == "42"
    assert invoice.error_message is None


@respx.mock
@pytest.mark.asyncio
async def test_zcash_create_invoice_error():
    """create_invoice() should handle errors gracefully."""
    respx.post(f"{ZWALLETD_URL}/api/v1/address/new").mock(
        return_value=Response(503, json={"error": "Wallet not initialized"})
    )
    invoice = await backend.create_invoice(Amount(zec_unit, 100000))
    assert invoice.ok is False
    assert invoice.error_message is not None


# ------------------------------------------------------------------
# pay_invoice()
# ------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_zcash_pay_invoice_success():
    """pay_invoice() should return pending status with txid."""
    mock_response = {
        "txid": "abc123def456",
        "status": "pending",
        "fee": 10000,
    }
    respx.post(f"{ZWALLETD_URL}/api/v1/send").mock(
        return_value=Response(200, json=mock_response)
    )
    quote = MeltQuote(
        request=SAMPLE_ADDRESS,
        quote="test-quote-id",
        method="zcash",
        checking_id="pending",
        unit="zec",
        amount=500000,
        fee_reserve=10000,
        state=MeltQuoteState.unpaid,
    )
    payment = await backend.pay_invoice(quote, fee_limit_msat=0)
    assert payment.result == PaymentResult.PENDING
    assert payment.checking_id == "abc123def456"
    assert payment.fee == Amount(zec_unit, 10000)
    assert payment.preimage == "abc123def456"  # txid is the proof
    assert payment.error_message is None


@respx.mock
@pytest.mark.asyncio
async def test_zcash_pay_invoice_failure():
    """pay_invoice() should handle send failures."""
    respx.post(f"{ZWALLETD_URL}/api/v1/send").mock(
        return_value=Response(400, json={"error": "Insufficient funds"})
    )
    quote = MeltQuote(
        request=SAMPLE_ADDRESS,
        quote="test-quote-id",
        method="zcash",
        checking_id="pending",
        unit="zec",
        amount=500000,
        fee_reserve=10000,
        state=MeltQuoteState.unpaid,
    )
    payment = await backend.pay_invoice(quote, fee_limit_msat=0)
    assert payment.result == PaymentResult.FAILED
    assert payment.error_message is not None


# ------------------------------------------------------------------
# get_invoice_status()
# ------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_zcash_get_invoice_status_settled():
    """get_invoice_status() should return SETTLED when confirmed deposits exist."""
    mock_response = {
        "deposits": [
            {
                "txid": "abc123",
                "amount": 100000,
                "confirmations": 5,
                "block_height": 1999995,
                "from_pool": "orchard",
            }
        ],
        "total_confirmed": 100000,
        "total_unconfirmed": 0,
    }
    respx.get(f"{ZWALLETD_URL}/api/v1/deposit/7").mock(
        return_value=Response(200, json=mock_response)
    )
    status = await backend.get_invoice_status("7")
    assert status.result == PaymentResult.SETTLED


@respx.mock
@pytest.mark.asyncio
async def test_zcash_get_invoice_status_pending():
    """get_invoice_status() should return UNKNOWN when no confirmed deposits."""
    mock_response = {
        "deposits": [],
        "total_confirmed": 0,
        "total_unconfirmed": 50000,
    }
    respx.get(f"{ZWALLETD_URL}/api/v1/deposit/7").mock(
        return_value=Response(200, json=mock_response)
    )
    status = await backend.get_invoice_status("7")
    assert status.result == PaymentResult.UNKNOWN


@respx.mock
@pytest.mark.asyncio
async def test_zcash_get_invoice_status_error():
    """get_invoice_status() should handle errors gracefully."""
    respx.get(f"{ZWALLETD_URL}/api/v1/deposit/99").mock(
        return_value=Response(404, json={"error": "Address not found"})
    )
    status = await backend.get_invoice_status("99")
    assert status.result == PaymentResult.UNKNOWN
    assert status.error_message is not None


# ------------------------------------------------------------------
# get_payment_status()
# ------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_zcash_get_payment_status_confirmed():
    """get_payment_status() should return SETTLED when tx is confirmed."""
    mock_response = {
        "txid": "abc123def456",
        "status": "confirmed",
        "confirmations": 10,
        "block_height": 1999990,
        "fee": 10000,
    }
    respx.get(f"{ZWALLETD_URL}/api/v1/send/abc123def456/status").mock(
        return_value=Response(200, json=mock_response)
    )
    status = await backend.get_payment_status("abc123def456")
    assert status.result == PaymentResult.SETTLED
    assert status.fee == Amount(zec_unit, 10000)
    assert status.preimage == "abc123def456"


@respx.mock
@pytest.mark.asyncio
async def test_zcash_get_payment_status_pending():
    """get_payment_status() should return PENDING when tx is unconfirmed."""
    mock_response = {
        "txid": "abc123def456",
        "status": "pending",
        "confirmations": 0,
        "block_height": None,
        "fee": 10000,
    }
    respx.get(f"{ZWALLETD_URL}/api/v1/send/abc123def456/status").mock(
        return_value=Response(200, json=mock_response)
    )
    status = await backend.get_payment_status("abc123def456")
    assert status.result == PaymentResult.PENDING


@respx.mock
@pytest.mark.asyncio
async def test_zcash_get_payment_status_failed():
    """get_payment_status() should return FAILED for failed/expired txs."""
    mock_response = {
        "txid": "abc123def456",
        "status": "failed",
        "confirmations": 0,
        "block_height": None,
        "fee": 0,
    }
    respx.get(f"{ZWALLETD_URL}/api/v1/send/abc123def456/status").mock(
        return_value=Response(200, json=mock_response)
    )
    status = await backend.get_payment_status("abc123def456")
    assert status.result == PaymentResult.FAILED


@respx.mock
@pytest.mark.asyncio
async def test_zcash_get_payment_status_unknown():
    """get_payment_status() should return UNKNOWN for unrecognized status."""
    mock_response = {
        "txid": "abc123def456",
        "status": "something_else",
        "confirmations": 0,
        "block_height": None,
        "fee": 0,
    }
    respx.get(f"{ZWALLETD_URL}/api/v1/send/abc123def456/status").mock(
        return_value=Response(200, json=mock_response)
    )
    status = await backend.get_payment_status("abc123def456")
    assert status.result == PaymentResult.UNKNOWN


@respx.mock
@pytest.mark.asyncio
async def test_zcash_get_payment_status_error():
    """get_payment_status() should handle HTTP errors gracefully."""
    respx.get(f"{ZWALLETD_URL}/api/v1/send/nonexistent/status").mock(
        return_value=Response(404, json={"error": "Transaction not found"})
    )
    status = await backend.get_payment_status("nonexistent")
    assert status.result == PaymentResult.UNKNOWN
    assert status.error_message is not None


# ------------------------------------------------------------------
# get_payment_quote()
# ------------------------------------------------------------------


def test_zcash_get_payment_quote_requires_amount():
    """get_payment_quote() should raise when amount is missing."""
    melt_quote = PostMeltQuoteRequest(
        unit="zec",
        request=SAMPLE_ADDRESS,
        # no amount field
    )
    # amount defaults to None, which should cause an error
    import asyncio

    with pytest.raises(Exception, match="Amount is required"):
        asyncio.get_event_loop().run_until_complete(
            backend.get_payment_quote(melt_quote)
        )


@pytest.mark.asyncio
async def test_zcash_get_payment_quote_success():
    """get_payment_quote() should return amount and fee for valid request."""
    melt_quote = PostMeltQuoteRequest(
        unit="zec",
        request=SAMPLE_ADDRESS,
        amount=500000,
    )
    quote = await backend.get_payment_quote(melt_quote)
    assert quote.amount == Amount(zec_unit, 500000)
    assert quote.fee == Amount(zec_unit, 10000)  # standard fee
    assert quote.checking_id == "pending"


@pytest.mark.asyncio
async def test_zcash_get_payment_quote_small_amount():
    """get_payment_quote() should work with small amounts."""
    melt_quote = PostMeltQuoteRequest(
        unit="zec",
        request=SAMPLE_ADDRESS,
        amount=1000,
    )
    quote = await backend.get_payment_quote(melt_quote)
    assert quote.amount == Amount(zec_unit, 1000)
    assert quote.fee == Amount(zec_unit, 10000)


# ------------------------------------------------------------------
# Integration-style test: full mint flow
# ------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_zcash_mint_flow():
    """Test the full mint flow: create_invoice -> get_invoice_status."""
    # Step 1: Create an address (invoice)
    respx.post(f"{ZWALLETD_URL}/api/v1/address/new").mock(
        return_value=Response(
            200,
            json={"address": SAMPLE_ADDRESS, "address_index": 5},
        )
    )
    invoice = await backend.create_invoice(Amount(zec_unit, 200000))
    assert invoice.ok is True
    checking_id = invoice.checking_id

    # Step 2: Check status - not yet paid
    respx.get(f"{ZWALLETD_URL}/api/v1/deposit/{checking_id}").mock(
        return_value=Response(
            200,
            json={"deposits": [], "total_confirmed": 0, "total_unconfirmed": 0},
        )
    )
    status = await backend.get_invoice_status(checking_id)
    assert status.result == PaymentResult.UNKNOWN

    # Step 3: Deposit arrives and is confirmed
    respx.get(f"{ZWALLETD_URL}/api/v1/deposit/{checking_id}").mock(
        return_value=Response(
            200,
            json={
                "deposits": [
                    {
                        "txid": "deposit_tx_123",
                        "amount": 200000,
                        "confirmations": 3,
                        "block_height": 1999997,
                        "from_pool": "orchard",
                    }
                ],
                "total_confirmed": 200000,
                "total_unconfirmed": 0,
            },
        )
    )
    status = await backend.get_invoice_status(checking_id)
    assert status.result == PaymentResult.SETTLED


@respx.mock
@pytest.mark.asyncio
async def test_zcash_melt_flow():
    """Test the full melt flow: get_payment_quote -> pay_invoice -> get_payment_status."""
    # Step 1: Get a payment quote
    melt_quote_req = PostMeltQuoteRequest(
        unit="zec",
        request=SAMPLE_ADDRESS,
        amount=300000,
    )
    quote_resp = await backend.get_payment_quote(melt_quote_req)
    assert quote_resp.amount == Amount(zec_unit, 300000)
    assert quote_resp.fee == Amount(zec_unit, 10000)

    # Step 2: Pay the invoice
    respx.post(f"{ZWALLETD_URL}/api/v1/send").mock(
        return_value=Response(
            200,
            json={"txid": "melt_tx_456", "status": "pending", "fee": 10000},
        )
    )
    quote = MeltQuote(
        request=SAMPLE_ADDRESS,
        quote="melt-quote-id",
        method="zcash",
        checking_id="pending",
        unit="zec",
        amount=300000,
        fee_reserve=10000,
        state=MeltQuoteState.unpaid,
    )
    payment = await backend.pay_invoice(quote, fee_limit_msat=0)
    assert payment.result == PaymentResult.PENDING
    assert payment.checking_id == "melt_tx_456"

    # Step 3: Check status - confirmed
    respx.get(f"{ZWALLETD_URL}/api/v1/send/melt_tx_456/status").mock(
        return_value=Response(
            200,
            json={
                "txid": "melt_tx_456",
                "status": "confirmed",
                "confirmations": 5,
                "block_height": 1999995,
                "fee": 10000,
            },
        )
    )
    status = await backend.get_payment_status("melt_tx_456")
    assert status.result == PaymentResult.SETTLED
    assert status.fee == Amount(zec_unit, 10000)
