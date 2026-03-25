# Fiat Backend Configuration

The Nutshell mint supports multiple fiat currencies through the **unitsd** service. unitsd is a standalone daemon that manages exchange rates, fees, and hedging, communicating with the mint via REST API.

## Architecture

```
┌─────────────────┐         REST API          ┌─────────────┐
│  Nutshell Mint   │ ◄────────────────────────► │   unitsd    │
│  (UnitsBackend)  │   Bearer token auth        │  (FastAPI)  │
└─────────────────┘                             └──────┬──────┘
                                                       │
                                              ┌────────┴────────┐
                                              │   CoinGecko     │
                                              │   (prices)      │
                                              └─────────────────┘
```

- **unitsd** owns all pricing, fee configuration, and hedging logic
- **Nutshell** queries unitsd for exchange rates and reports mint/melt operations
- Communication uses a shared bearer token (`UNITSD_API_SECRET`)

## Mint Configuration

### Required Environment Variables

```bash
# List of units the mint supports (sat is always included)
MINT_UNITS=usd,eur,czk

# unitsd connection
UNITSD_URL=http://localhost:3339
UNITSD_API_SECRET=<shared-secret-with-unitsd>

# Lightning backend (used for actual payments)
MINT_BACKEND_BOLT11_SAT=LndRestWallet  # or CLNRestWallet, etc.
```

### How Unit Discovery Works

On startup, the mint:

1. Calls `GET /api/v1/units` on unitsd to discover available currencies
2. Registers each fiat unit from `MINT_UNITS` that unitsd supports
3. Generates keysets using the `path_index` returned by unitsd (e.g., usd=2, eur=3)
4. Creates a `UnitsBackend` wrapper around the Lightning backend for each fiat unit

No manual `MINT_DERIVATION_PATH_LIST` configuration is needed -- derivation paths are assigned by unitsd and communicated via the API.

## unitsd Configuration

unitsd is configured via its own `.env` file. See `unitsd/.env.example` for all options.

Key settings:

```bash
# API secret (must match UNITSD_API_SECRET in mint .env)
UNITSD_API_SECRET=<shared-secret>

# Initial currencies to seed on first run
UNITSD_INITIAL_CURRENCIES=usd,eur,czk

# Fee configuration (percent)
UNITSD_INITIAL_MINT_FEE_USD=1.0
UNITSD_INITIAL_MELT_FEE_USD=1.0
UNITSD_INITIAL_MINT_FEE_EUR=1.0
UNITSD_INITIAL_MELT_FEE_EUR=1.0
UNITSD_INITIAL_MINT_FEE_CZK=0.8
UNITSD_INITIAL_MELT_FEE_CZK=0.8

# Price cache duration (seconds)
PRICE_CACHE_SECONDS=300
```

## How It Works

### Minting (Deposit)

1. User requests to mint 100 USD (= 10000 cents)
2. Mint calls `GET /api/v1/quote/mint?amount=10000&unit=usd` on unitsd
3. unitsd looks up BTC/USD rate, applies 1% fee, returns `amount_msat`
4. Mint creates a Lightning invoice for that amount
5. Upon payment, tokens worth 100 USD are issued
6. Mint calls `POST /api/v1/callback/mint` to record the hedging position

### Melting (Withdrawal)

1. User submits a Lightning invoice to pay with USD tokens
2. Mint calls `GET /api/v1/quote/melt?invoice_msat=X&unit=usd&ln_fee_msat=Y` on unitsd
3. unitsd converts the msat amount to fiat, adds melt fee
4. Mint burns the required USD tokens and pays the invoice
5. Mint calls `POST /api/v1/callback/melt` to record the hedging position

## API Endpoints (unitsd)

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/health` | GET | No | Health check |
| `/api/v1/units` | GET | Yes | List enabled currencies |
| `/api/v1/quote/mint` | GET | Yes | Get mint quote (fiat to msat) |
| `/api/v1/quote/melt` | GET | Yes | Get melt quote (msat to fiat) |
| `/api/v1/callback/mint` | POST | Yes | Report completed mint (hedging) |
| `/api/v1/callback/melt` | POST | Yes | Report completed melt (hedging) |
| `/api/v1/currencies` | CRUD | Yes | Manage currencies |
| `/api/v1/positions` | GET | Yes | Query hedging positions |

## Adding New Currencies

### Via unitsd API

```bash
curl -X POST http://localhost:3339/api/v1/currencies \
  -H "Authorization: Bearer $UNITSD_API_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"code": "gbp", "mint_fee_percent": 1.0, "melt_fee_percent": 1.0}'
```

unitsd automatically assigns a stable `path_index` and fills in known metadata (name, decimals).

### Then Update the Mint

1. Add the new currency to `MINT_UNITS` in the mint's `.env`
2. Restart the mint -- it will discover the new unit from unitsd and generate keysets

### Reserved Path Indices

These indices are reserved and match the nutshell `Unit` enum:

| Code | path_index | Notes |
|---|---|---|
| sat | 0 | Built-in |
| msat | 1 | Built-in |
| usd | 2 | |
| eur | 3 | |
| btc | 4 | Built-in |
| auth | 999 | NUT-22 |

Custom currencies start at index 5 and are never reused (even after disabling).

## Hedging

unitsd records every mint and melt operation as a position. The positions summary shows net exposure per currency:

```bash
curl http://localhost:3339/api/v1/positions/summary \
  -H "Authorization: Bearer $UNITSD_API_SECRET"
```

The v1.0 hedging backend is database-only (position tracking). Future versions may integrate with exchange APIs for automatic hedging.

## Limitations

- Exchange rates are cached for 5 minutes by default (configurable via `PRICE_CACHE_SECONDS`)
- Requires internet connection for CoinGecko price fetches
- New currencies require a mint restart (v1.0 limitation, planned for v1.1)
- The mint fails fast if unitsd is unreachable (no fallback to stale prices)

## Wallet Compatibility

- **Cashu.me**: Discovers units automatically via NUT-06, but decimals must be 0 for unknown currencies
- **Minibits**: Custom currencies are not yet discovered automatically

## Running Tests

```bash
# unitsd tests (no network calls)
cd unitsd
poetry run pytest tests/ --ignore=tests/test_price_sources.py -v

# Nutshell fiat backend tests
cd nutshell
poetry run pytest tests/test_mint_fiat.py -v
```
