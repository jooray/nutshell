# startup routine of the standalone app. These are the steps that need
# to be taken by external apps importing the cashu mint.

import asyncio
import importlib
from copy import copy
from typing import Dict

from loguru import logger

import cashu.mint.management_rpc.management_rpc as management_rpc

from ..core.base import Method, Unit
from ..core.db import Database
from ..core.migrations import migrate_databases
from ..core.settings import settings
from ..lightning.base import LightningBackend
from ..lightning.unitsbackend import UnitsBackend
from ..mint import migrations as mint_migrations
from ..mint.auth import migrations as auth_migrations
from ..mint.auth.server import AuthLedger
from ..mint.crud import LedgerCrudSqlite
from ..mint.ledger import Ledger

# kill the program if python runs in non-__debug__ mode
# which could lead to asserts not being executed for optimized code
if not __debug__:
    raise Exception("Nutshell cannot run in non-debug mode.")

logger.debug("Enviroment Settings:")
for key, value in settings.dict().items():
    if key in [
        "mint_private_key",
        "mint_seed_decryption_key",
        "mint_lnbits_key",
        "mint_blink_key",
        "mint_strike_key",
        "mint_lnd_rest_macaroon",
        "mint_lnd_rest_admin_macaroon",
        "mint_lnd_rest_invoice_macaroon",
        "mint_corelightning_rest_macaroon",
        "mint_clnrest_rune",
        "unitsd_api_secret",
        "mint_zcash_zwalletd_secret",
    ]:
        value = "********" if value is not None else None

    if key == "mint_database" and value and value.startswith("postgres://"):
        value = "postgres://********"

    logger.debug(f"{key}: {value}")

Unit.init_custom_units()

wallets_module = importlib.import_module("cashu.lightning")

backends: Dict[Method, Dict[Unit, LightningBackend]] = {}
if settings.mint_backend_bolt11_sat:
    backend_bolt11_sat = getattr(wallets_module, settings.mint_backend_bolt11_sat)(
        unit=Unit.sat
    )
    backends.setdefault(Method.bolt11, {})[Unit.sat] = backend_bolt11_sat
if settings.mint_backend_bolt11_msat:
    backend_bolt11_msat = getattr(wallets_module, settings.mint_backend_bolt11_msat)(
        unit=Unit.msat
    )
    backends.setdefault(Method.bolt11, {})[Unit.msat] = backend_bolt11_msat
if settings.mint_backend_bolt11_usd:
    backend_bolt11_usd = getattr(wallets_module, settings.mint_backend_bolt11_usd)(
        unit=Unit.usd
    )
    backends.setdefault(Method.bolt11, {})[Unit.usd] = backend_bolt11_usd
if settings.mint_backend_bolt11_eur:
    backend_bolt11_eur = getattr(wallets_module, settings.mint_backend_bolt11_eur)(
        unit=Unit.eur
    )
    backends.setdefault(Method.bolt11, {})[Unit.eur] = backend_bolt11_eur
if not backends:
    raise Exception("No backends are set.")

if not settings.mint_private_key:
    raise Exception("No mint private key is set.")

ledger = Ledger(
    db=Database("mint", settings.mint_database),
    seed=settings.mint_private_key,
    seed_decryption_key=settings.mint_seed_decryption_key,
    derivation_path=settings.mint_derivation_path,
    backends=backends,
    crud=LedgerCrudSqlite(),
)

# UnitsBackend will be initialized during async startup (see start_mint function below)
# We defer this to avoid asyncio.run() during module import

# start auth ledger
auth_ledger = AuthLedger(
    db=Database("auth", settings.mint_auth_database),
    seed="auth seed here",
    amounts=[1],
    derivation_path="m/0'/999'/0'",
    crud=LedgerCrudSqlite(),
)


async def rotate_keys(n_seconds=60):
    """Rotate keyset epoch every n_seconds.
    Note: This is just a helper function for testing purposes.
    """
    i = 0
    while True:
        i += 1
        logger.info("Rotating keys.")
        incremented_derivation_path = (
            f"{'/'.join(ledger.derivation_path.split('/')[:-1])}/{i}"
        )
        await ledger.activate_keyset(derivation_path=incremented_derivation_path)
        logger.info(f"Current keyset: {ledger.keyset.id}")
        await asyncio.sleep(n_seconds)


