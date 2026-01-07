# Fiat Backend Configuration

The Nutshell mint supports multiple currencies through a configurable fiat backend. This allows the mint to accept and issue tokens denominated in various fiat currencies while settling Lightning transactions in Bitcoin.

## Configuration

### 1. Supported Units

Define which units your mint supports:

```bash
# List of supported units (comma-separated)
MINT_UNITS=sat,usd,eur,czk

# Define decimal places for custom units (optional, defaults to 0)
MINT_UNIT_DECIMALS_CZK=0
```

You also need to specify keysets, this is a bit hacky currently.

These are set currently:

    sat = 0
    msat = 1
    usd = 2
    eur = 3
    btc = 4

Next one that is not of these starts at 5. So for the above sat,usd,eur,czk mint units, you need to set

```bash
MINT_DERIVATION_PATH_LIST=["m/0'/2'/0'","m/0'/3'/0'","m/0'/5'/0'"]
```

(the path "m/0'/0'/0'" is included by default from MINT_DERIVATION_PATH).

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
4. **Token Issuance**: Nuts are issued in the requested fiat currency

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
3. Set decimal places if not 0: `MINT_UNIT_DECIMALS_XXX=N`
4. Configure fees: `FIAT_BACKEND_MINT_FEE_XXX` and `FIAT_BACKEND_MELT_FEE_XXX`
5. Don't forget to set correct MINT_DERIVATION_PATH_LIST

Example for Japanese Yen:
```bash
MINT_UNITS=sat,usd,eur,jpy
MINT_FIAT_BACKEND_UNITS=usd,eur,jpy
MINT_UNIT_DECIMALS_JPY=0
FIAT_BACKEND_MINT_FEE_JPY=0.5
FIAT_BACKEND_MELT_FEE_JPY=0.5
MINT_DERIVATION_PATH_LIST=["m/0'/2'/0'","m/0'/3'/0'","m/0'/5'/0'"]
```

## Limitations

- Exchange rates are cached for 5 minutes by default
- Requires internet connection to fetch exchange rates
- USD rate must be available
- Does not support currencies which don't have coingecko backend

## Decimals and support in wallets

Cashu.me wallet discovers the units automatically, but since there's no way to communicate decimals, it
will only work if decimals=0. Otherwise it would need to be hardcoded in the client. Notably, swiss franc with
cents would be a problem, you would need to round to full franks.

Minibits does not support custom currencies, they are hardcoded. They should be discovered.

## Further development

- Better and more resilient exchange rate API
- Some mechanism to drive the hedging of the exchange rate
  - Either regular output about the number of minted tokens adjusts the position (local fluctuations are covered by fees)
  - Or only premint tokens and exchange
- Add support for a non-fiat backing backend.
  - To have XMR in the backend. This quickly turns into an exchange, or instant XMR-denominated payments without needing for blockchain
    confirmations (you mint the tokens, wait and then you can use them immediately for eCash payments or through lightning)
  - Or a ticket to an event. It can have static mint quote and disabled melt quote.
  - Or a gift certificate to use with a proxynut service that is not meltable to lightning. "Here are some credits to try our service", but we don't want the users to cash them out through lightning.

I did not even try to run the tests yet.
