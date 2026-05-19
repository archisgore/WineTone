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

from winetone import calibrate, canonicalize, cluster, db, embed, embed_sparse
from winetone import recommend as reco
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


@build_group.command("embeddings")
@click.option(
    "--sample", type=int, default=None,
    help="Encode only this many wines (stratified). Default: full corpus."
)
def build_embeddings(sample: int | None) -> None:
    """Phase 3: dense wine embeddings via sentence-transformer."""
    if not db.ping():
        console.print(
            "[red]CedarDB unreachable.[/] Run `make db-up-bg` first."
        )
        raise click.Abort()
    console.rule("[bold cyan]Phase 3 — dense embeddings")
    summary = embed.build(sample=sample)
    console.print(
        f"[green]ok[/] · "
        f"wines=[bold]{summary['n_wines']:,}[/] "
        f"dim=[bold]{summary['dim']}[/]"
    )


@build_group.command("sparse")
def build_sparse() -> None:
    """Phase 3b: TF-IDF sparse embeddings (full corpus, fast)."""
    if not db.ping():
        console.print("[red]CedarDB unreachable.[/]")
        raise click.Abort()
    console.rule("[bold cyan]Phase 3b — sparse embeddings (TF-IDF)")
    summary = embed_sparse.build()
    console.print(
        f"[green]ok[/] · wines=[bold]{summary['n_wines']:,}[/] "
        f"vocab=[bold]{summary['vocab_size']:,}[/] "
        f"avg_terms/wine=[bold]{summary['avg_terms_per_wine']:.1f}[/]"
    )


@build_group.command("clusters")
@click.option("-k", type=int, default=16, show_default=True)
def build_clusters(k: int) -> None:
    """Phase 5: KMeans clusters over the embedding space."""
    if not db.ping():
        console.print("[red]CedarDB unreachable.[/]")
        raise click.Abort()
    console.rule(f"[bold cyan]Phase 5 — clusters (k={k})")
    summary = cluster.build(k=k)
    console.print(
        f"[green]ok[/] · k=[bold]{summary['n_clusters']}[/] "
        f"wines=[bold]{summary['n_wines']:,}[/]"
    )


@build_group.command("all")
@click.pass_context
def build_all(ctx: click.Context) -> None:
    """Run every build phase in order: canonical → embeddings → clusters."""
    ctx.invoke(build_canonical)
    ctx.invoke(build_embeddings)
    ctx.invoke(build_clusters)


# --- recommendation surface -------------------------------------------


@main.group("calibrate")
def calibrate_group() -> None:
    """Personalize the recommender with your own labels."""


@calibrate_group.command("add")
@click.option("--user", "-u", required=True, help="Your display name.")
@click.option("--query", "-q", required=True, help="Search to find the wine.")
@click.option("--description", "-d", required=True, help="Your own words.")
@click.option("--pick", type=int, default=None, help="Skip prompt; pick the Nth result.")
def calibrate_add(user: str, query: str, description: str, pick: int | None) -> None:
    """Look up a wine by search string and record your description of it."""
    if not db.ping():
        console.print("[red]CedarDB unreachable.[/]")
        raise click.Abort()
    user_id = reco.get_or_create_user(user)
    matches = reco.find_wine_by_text(query, limit=10)
    if matches.empty:
        console.print(f"[red]no wines match '{query}'[/]")
        raise click.Abort()
    if pick is None:
        table = Table(title=f"matches for '{query}'")
        for col in ("idx", "producer", "wine", "vintage", "variety", "country"):
            table.add_column(col)
        for i, row in matches.iterrows():
            table.add_row(
                str(i),
                str(row.get("producer_display", "")),
                str(row.get("wine_display", "")),
                str(row.get("vintage", "")),
                str(row.get("variety", "")),
                str(row.get("country", "")),
            )
        console.print(table)
        pick = click.prompt("which one? (idx)", type=int)
    if pick is None or pick < 0 or pick >= len(matches):
        console.print(f"[red]invalid pick {pick}[/]")
        raise click.Abort()
    wine_id = matches.iloc[pick]["wine_id"]
    reco.add_label(user_id, wine_id, description)
    console.print(
        f"[green]ok[/] · added label for "
        f"[cyan]{matches.iloc[pick]['producer_display']}[/] "
        f"[cyan]{matches.iloc[pick]['wine_display']}[/] "
        f"({matches.iloc[pick]['vintage']})"
    )


