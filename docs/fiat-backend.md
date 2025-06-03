# Fiat Backend Configuration

The Nutshell mint supports multiple currencies through a configurable fiat backend. This allows the mint to accept and issue tokens denominated in various fiat currencies while settling Lightning transactions in Bitcoin.

## Configuration

### 1. Supported Units

Define which units your mint supports:

```bash
# List of supported units (comma-separated)
MINT_UNITS=sat,usd,eur,czk

# Define decimal places for custom units (optional, defaults to 2)
MINT_UNIT_DECIMALS_CZK=2
```

### 2. Fiat Backend Units

Specify which units should be handled by the fiat backend:

```bash
# Units that will use the fiat backend
MINT_FIAT_BACKEND_UNITS=usd,eur,czk

# Optional: specify which Lightning backend to use for fiat conversions
# If not set, uses the sat backend
MINT_FIAT_BOLT11_BACKEND=sat
```

### 3. Fee Configuration

Set mint and melt fees for each fiat unit (in percent):

```bash
# Minting fees (when users deposit)
FIAT_BACKEND_MINT_FEE_USD=1.0
FIAT_BACKEND_MINT_FEE_EUR=1.0
FIAT_BACKEND_MINT_FEE_CZK=0.8

# Melting fees (when users withdraw)
FIAT_BACKEND_MELT_FEE_USD=1.0
FIAT_BACKEND_MELT_FEE_EUR=1.0
FIAT_BACKEND_MELT_FEE_CZK=0.8
```

## How It Works

1. **Exchange Rates**: The fiat backend fetches current BTC exchange rates from CoinGecko API
2. **Fee Application**: Configured fees are applied on top of the exchange rate
3. **Lightning Settlement**: All Lightning transactions are settled in Bitcoin (sats)
4. **Token Issuance**: Tokens are issued in the requested fiat currency

## Example Flow

### Minting (Deposit)
1. User requests to mint $100 USD
2. Fiat backend applies 1% fee = $101
3. Converts to sats at current rate (e.g., 1 BTC = $50,000)
4. Creates Lightning invoice for 202,000 sats
5. Upon payment, issues tokens worth $100 USD

### Melting (Withdrawal)
1. User requests to melt $100 USD worth of tokens
2. Fiat backend applies 1% fee = $101
3. Converts to sats at current rate
4. Pays Lightning invoice minus fees
5. Burns the $100 USD tokens

## Adding New Currencies

To add a new currency:

1. Add it to `MINT_UNITS`
2. Add it to `MINT_FIAT_BACKEND_UNITS` 
3. Set decimal places if not 2: `MINT_UNIT_DECIMALS_XXX=N`
4. Configure fees: `FIAT_BACKEND_MINT_FEE_XXX` and `FIAT_BACKEND_MELT_FEE_XXX`

Example for Japanese Yen:
```bash
MINT_UNITS=sat,usd,eur,jpy
MINT_FIAT_BACKEND_UNITS=usd,eur,jpy
MINT_UNIT_DECIMALS_JPY=0
FIAT_BACKEND_MINT_FEE_JPY=0.5
FIAT_BACKEND_MELT_FEE_JPY=0.5
```

## API Endpoints

The mint will automatically support the configured units in all endpoints:

- `/v1/info` - Shows supported units
- `/v1/mint/quote/{method}` - Request quotes in any supported unit
- `/v1/melt/quote/{method}` - Request melt quotes in any supported unit
- `/v1/swap` - Swap between different unit tokens

## Limitations

- Exchange rates are cached for 5 minutes by default
- Requires internet connection to fetch exchange rates
- USD rate must be available (used as fallback)
