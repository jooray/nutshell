# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Branch Context

**This is a fork of [cashubtc/nutshell](https://github.com/cashubtc/nutshell) on the `units` branch, implementing the fiat-backend feature.**

**IMPORTANT: Make minimal changes outside of the fiat backend code.** When working on this branch:
- Focus changes on fiat backend files (listed below)
- Avoid refactoring or modifying unrelated code
- Keep upstream compatibility in mind for eventual PR

## Project Overview

Cashu Nutshell is a Chaumian Ecash wallet and mint for Bitcoin Lightning implementing the [Cashu protocol](https://github.com/cashubtc/nuts). It uses Blind Diffie-Hellman Key Exchange (B-DHKE) for privacy-preserving digital payments.

## Common Commands

```bash
# Install dependencies
poetry install

# Run tests (requires MINT_BACKEND_BOLT11_SAT=FakeWallet and TOR=FALSE in .env)
make test                    # All tests with coverage
make test-wallet             # Wallet tests only
make test-mint               # Mint tests only
poetry run pytest tests/path/to/test.py -k "test_name"  # Single test

# Code quality
make format                  # Auto-format with Ruff
make check                   # Run ruff-check + mypy
poetry run pre-commit install  # Install git hooks

# Run the applications
poetry run cashu             # Wallet CLI
poetry run mint              # Mint server
poetry run mint-cli          # Mint management CLI
```

## Architecture

### Core Package Structure (`cashu/`)

- **`core/`** - Protocol implementation shared between wallet and mint
  - `base.py` - Core data models: `Proof`, `BlindedMessage`, `BlindedSignature`, `MintQuote`, `MeltQuote`
  - `crypto/` - B-DHKE, secp256k1, AES encryption
  - `db.py` - Database abstraction (SQLite/PostgreSQL via SQLAlchemy 2.0 async)
  - `settings.py` - Global configuration from environment variables
  - `nuts/` - NUT specification implementations

- **`mint/`** - Mint server (FastAPI)
  - `ledger.py` - Core mint logic: issuing tokens, verifying proofs, handling quotes
  - `router.py` - API endpoints
  - `crud.py` - Database operations
  - `db/read.py`, `db/write.py` - Separated read/write database helpers

- **`wallet/`** - Wallet implementation
  - `wallet.py` - Main wallet class, composed via mixins (`WalletP2PK`, `WalletHTLC`, `WalletSecrets`)
  - `cli/` - Click-based CLI interface
  - `v1_api.py` - Wallet API implementation

- **`lightning/`** - Lightning backend implementations
  - `base.py` - Abstract `LightningBackend` interface
  - Backends: `lndrest.py`, `clnrest.py`, `lnbits.py`, `fake.py` (testing), `unitsbackend.py`

### Key Patterns

- **Async throughout**: All database and network operations use async/await
- **Pydantic models**: Data validation via Pydantic v1 (`cashu/core/models.py`)
- **Mixin composition**: Wallet extends multiple mixins for P2PK, HTLC, secrets functionality
- **Backend abstraction**: Lightning operations go through `LightningBackend` interface

### Testing

Tests use `FakeWallet` backend (auto-pays all invoices). Configure in `.env`:
```
MINT_BACKEND_BOLT11_SAT=FakeWallet
TOR=FALSE
```

For Lightning integration tests, use [cashu-regtest](https://github.com/callebtc/cashu-regtest) environment.

### Debugging

```bash
# Enable debug logging
DEBUG=TRUE
LOG_LEVEL=TRACE  # Even more verbose

# Performance profiling
DEBUG_PROFILING=TRUE
```

## Fiat Backend Feature (This Branch)

The fiat backend allows the mint to issue tokens in fiat currencies (USD, EUR, CZK, etc.) while settling Lightning transactions in Bitcoin. Exchange rates are fetched from CoinGecko.

### Fiat Backend Files (Primary Focus)

**New files:**
- `cashu/lightning/unitsbackend.py` - Main `UnitsBackend` class wrapping any `LightningBackend` with FX conversion
- `tests/test_mint_fiat.py` - Fiat mint tests
- `docs/fiat-backend.md` - Full documentation

**Modified files:**
- `cashu/core/base.py` - Dynamic `Unit` enum with `_missing_()` for custom units, `decimals` property
- `cashu/core/settings.py` - Settings: `mint_units`, `mint_unit_decimals`, `mint_fiat_backend_units`, fee configs
- `cashu/mint/startup.py` - Fiat backend initialization in mint startup

**Config changes:**
- `.env.example` - New env vars for fiat configuration
- `docker-compose.yaml`, `Dockerfile` - Docker support

### Key Concepts

- `UnitsBackend` wraps the sat-based Lightning backend and converts amounts at current FX rates
- Fees are configured per-unit via `FIAT_BACKEND_MINT_FEE_XXX` and `FIAT_BACKEND_MELT_FEE_XXX`
- Custom units need derivation paths in `MINT_DERIVATION_PATH_LIST` (see docs/fiat-backend.md)

### Running Fiat Backend Tests

```bash
# Run fiat-specific tests
poetry run pytest tests/test_mint_fiat.py -v
```