async def start_auth():
    await migrate_databases(auth_ledger.db, auth_migrations)
    logger.info("Starting auth ledger.")
    await auth_ledger.init_keysets()
    await auth_ledger.init_auth()
    logger.info("Auth ledger started.")


async def start_mint():
    # Track derivation paths to activate (from unitsd)
    unitsd_derivation_paths = []

    # Initialize UnitsBackend if unitsd is configured
    if (
        settings.unitsd_url
        and settings.unitsd_api_secret
        and settings.mint_backend_bolt11_sat
    ):
        try:
            logger.info(f"Querying unitsd at {settings.unitsd_url} for supported units")

            # Create UnitsBackend and fetch units from unitsd
            units_backend = UnitsBackend(backend_bolt11_sat)
            unitsd_units = await units_backend.fetch_units_from_unitsd()

            if unitsd_units:
                # Register backend for each unit
                for unit_code in units_backend.get_supported_unit_codes():
                    try:
                        unit = Unit(unit_code)
                        if unit not in backends.get(Method.bolt11, {}):
                            backends.setdefault(Method.bolt11, {})[unit] = units_backend
                            logger.info(
                                f"Initialized UnitsBackend for unit: {unit.name} (from unitsd)"
                            )
                    except (KeyError, ValueError) as e:
                        logger.warning(f"Unknown unit from unitsd: {unit_code} - {e}")

                # Store derivation paths for keyset activation
                unitsd_derivation_paths = units_backend.get_derivation_paths()
                logger.info(
                    f"Successfully initialized UnitsBackend with {len(unitsd_units)} units from unitsd"
                )
            else:
                logger.warning("Unitsd returned no enabled currencies")

        except Exception as e:
            logger.error(f"Failed to connect to unitsd at {settings.unitsd_url}: {e}")
            raise Exception(f"Cannot start mint without unitsd connection: {e}")

    # Initialize ZcashBackend if enabled
    if settings.mint_zcash_enabled:
        from ..lightning.zcash_backend import ZcashBackend

        logger.info("Initializing ZcashBackend for onchain ZEC support")

        # Ensure the "zec" unit exists (may already be registered via unitsd)
        zec_unit = Unit("zec")  # triggers _missing_ if not already a member

        # Register "zcash" as a dynamic Method
        zcash_method = Method("zcash")

        # Create and register ZcashBackend
        zcash_backend = ZcashBackend(unit=zec_unit)
        backends.setdefault(zcash_method, {})[zec_unit] = zcash_backend

        logger.info(
            f"Registered ZcashBackend: backends[{zcash_method.name}][{zec_unit.name}]"
        )

    await migrate_databases(ledger.db, mint_migrations)
    logger.info("Starting mint ledger.")
    await ledger.startup_ledger()

    # Activate keysets for unitsd-managed units using their derivation paths
    for derivation_path in unitsd_derivation_paths:
        logger.info(f"Activating keyset for derivation path: {derivation_path}")
        await ledger.activate_keyset(derivation_path=derivation_path)

    # Activate keyset for ZEC if zcash is enabled (ensures keyset exists for the unit)
    if settings.mint_zcash_enabled:
        zec_derivation_parts = settings.mint_derivation_path.split("/")
        zec_derivation_parts[-1] = f"{zec_unit.value}'"
        zec_derivation_path = "/".join(zec_derivation_parts)
        logger.info(f"Activating ZEC keyset for derivation path: {zec_derivation_path}")
        await ledger.activate_keyset(derivation_path=zec_derivation_path)

    logger.info("Mint started.")
    # asyncio.create_task(rotate_keys())


async def shutdown_mint():
    await ledger.shutdown_ledger()
    logger.info("Mint shutdown.")
    logger.remove()


rpc_server = None


async def start_management_rpc():
    global rpc_server
    rpc_server = await management_rpc.serve(copy(ledger))


async def shutdown_management_rpc():
    if rpc_server:
        await management_rpc.shutdown(rpc_server)
