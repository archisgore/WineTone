"""Command-line entry point for the WineTone data pipeline.

Examples:

    winetone list                        # show registered sources
    winetone pull uci_wine_quality       # run one source end-to-end
    winetone pull --tier a               # run every source in Tier A
    winetone inspect uci_wine_quality    # show staged Parquet summary
"""

from __future__ import annotations

import logging

import click
import pandas as pd
import pyarrow.parquet as pq
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from winetone import canonicalize, db
from winetone.paths import staging_dir
from winetone.sources import SOURCES, get

console = Console()

# Tier A = the always-on, free-and-redistributable downloads.
# Tier B = scrape-with-care + free-API sources from the plan
#         (Wikidata, EU registries, USDA grape, etc.). As we add more
#         scrapers (TTB COLA — Sprint 3), they land here.
TIERS: dict[str, list[str]] = {
    "a": [
        "uci_wine_quality",
        "uci_wine",
        "wine_enthusiast_130k",
        "wine_enthusiast_150k",
    ],
    "b": [
        "wikidata",
    ],
}


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="DEBUG logging.")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """WineTone data + ML pipeline CLI."""
    _setup_logging(verbose)
    ctx.ensure_object(dict)


@main.command("list")
def list_sources() -> None:
    """List every registered source."""
    table = Table(show_lines=False, title="WineTone sources")
    table.add_column("name", style="cyan", no_wrap=True)
    table.add_column("description")
    table.add_column("homepage", style="dim")
    for name, cls in sorted(SOURCES.items()):
        table.add_row(name, cls.description, cls.homepage)
    console.print(table)


@main.command("pull")
@click.argument("source", required=False)
@click.option(
    "--tier",
    type=click.Choice(sorted(TIERS), case_sensitive=False),
    help="Pull all sources in a tier instead of one source by name.",
)
def pull(source: str | None, tier: str | None) -> None:
    """Fetch + parse + stage a source (or every source in a tier)."""
    if (source is None) == (tier is None):
        raise click.UsageError("specify exactly one of SOURCE or --tier")

    names = [source] if source else TIERS[tier.lower()]
    manifests = []
    for n in names:
        if n not in SOURCES:
            raise click.UsageError(f"unknown source: {n}")
        console.rule(f"[bold cyan]{n}")
        src = get(n)
        m = src.run()
        manifests.append(m)
        console.print(
            f"[green]ok[/] · rows=[bold]{m['rows']:,}[/] "
            f"cols=[bold]{len(m['cols'])}[/] → {m['parquet_path']}"
        )

    console.print()
    console.print(
        f"[bold]done[/] · {len(manifests)} source(s), "
        f"{sum(int(m['rows']) for m in manifests):,} total rows"
    )


@main.command("inspect")
@click.argument("source")
@click.option("--head", type=int, default=5, help="Rows to preview.")
def inspect(source: str, head: int) -> None:
    """Show row count, schema, and a head() of a staged source."""
    if source not in SOURCES:
        raise click.UsageError(f"unknown source: {source}")
    pq_path = staging_dir(source) / f"{source}.parquet"
    if not pq_path.exists():
        raise click.UsageError(
            f"no staged parquet at {pq_path}; run `winetone pull {source}` first"
        )
    df = pd.read_parquet(pq_path)
    console.print(
        f"[bold]{source}[/] · rows=[bold]{len(df):,}[/] "
        f"cols=[bold]{len(df.columns)}[/] · {pq_path}"
    )
    schema_t = Table(title="schema")
    schema_t.add_column("column", style="cyan")
    schema_t.add_column("dtype")
    schema_t.add_column("null %", justify="right")
    for c in df.columns:
        null_pct = f"{df[c].isna().mean() * 100:.1f}%"
        schema_t.add_row(c, str(df[c].dtype), null_pct)
    console.print(schema_t)
    console.print(f"[bold]head[/] (first {head} rows):")
    console.print(df.head(head).to_string(max_cols=8))


@main.command("status")
def status() -> None:
    """One-line summary of what's staged on disk."""
    table = Table(title="staged sources")
    table.add_column("source", style="cyan")
    table.add_column("rows", justify="right")
    table.add_column("size", justify="right")
    table.add_column("path", style="dim")
    total_rows = 0
    total_bytes = 0
    for name in sorted(SOURCES):
        pq_path = staging_dir(name) / f"{name}.parquet"
        if pq_path.exists():
            size = pq_path.stat().st_size
            # Read row count from Parquet metadata directly — pandas 3.0's
            # read_parquet(columns=[]) returns an empty DataFrame.
            rows = pq.ParquetFile(pq_path).metadata.num_rows
            table.add_row(name, f"{rows:,}", _human_bytes(size), str(pq_path))
            total_rows += rows
            total_bytes += size
        else:
            table.add_row(name, "-", "-", "[red]not staged")
    console.print(table)
    console.print(
        f"[bold]total[/]: {total_rows:,} rows · {_human_bytes(total_bytes)}"
    )


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


@main.group("build")
def build_group() -> None:
    """Build downstream artifacts from the staged sources."""


@build_group.command("canonical")
def build_canonical() -> None:
    """Phase 2: entity resolution + canonical wines/features tables in CedarDB."""
    if not db.ping():
        console.print(
            "[red]CedarDB unreachable.[/] Run `make db-up-bg` first."
        )
        raise click.Abort()
    console.rule("[bold cyan]Phase 2 — canonicalize")
    summary = canonicalize.build()
    console.print(
        f"[green]ok[/] · "
        f"wines=[bold]{summary['n_wines']:,}[/] "
        f"source_records=[bold]{summary['n_source_records']:,}[/] "
        f"features=[bold]{summary['n_features']:,}[/]"
    )


@main.command("db-status")
def db_status() -> None:
    """Show CedarDB connection + canonical-table row counts."""
    if not db.ping():
        console.print("[red]CedarDB unreachable.[/]")
        return
    console.print("[green]CedarDB reachable[/]")
    eng = db.engine()
    table = Table(title="canonical tables")
    table.add_column("table", style="cyan")
    table.add_column("rows", justify="right")
    for t in ("wines", "source_records", "wine_features", "wine_embeddings"):
        try:
            n = pd.read_sql(f"SELECT COUNT(*) AS n FROM {t}", eng).iloc[0]["n"]
            table.add_row(t, f"{int(n):,}")
        except Exception:  # noqa: BLE001
            table.add_row(t, "[dim]not built[/]")
    console.print(table)


if __name__ == "__main__":
    main()