@calibrate_group.command("fit")
@click.option("--user", "-u", required=True)
@click.option(
    "--backend",
    type=click.Choice(
        ["auto", "mlx", "torch-cuda", "torch-mps", "torch-cpu", "ridge"],
        case_sensitive=False,
    ),
    default="auto",
    help=(
        "ML backend. `auto` picks MLX on Apple Silicon, then PyTorch "
        "CUDA, then PyTorch MPS, then PyTorch CPU. `ridge` uses the "
        "closed-form NumPy fallback (no PyTorch needed)."
    ),
)
def calibrate_fit(user: str, backend: str) -> None:
    """Fit your personal projection from your existing labels."""
    if not db.ping():
        console.print("[red]CedarDB unreachable.[/]")
        raise click.Abort()
    user_id = reco.get_or_create_user(user)
    if backend == "ridge":
        proj = reco.fit_projection(user_id)
        console.print(
            f"[green]ok[/] · fit (closed-form ridge) for [cyan]{user}[/] "
            f"from [bold]{proj.n_labels}[/] labels"
        )
        return

    chosen = None if backend == "auto" else backend
    summary = calibrate.fit(user_id, backend=chosen)
    console.print(
        f"[green]ok[/] · fit ([cyan]{summary['backend']}[/]) for "
        f"[cyan]{user}[/] · version=[bold]{summary['version']}[/] · "
        f"n_labels=[bold]{summary['n_labels']}[/] · "
        f"loss=[bold]{summary['loss_final']:.4f}[/] · "
        f"||A-I||=[bold]{summary['drift_a']:.3f}[/] · "
        f"||b||=[bold]{summary['drift_b']:.3f}[/]"
    )


@calibrate_group.command("backend")
def calibrate_backend() -> None:
    """Show which ML backend would be auto-selected on this machine."""
    be = calibrate.detect_backend()
    console.print(
        f"[bold]auto-detected backend[/]: [cyan]{be}[/] "
        f"({calibrate.describe_backend(be)})"
    )


@calibrate_group.command("history")
@click.option("--user", "-u", required=True)
def calibrate_history(user: str) -> None:
    """Show the full calibration history for a user.

    Every call to `calibrate fit` appends a versioned row. Watching
    the drift (||A−I||, ||b||) grow as the user adds labels makes
    the personalization story tangible.
    """
    if not db.ping():
        console.print("[red]CedarDB unreachable.[/]")
        raise click.Abort()
    user_id = reco.get_or_create_user(user)
    df = calibrate.history(user_id)
    if df.empty:
        console.print(f"[dim]no calibration history yet for {user}[/]")
        return
    table = Table(title=f"{user}'s calibration history ({len(df)} fits)")
    table.add_column("version", justify="right")
    table.add_column("n_labels", justify="right")
    table.add_column("backend")
    table.add_column("loss", justify="right")
    table.add_column("λ_A", justify="right")
    table.add_column("λ_B", justify="right")
    table.add_column("fit_at")
    for _, row in df.iterrows():
        table.add_row(
            str(row["version"]),
            str(row["n_labels"]),
            str(row.get("backend", "?")),
            f"{row['loss_final']:.4f}",
            f"{row['lambda_a']:.0f}",
            f"{row['lambda_b']:.0f}",
            str(row["fit_at"])[:19],
        )
    console.print(table)


