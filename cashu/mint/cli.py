import asyncio
import json
from datetime import datetime
from typing import Optional

import click
from tabulate import tabulate

from cashu.core.db import Database
from cashu.core.settings import settings
from cashu.mint.crud import LedgerCrudSqlite


@click.group()
def cli():
    """Mint management CLI"""
    pass


@cli.command()
@click.option("--unit", "-u", help="Filter by currency unit (e.g., usd, eur)")
@click.option("--start-date", "-s", help="Start date (YYYY-MM-DD)")
@click.option("--end-date", "-e", help="End date (YYYY-MM-DD)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def accounting_summary(unit: Optional[str], start_date: Optional[str], end_date: Optional[str], as_json: bool):
    """Show accounting summary for unit operations"""

    async def run():
        db = Database("mint", settings.mint_database)
        crud = LedgerCrudSqlite()
        summary = await crud.get_unit_accounting_summary(db=db, unit=unit, start_date=start_date, end_date=end_date)

        if as_json:
            click.echo(json.dumps(summary, indent=2))
        else:
            headers = ["Unit", "Minted", "Melted", "Net", "Mint Fees", "Melt Fees", "Total Fees", "Mint Count", "Melt Count"]
            rows = []

            for unit_name, data in summary.items():
                net = data["minted"] - data["melted"]
                total_fees = data["mint_fees"] + data["melt_fees"]
                rows.append([
                    unit_name.upper(),
                    data["minted"],
                    data["melted"],
                    net,
                    data["mint_fees"],
                    data["melt_fees"],
                    total_fees,
                    data["mint_count"],
                    data["melt_count"]
                ])

            click.echo(tabulate(rows, headers=headers, tablefmt="pretty"))

    asyncio.run(run())


@cli.command()
@click.option("--unit", "-u", help="Filter by currency unit")
@click.option("--operation", "-o", type=click.Choice(["mint", "melt"]), help="Filter by operation type")
@click.option("--limit", "-l", default=50, help="Number of entries to show")
@click.option("--offset", "-of", default=0, help="Offset for pagination")
def accounting_entries(unit: Optional[str], operation: Optional[str], limit: int, offset: int):
    """Show individual accounting entries"""

    async def run():
        db = Database("mint", settings.mint_database)
        crud = LedgerCrudSqlite()
        entries = await crud.get_unit_accounting_entries(db=db, unit=unit, operation=operation, limit=limit, offset=offset)

        headers = ["ID", "Unit", "Amount", "Operation", "Exchange Rate", "Sat Amount", "Fee %", "Fee Amount", "Created"]
        rows = []

        for entry in entries:
            created = entry["created"]
            if isinstance(created, (int, float)):
                created = datetime.fromtimestamp(created).strftime("%Y-%m-%d %H:%M:%S")

            rows.append([
                entry["id"],
                entry["unit"].upper(),
                entry["amount"],
                entry["operation"],
                f"{entry['exchange_rate']:.6f}",
                entry["sat_amount"],
                f"{entry['fee_percent']:.2f}%",
                entry["fee_amount"],
                created
            ])

        click.echo(tabulate(rows, headers=headers, tablefmt="pretty"))

    asyncio.run(run())


if __name__ == "__main__":
    cli()
