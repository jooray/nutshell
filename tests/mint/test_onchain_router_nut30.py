"""NUT-30 (onchain payment method) wire-shape regression tests.

These cover the pure helpers and Pydantic models of the onchain router
without needing the live-server fixture. They lock in the fields that
settled when cashubtc/nuts#365 merged as NUT-30 on 2026-05-20:

* fee options are selected by an integer ``fee_index`` (not by the
  ``estimated_blocks`` value, which was the shape of an earlier draft),
* the melt-quote response carries ``selected_fee_index`` and a NUT-08
  ``change`` field.

Cross-checked against cdk ``MeltQuoteOnchainResponse``
(crates/cashu/src/nuts/nut30.rs) and cashu-ts ``NUT30.ts``.
"""

import pytest
from pydantic import ValidationError

from cashu.core.base import BlindedSignature, MeltQuote, MeltQuoteState
from cashu.core.models.onchain import (
    OnchainFeeOption,
    PostMeltOnchainRequest,
    PostMeltQuoteOnchainResponse,
)
from cashu.core.settings import settings

# "zec" must be a valid unit before importing the router (which pulls in the
# ledger via startup). Mirror the setup in test_mint_lightning_zcash.py.
settings.mint_zcash_min_confirmations = 2
if not settings.mint_units:
    settings.mint_units = ["sat", "zec"]
elif "zec" not in settings.mint_units:
    settings.mint_units.append("zec")

from cashu.mint import router_onchain  # noqa: E402


def _melt_quote(state: MeltQuoteState) -> MeltQuote:
    return MeltQuote(
        quote="melt-quote-id",
        method="onchain",
        request="u1sampleaddress",
        checking_id="pending",
        unit="zec",
        amount=300000,
        fee_reserve=10000,
        state=state,
        expiry=1701704757,
    )


# ------------------------------------------------------------------
# Fee options
# ------------------------------------------------------------------


def test_fee_options_single_indexed_entry():
    """Zcash advertises exactly one fee option, at index 0."""
    options = router_onchain._melt_fee_options(10000)
    assert len(options) == 1
    (opt,) = options
    assert opt.fee_index == router_onchain._ONLY_FEE_INDEX == 0
    assert opt.fee_reserve == 10000
    assert opt.estimated_blocks == settings.mint_zcash_min_confirmations


# ------------------------------------------------------------------
# selected_fee_index accounting
# ------------------------------------------------------------------


def test_selected_fee_index_none_before_execution():
    assert router_onchain._selected_fee_index(_melt_quote(MeltQuoteState.unpaid)) is None


@pytest.mark.parametrize("state", [MeltQuoteState.pending, MeltQuoteState.paid])
def test_selected_fee_index_set_after_execution(state):
    assert router_onchain._selected_fee_index(_melt_quote(state)) == 0


# ------------------------------------------------------------------
# Response builder threads change + selected_fee_index
# ------------------------------------------------------------------


def test_build_melt_quote_response_carries_change():
    quote = _melt_quote(MeltQuoteState.paid)
    change = [
        BlindedSignature(
            id="009a1f293253e41e",
            amount=1000,
            C_="03c668f551855ddc792e22ea61d32ddfa6a45b1eb659ce66e915bf5127a8657be0",
        )
    ]
    resp = router_onchain._build_melt_quote_response(
        quote=quote,
        fee_options=router_onchain._melt_fee_options(quote.fee_reserve),
        outpoint="4d5e6f:0",
        selected_fee_index=0,
        change=change,
    )
    assert isinstance(resp, PostMeltQuoteOnchainResponse)
    assert resp.selected_fee_index == 0
    assert resp.outpoint == "4d5e6f:0"
    assert resp.change == change
    # No top-level fee_reserve; it lives inside fee_options.
    assert not hasattr(resp, "fee_reserve")


# ------------------------------------------------------------------
# Model contracts
# ------------------------------------------------------------------


def test_melt_request_requires_fee_index():
    with pytest.raises(ValidationError):
        PostMeltOnchainRequest(quote="q", inputs=[])


def test_melt_request_rejects_negative_fee_index():
    with pytest.raises(ValidationError):
        PostMeltOnchainRequest(quote="q", fee_index=-1, inputs=[])


def test_fee_option_serialization_shape():
    opt = OnchainFeeOption(fee_index=0, fee_reserve=10000, estimated_blocks=2)
    assert opt.model_dump() == {
        "fee_index": 0,
        "fee_reserve": 10000,
        "estimated_blocks": 2,
    }


# ------------------------------------------------------------------
# Handlers surface NUT-08 change on a settled melt
#
# Onchain melts are always async: the change is computed when the send
# settles and is only observable via the check-quote endpoint. These tests
# drive the handlers with a mocked ledger to assert the response actually
# threads `change` through — the regression that motivated this file.
# ------------------------------------------------------------------


_CHANGE = [
    BlindedSignature(
        id="009a1f293253e41e",
        amount=1000,
        C_="03c668f551855ddc792e22ea61d32ddfa6a45b1eb659ce66e915bf5127a8657be0",
    )
]


def _settled_quote_with_change() -> MeltQuote:
    quote = _melt_quote(MeltQuoteState.paid)
    quote.change = _CHANGE
    quote.payment_preimage = "melt_tx_456"  # surfaced as `outpoint`
    return quote


@pytest.mark.asyncio
async def test_get_melt_quote_surfaces_change(monkeypatch):
    async def fake_get_melt_quote(quote_id, *a, **k):
        return _settled_quote_with_change()

    monkeypatch.setattr(router_onchain.ledger, "get_melt_quote", fake_get_melt_quote)

    resp = await router_onchain.onchain_get_melt_quote.__wrapped__(
        request=None, quote="melt-quote-id"
    )
    assert resp.change == _CHANGE
    assert resp.outpoint == "melt_tx_456"
    assert resp.selected_fee_index == 0


@pytest.mark.asyncio
async def test_melt_surfaces_change_from_refetched_quote(monkeypatch):
    async def fake_melt(*a, **k):
        return None  # handler ignores the return value

    async def fake_get_melt_quote(quote_id, *a, **k):
        return _settled_quote_with_change()

    monkeypatch.setattr(router_onchain.ledger, "melt", fake_melt)
    monkeypatch.setattr(router_onchain.ledger, "get_melt_quote", fake_get_melt_quote)

    payload = PostMeltOnchainRequest(quote="melt-quote-id", fee_index=0, inputs=[])
    resp = await router_onchain.onchain_melt.__wrapped__(request=None, payload=payload)
    assert resp.change == _CHANGE
    assert resp.selected_fee_index == 0