@calibrate_group.command("labels")
@click.option("--user", "-u", required=True)
def calibrate_labels(user: str) -> None:
    """Show the labels you've recorded so far."""
    if not db.ping():
        console.print("[red]CedarDB unreachable.[/]")
        raise click.Abort()
    user_id = reco.get_or_create_user(user)
    df = reco.get_labels(user_id)
    if df.empty:
        console.print(f"[dim]no labels yet for {user}[/]")
        return
    placeholders = ",".join(f"'{w}'" for w in df["wine_id"])
    wines = pd.read_sql(
        f"SELECT wine_id, producer_display, wine_display, vintage "
        f"FROM wines WHERE wine_id IN ({placeholders})",
        db.engine(),
    )
    joined = df.merge(wines, on="wine_id")
    table = Table(title=f"{user}'s labels ({len(joined)})")
    table.add_column("wine")
    table.add_column("description")
    for _, row in joined.iterrows():
        wine_str = f"{row['producer_display']} {row['wine_display']} ({row['vintage']})"
        table.add_row(wine_str, str(row["description"]))
    console.print(table)


@main.command("recommend")
@click.argument("query")
@click.option("--user", "-u", default=None, help="Personalized for this user.")
@click.option("-k", type=int, default=10)
@click.option("--country", default=None)
@click.option("--variety", default=None)
@click.option(
    "--alpha", type=float, default=0.6,
    help="Dense weight in hybrid score [0, 1]. 1=dense only, 0=sparse only."
)
def recommend_cmd(
    query: str,
    user: str | None,
    k: int,
    country: str | None,
    variety: str | None,
    alpha: float,
) -> None:
    """Find top-k wines matching a free-text query."""
    if not db.ping():
        console.print("[red]CedarDB unreachable.[/]")
        raise click.Abort()
    user_id = reco.get_or_create_user(user) if user else None
    filters: dict[str, object] = {}
    if country:
        filters["country"] = country
    if variety:
        filters["variety"] = variety
    results = reco.recommend(
        user_id=user_id,
        query=query,
        k=k,
        filters=filters if filters else None,
        alpha=alpha,
    )
    if results.empty:
        console.print("[dim]no results[/]")
        return
    style = (
        f"[cyan]personalized for {user}[/]"
        if user and user_id and reco.load_projection(user_id) is not None
        else "[dim]generic (no user calibration)[/]"
    )
    console.print(
        f"[bold]recommendations for[/] '[cyan]{query}[/]' · {style}"
    )
    table = Table()
    table.add_column("#", justify="right", style="dim")
    table.add_column("score", justify="right")
    table.add_column("dense", justify="right", style="dim")
    table.add_column("sparse", justify="right", style="dim")
    table.add_column("producer")
    table.add_column("wine")
    table.add_column("vintage", justify="right")
    table.add_column("variety")
    table.add_column("country")
    for i, row in results.iterrows():
        table.add_row(
            str(i + 1),
            f"{row['similarity']:.3f}",
            f"{row.get('dense_sim', 0):.3f}",
            f"{row.get('sparse_sim', 0):.3f}",
            str(row["producer_display"]),
            str(row["wine_display"]),
            str(row.get("vintage", "")),
            str(row.get("variety", "")),
            str(row.get("country", "")),
        )
    console.print(table)


@main.command("clusters")
@click.option("-k", type=int, default=16)
@click.option("--examples", type=int, default=3, help="Examples per cluster.")
def clusters_cmd(k: int, examples: int) -> None:
    """Show a summary of the learned wine clusters."""
    if not db.ping():
        console.print("[red]CedarDB unreachable.[/]")
        raise click.Abort()
    df = cluster.summarize(k=k, top_n_examples=examples)
    table = Table(title=f"WineTone clusters (k={k})")
    for col in ("cluster_id", "n_wines", "top_varieties", "top_countries", "examples"):
        table.add_column(col)
    for _, row in df.iterrows():
        table.add_row(
            str(row["cluster_id"]),
            f"{row['n_wines']:,}",
            str(row["top_varieties"]),
            str(row["top_countries"]),
            str(row["examples"]),
        )
    console.print(table)


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
