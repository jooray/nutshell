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

## Accounting and Monitoring

The fiat backend includes built-in accounting to track all mint and melt operations. This data is stored in the `unit_accounting` table and can be accessed through CLI commands.

### Database Schema

All fiat transactions are recorded in the `unit_accounting` table with the following information:
- Unit (currency)
- Amount (in smallest unit, e.g., cents)
- Operation type (mint or melt)
- Exchange rate at time of transaction
- Satoshi amount
- Fee percentage and amount
- Timestamp

### Accessing Accounting Data

#### Summary View

To get an overview of all fiat operations:

```bash
# Show summary for all currencies
poetry run mint-tools accounting-summary

# Filter by currency
poetry run mint-tools accounting-summary --unit usd

# Filter by date range
poetry run mint-tools accounting-summary --start-date 2024-01-01 --end-date 2024-01-31

# Export as JSON
poetry run mint-tools accounting-summary --json > accounting.json
```

Example output:
```
+------+---------+--------+-------+-----------+-----------+-------------+------------+------------+
| Unit | Minted  | Melted | Net   | Mint Fees | Melt Fees | Total Fees  | Mint Count | Melt Count |
+------+---------+--------+-------+-----------+-----------+-------------+------------+------------+
| USD  | 1000000 | 750000 | 250000| 10000     | 7500      | 17500       | 45         | 32         |
| EUR  | 850000  | 620000 | 230000| 8500      | 6200      | 14700       | 38         | 28         |
+------+---------+--------+-------+-----------+-----------+-------------+------------+------------+
```

#### Detailed Transaction View

To see individual transactions:

```bash
# Show recent transactions
poetry run mint-tools accounting-entries

# Filter by operation type
poetry run mint-tools accounting-entries --operation mint

# Filter by currency and limit results
poetry run mint-tools accounting-entries --unit eur --limit 10
```

### Running Commands in Different Environments

#### Local Development (Poetry)

When running locally with poetry (recommended for development):

```bash
# Install dependencies
poetry install

# Run accounting commands
poetry run mint-tools accounting-summary
poetry run mint-tools accounting-entries
```

#### Docker Compose

When running with docker-compose:

```bash
# Run accounting commands in the mint container
docker-compose exec mint mint-tools accounting-summary
docker-compose exec mint mint-tools accounting-entries --unit usd

# Or run one-off commands
docker-compose run --rm mint mint-tools accounting-summary --json
```

#### Production (Installed Package)

If you have installed cashu as a system package:

```bash
# Direct command execution
mint-tools accounting-summary
mint-tools accounting-entries
```

### Programmatic Access

You can also access accounting data programmatically:

```python
from cashu.core.db import Database
from cashu.mint.crud import get_fiat_accounting_summary
from cashu.core.settings import settings

async def check_accounting():
    db = Database("mint", settings.mint_database)
    summary = await get_fiat_accounting_summary(db)
    
    for unit, data in summary.items():
        net_position = data["minted"] - data["melted"]
        total_fees = data["mint_fees"] + data["melt_fees"]
        print(f"{unit}: Net position: {net_position}, Total fees collected: {total_fees}")
```

### Monitoring Best Practices

1. **Regular Reconciliation**: Run accounting summaries daily to track positions
2. **Exchange Rate Monitoring**: Monitor the exchange rates being used for conversions
3. **Fee Analysis**: Track fee collection to ensure profitability
4. **Hedging**: Use the net position data to inform hedging strategies

### Database Queries

For advanced analysis, you can query the database directly:

```sql
-- Daily volume by currency
SELECT 
    unit,
    DATE(created) as date,
    SUM(CASE WHEN operation = 'mint' THEN amount ELSE 0 END) as minted,
    SUM(CASE WHEN operation = 'melt' THEN amount ELSE 0 END) as melted
FROM fiat_accounting
GROUP BY unit, DATE(created)
ORDER BY date DESC;

-- Average exchange rates over time
SELECT 
    unit,
    DATE(created) as date,
    AVG(exchange_rate) as avg_rate,
    MIN(exchange_rate) as min_rate,
    MAX(exchange_rate) as max_rate
FROM fiat_accounting
GROUP BY unit, DATE(created);
```

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
