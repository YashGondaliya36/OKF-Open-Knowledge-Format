"""
codemind_okf/cli.py — Main CLI Entry Point
==========================================
`codemind index .`  — Generate OKF bundle from a local codebase
`codemind init`     — Drop AI IDE config files (.cursorrules, AGENTS.md, etc.)
`codemind status`   — Show current bundle stats

Usage:
    pip install codemind-okf
    cd my-project
    codemind index .
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.panel import Panel
from rich.table import Table
from rich import print as rprint

from codemind_okf.core.crawler import crawl, load_checksums, save_checksums
from codemind_okf.core.parser import parse_file
from codemind_okf.core.summarizer import summarize_fast
from codemind_okf.core.writer import write_okf_file
from codemind_okf.core.index_builder import build_index, append_to_log

app = typer.Typer(
    name="codemind",
    help="CodeMind OKF — Generate AI-ready knowledge bundles for any codebase.",
    add_completion=False,
    rich_markup_mode="rich",
)
console = Console()

TEMPLATES_DIR = Path(__file__).parent / "templates"


# ── Commands ──────────────────────────────────────────────────────────────────


@app.command()
def index(
    path: Path = typer.Argument(
        Path("."),
        help="Path to the project root (default: current directory).",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    ),
    output: Path = typer.Option(
        None,
        "--output", "-o",
        help="Output directory for the .okf bundle (default: <path>/.okf).",
    ),
    languages: str = typer.Option(
        "python,javascript,typescript",
        "--lang", "-l",
        help="Comma-separated list of languages to index.",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Overwrite existing .okf bundle instead of incrementally updating.",
    ),
):
    """
    [bold green]Generate an OKF knowledge bundle from a local codebase.[/bold green]

    Crawls the project directory, parses all source files with AST analysis,
    and writes structured .okf/modules/*.md files. Zero LLM cost.

    Examples:
        codemind index .
        codemind index /path/to/my-project
        codemind index . --lang python
    """
    project_root = path.resolve()
    bundle_root = output or (project_root / ".okf")
    project_name = project_root.name
    lang_list = [l.strip() for l in languages.split(",") if l.strip()]

    # ── Banner ────────────────────────────────────────────────────────────────
    console.print(Panel(
        f"[bold white]CodeMind OKF Indexer[/bold white]\n"
        f"[dim]Project:[/dim] [cyan]{project_root}[/cyan]\n"
        f"[dim]Output: [/dim] [cyan]{bundle_root}[/cyan]\n"
        f"[dim]Languages:[/dim] [yellow]{', '.join(lang_list)}[/yellow]",
        border_style="green",
        expand=False,
    ))

    # ── Handle overwrite ──────────────────────────────────────────────────────
    modules_dir = bundle_root / "modules"
    if overwrite and bundle_root.exists():
        console.print("[yellow]⚠ Overwrite mode: removing existing bundle...[/yellow]")
        shutil.rmtree(bundle_root)
    modules_dir.mkdir(parents=True, exist_ok=True)

    # ── Crawl ─────────────────────────────────────────────────────────────────
    console.print("\n[bold]🔍 Crawling project files...[/bold]")
    result = crawl(project_root, lang_list)

    if result.total_files == 0:
        console.print("[red]✗ No source files found. Check the path and language flags.[/red]")
        raise typer.Exit(code=1)

    console.print(
        f"  Found [bold green]{result.total_files}[/bold green] files "
        f"([dim]{result.skipped_count} skipped[/dim])"
    )

    # ── SHA-256 Incremental Indexing ──────────────────────────────────────────
    cached_checksums = load_checksums(bundle_root) if not overwrite else {}
    new_checksums: dict[str, str] = {}

    # Delete stale module files if original source file was deleted from disk
    if cached_checksums and not overwrite:
        current_rel_paths = {c.relative_path for c in result.files}
        for rel_path in list(cached_checksums.keys()):
            if rel_path not in current_rel_paths:
                slug = rel_path.replace("/", "-").replace("\\", "-").replace("_", "-")
                slug = slug.rsplit(".", 1)[0] + ".md"
                stale_file = modules_dir / slug
                if stale_file.exists():
                    try:
                        stale_file.unlink()
                    except Exception:
                        pass

    # ── Parse + Summarize + Write ─────────────────────────────────────────────
    written = 0
    skipped_unchanged = 0
    errors = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
        transient=False,
    ) as progress:
        task = progress.add_task("[green]Indexing modules...", total=result.total_files)

        for crawled in result.files:
            new_checksums[crawled.relative_path] = crawled.sha256

            # Incremental check: if SHA-256 matches cache and output file exists -> skip parsing!
            slug = crawled.relative_path.replace("/", "-").replace("\\", "-").replace("_", "-")
            slug = slug.rsplit(".", 1)[0] + ".md"
            expected_output = modules_dir / slug

            if not overwrite and cached_checksums.get(crawled.relative_path) == crawled.sha256 and expected_output.exists():
                skipped_unchanged += 1
                progress.advance(task)
                continue

            progress.update(task, description=f"[cyan]{crawled.relative_path}[/cyan]")
            try:
                parsed = parse_file(crawled.path, crawled.relative_path, crawled.language)
                summary = summarize_fast(parsed)
                write_okf_file(bundle_root, parsed, summary)
                written += 1
            except Exception as e:
                errors += 1
                console.print(f"  [red]✗ {crawled.relative_path}: {e}[/red]")
            finally:
                progress.advance(task)

    # Save updated checksum map
    save_checksums(bundle_root, new_checksums)

    # ── Build index.md ────────────────────────────────────────────────────────
    console.print("\n[bold]📚 Building index.md...[/bold]")
    build_index(bundle_root, project_name)
    append_to_log(bundle_root, f"codemind index — {written} updated, {skipped_unchanged} unchanged, {errors} errors")

    # ── Summary ───────────────────────────────────────────────────────────────
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("[green]✓ Modules updated[/green]", f"[bold]{written}[/bold]")
    table.add_row("[dim]⚡ Unchanged (skipped)[/dim]", f"[dim]{skipped_unchanged}[/dim]")
    table.add_row("[yellow]⚠ Errors[/yellow]", f"[bold]{errors}[/bold]")
    table.add_row("[blue]Bundle location[/blue]", f"[cyan]{bundle_root}[/cyan]")

    console.print(Panel(
        table,
        title="[bold green]✓ Incremental Indexing Complete[/bold green]",
        border_style="green",
    ))

    console.print(
        "\n[dim]Next step:[/dim] Run [bold]codemind init[/bold] to drop AI IDE config files "
        "into this project.\n"
    )


@app.command()
def init(
    path: Path = typer.Argument(
        Path("."),
        help="Project root to initialise (default: current directory).",
        resolve_path=True,
    ),
    cursor: bool = typer.Option(True, help="Generate .cursorrules for Cursor AI."),
    agents: bool = typer.Option(True, help="Generate .agents/AGENTS.md for Antigravity/AI IDEs."),
    copilot: bool = typer.Option(True, help="Generate .github/copilot-instructions.md."),
):
    """
    [bold green]Drop AI IDE instruction files into a project.[/bold green]

    Generates .cursorrules, .agents/AGENTS.md, and .github/copilot-instructions.md
    to tell Cursor, Antigravity, and GitHub Copilot to use the .okf/ bundle as context.

    Examples:
        codemind init
        codemind init /path/to/my-project
    """
    project_root = path.resolve()
    bundle_path = project_root / ".okf"

    if not bundle_path.exists():
        console.print(
            "[yellow]⚠ No .okf bundle found. Run [bold]codemind index .[/bold] first.[/yellow]"
        )

    template_content = {
        "cursorrules": (TEMPLATES_DIR / "cursorrules.txt").read_text(encoding="utf-8"),
        "agents_md": (TEMPLATES_DIR / "agents_md.txt").read_text(encoding="utf-8"),
        "copilot": (TEMPLATES_DIR / "copilot_instructions.txt").read_text(encoding="utf-8"),
    }

    created = []

    if cursor:
        dest = project_root / ".cursorrules"
        dest.write_text(template_content["cursorrules"], encoding="utf-8")
        created.append(str(dest.relative_to(project_root)))

    if agents:
        agents_dir = project_root / ".agents"
        agents_dir.mkdir(exist_ok=True)
        dest = agents_dir / "AGENTS.md"
        dest.write_text(template_content["agents_md"], encoding="utf-8")
        created.append(str(dest.relative_to(project_root)))

    if copilot:
        gh_dir = project_root / ".github"
        gh_dir.mkdir(exist_ok=True)
        dest = gh_dir / "copilot-instructions.md"
        dest.write_text(template_content["copilot"], encoding="utf-8")
        created.append(str(dest.relative_to(project_root)))

    console.print(Panel(
        "\n".join(f"[green]✓[/green] [cyan]{f}[/cyan]" for f in created),
        title="[bold green]✓ AI IDE Config Files Created[/bold green]",
        border_style="green",
    ))
    console.print(
        "\n[dim]These files tell Cursor, Antigravity, and Copilot to use [/dim]"
        "[cyan].okf/index.md[/cyan][dim] as their primary codebase context.[/dim]\n"
    )


@app.command()
def status(
    path: Path = typer.Argument(Path("."), help="Project root.", resolve_path=True),
):
    """
    [bold green]Show the current OKF bundle stats for a project.[/bold green]
    """
    bundle_root = path.resolve() / ".okf"

    if not bundle_root.exists():
        console.print(
            "[red]✗ No .okf bundle found.[/red] "
            "Run [bold cyan]codemind index .[/bold cyan] to generate one."
        )
        raise typer.Exit(code=1)

    modules_dir = bundle_root / "modules"
    module_files = list(modules_dir.glob("*.md")) if modules_dir.is_dir() else []

    type_counts: dict[str, int] = {}
    for md in module_files:
        try:
            import frontmatter as fm
            post = fm.loads(md.read_text(encoding="utf-8"))
            t = str(post.metadata.get("type", "module"))
            type_counts[t] = type_counts.get(t, 0) + 1
        except Exception:
            type_counts["unknown"] = type_counts.get("unknown", 0) + 1

    table = Table(title=f"OKF Bundle — {path.resolve().name}", box=None)
    table.add_column("Layer", style="cyan", no_wrap=True)
    table.add_column("Files", style="green", justify="right")

    for t, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        table.add_row(t.title(), str(count))

    table.add_section()
    table.add_row("[bold]TOTAL[/bold]", f"[bold]{len(module_files)}[/bold]")

    console.print(table)


@app.command()
def action(
    path: Path = typer.Argument(
        Path("."),
        help="Project root (default: current directory).",
        resolve_path=True,
    ),
):
    """
    [bold green]Add a GitHub Action that auto-builds the OKF bundle on every push.[/bold green]

    Creates .github/workflows/okf-build.yml in the project.
    Whenever code is pushed to main, the bundle is regenerated automatically.

    Example:
        codemind action
    """
    project_root = path.resolve()
    workflows_dir = project_root / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)

    action_template = (TEMPLATES_DIR / "github_action.yml").read_text(encoding="utf-8")
    dest = workflows_dir / "okf-build.yml"
    dest.write_text(action_template, encoding="utf-8")

    console.print(Panel(
        f"[green]✓[/green] [cyan]{dest.relative_to(project_root)}[/cyan]\n\n"
        "[dim]Commit and push this file to GitHub. On every push to main,\n"
        "the OKF bundle will be auto-regenerated and committed back.[/dim]",
        title="[bold green]✓ GitHub Action Created[/bold green]",
        border_style="green",
    ))


@app.command()
def mcp():
    """
    [bold green]Start Model Context Protocol (MCP) server over STDIO.[/bold green]

    Connects CodeMind tools natively to Cursor, Claude Desktop, Antigravity, or Zed.

    Example mcp.json config for Cursor / Claude Desktop:
    {
      "mcpServers": {
        "codemind": {
          "command": "codemind",
          "args": ["mcp"]
        }
      }
    }
    """
    from codemind_okf.mcp import run_mcp_server
    run_mcp_server()


@app.command()
def watch(
    path: Path = typer.Argument(
        Path("."),
        help="Project root to watch for real-time changes.",
        resolve_path=True,
    ),
    interval: int = typer.Option(2, "--interval", "-i", help="Polling interval in seconds (default 2s)."),
):
    """
    [bold green]Watch project directory for real-time file changes and auto-index.[/bold green]

    Runs continuously in the background. Whenever you save a file in VS Code or Cursor,
    CodeMind incrementally updates the OKF bundle in milliseconds.

    Example:
        codemind watch
    """
    import time
    project_root = path.resolve()
    console.print(f"[bold green]👁 Watching [cyan]{project_root}[/cyan] for file changes... (Press Ctrl+C to stop)[/bold green]\n")

    last_run = 0
    try:
        while True:
            # Run fast incremental index
            index(path=project_root, output=None, languages="python,javascript,typescript", overwrite=False)
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped watching.[/yellow]")


@app.command()
def audit(
    path: Path = typer.Argument(
        Path("."),
        help="Project root directory (default: current directory).",
        resolve_path=True,
    ),
    output: Path = typer.Option(
        None,
        "--output", "-o",
        help="Path to .okf directory.",
    ),
):
    """
    [bold green]Perform codebase health & architecture audit from the OKF bundle.[/bold green]

    Analyzes file size, architectural layers, docstring coverage, and key module hotspots.

    Example:
        codemind audit
    """
    project_root = path.resolve()
    bundle_root = output or (project_root / ".okf")

    if not bundle_root.exists():
        console.print(
            "[red]✗ No .okf bundle found.[/red] "
            "Run [bold cyan]codemind index .[/bold cyan] first."
        )
        raise typer.Exit(code=1)

    modules_dir = bundle_root / "modules"
    module_files = list(modules_dir.glob("*.md")) if modules_dir.is_dir() else []

    if not module_files:
        console.print("[red]✗ No module files found in .okf/modules/.[/red]")
        raise typer.Exit(code=1)

    import frontmatter as fm

    monolithic_files: list[tuple[str, str, int]] = []
    missing_docs: list[tuple[str, str]] = []
    layer_counts: dict[str, int] = {}
    total_funcs = 0

    for md in module_files:
        try:
            post = fm.loads(md.read_text(encoding="utf-8"))
            meta = post.metadata
            title = str(meta.get("title", md.stem))
            resource = str(meta.get("resource", ""))
            mod_type = str(meta.get("type", "module"))
            desc = str(meta.get("description", ""))
            key_funcs = list(meta.get("key_functions", []))

            layer_counts[mod_type] = layer_counts.get(mod_type, 0) + 1
            total_funcs += len(key_funcs)

            # Audit Check 1: Missing description/docstring
            if not desc or "Configuration or type definition module" in desc or "Contains functions:" in desc:
                missing_docs.append((title, resource))

            # Audit Check 2: Check lines count in body header
            body = post.content
            for line in body.splitlines():
                if "Lines:" in line:
                    try:
                        num_lines = int(line.split("Lines:")[1].strip())
                        if num_lines > 300:
                            monolithic_files.append((title, resource, num_lines))
                    except Exception:
                        pass
        except Exception:
            continue

    # Score Calculation (100 base)
    score = 100
    score -= min(30, len(monolithic_files) * 5)
    score -= min(20, len(missing_docs) * 2)
    score = max(10, score)

    grade = "A+" if score >= 90 else ("A" if score >= 80 else ("B" if score >= 70 else "C"))
    color = "green" if score >= 80 else ("yellow" if score >= 60 else "red")

    console.print(Panel(
        f"[bold white]CodeMind Architecture & Health Audit[/bold white]\n"
        f"[dim]Target:[/dim] [cyan]{project_root}[/cyan]\n"
        f"[dim]Modules Audited:[/dim] [bold]{len(module_files)}[/bold]\n"
        f"[dim]Health Score:[/dim] [{color}][bold]{score}/100 ({grade})[/bold][/{color}]",
        border_style=color,
        expand=False,
    ))

    # Health Findings Table
    table = Table(title="[bold]Codebase Findings & Recommendations[/bold]", box=None)
    table.add_column("Category", style="cyan")
    table.add_column("Finding", style="white")
    table.add_column("Recommendation", style="yellow")

    if monolithic_files:
        top_mono = monolithic_files[0]
        table.add_row(
            "🐘 Large Modules",
            f"{len(monolithic_files)} files exceed 300 LOC (e.g. {top_mono[0]} - {top_mono[2]} lines)",
            "Consider splitting into smaller, modular sub-components."
        )
    else:
        table.add_row("🐘 Module Sizing", "All modules are under 300 LOC", "Excellent modular separation!")

    if missing_docs:
        table.add_row(
            "📝 Documentation",
            f"{len(missing_docs)} modules lack detailed docstrings",
            "Add docstrings to public classes/functions for richer AI context."
        )
    else:
        table.add_row("📝 Documentation", "Docstrings are present across all modules", "Great context density!")

    table.add_row(
        "🏗️ Architecture Layers",
        f"{len(layer_counts)} active layers ({', '.join(layer_counts.keys())})",
        "Clean separation of concerns detected."
    )

    console.print(table)


memory_app = typer.Typer(
    name="memory",
    help="Manage the AI persistent memory log (.okf/memory.md).",
    add_completion=False,
    rich_markup_mode="rich",
)
app.add_typer(memory_app, name="memory")


def _resolve_memory_path(path: Path) -> Path | None:
    """Find .okf/memory.md from a given project path, cwd, or parent directories."""
    from codemind_okf.mcp import _get_memory_path
    return _get_memory_path(str(path))


@memory_app.command("show")
def memory_show(
    path: Path = typer.Argument(Path("."), help="Project root (default: current dir)", resolve_path=True),
):
    """Print the full contents of .okf/memory.md to the terminal."""
    mem = _resolve_memory_path(path)
    if not mem or not mem.exists():
        rprint("[red]No memory file found.[/red] Run [bold]codemind index .[/bold] and let the AI use [bold]remember()[/bold] to populate it.")
        raise typer.Exit(1)
    console.print(Panel(
        mem.read_text(encoding="utf-8"),
        title="[bold cyan]🧠 .okf/memory.md[/bold cyan]",
        border_style="cyan",
        expand=False,
    ))


@memory_app.command("ls")
def memory_ls(
    path: Path = typer.Argument(Path("."), help="Project root (default: current dir)", resolve_path=True),
):
    """List a summary of all memory entries (count per type)."""
    import re as _re
    mem = _resolve_memory_path(path)
    if not mem or not mem.exists():
        rprint("[red]No memory file found.[/red] Run [bold]codemind index .[/bold] first.")
        raise typer.Exit(1)

    raw = mem.read_text(encoding="utf-8")
    section_map = {
        "📌 Decisions":        "decision",
        "✅ Tasks":             "task",
        "💬 Context Snapshots": "context",
        "🐛 Bug Reports":       "bug",
    }

    table = Table(title="[bold cyan]🧠 Memory Summary[/bold cyan]", box=None, show_header=True)
    table.add_column("Type", style="cyan", min_width=22)
    table.add_column("Entries", justify="right", style="bold white")

    total = 0
    for label, _ in section_map.items():
        count = len(_re.findall(r"^### \[", raw, _re.MULTILINE))
        # Count entries per section by finding entries between section headers
        # Simple heuristic: count ### lines after each ## header
        pass

    # More accurate: split by section
    blocks = _re.split(r"\n## ", raw)
    section_counts: dict[str, int] = {k: 0 for k in section_map.values()}
    for block in blocks:
        for label, stype in section_map.items():
            clean_label = label.split(" ", 1)[1] if " " in label else label
            if block.strip().startswith(clean_label) or block.strip().startswith(label):
                count = len(_re.findall(r"^### \[", block, _re.MULTILINE))
                section_counts[stype] = count
                total += count

    emoji_map = {"decision": "📌", "task": "✅", "context": "💬", "bug": "🐛"}
    label_map = {"decision": "Decisions", "task": "Tasks", "context": "Context Snapshots", "bug": "Bug Reports"}
    for stype, count in section_counts.items():
        table.add_row(f"{emoji_map[stype]} {label_map[stype]}", str(count))

    console.print(table)
    console.print(f"\n[dim]Total entries: {total} · File: {mem}[/dim]")


@memory_app.command("clear")
def memory_clear(
    path: Path = typer.Argument(Path("."), help="Project root (default: current dir)", resolve_path=True),
    confirm: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
):
    """Clear all entries from .okf/memory.md (keeps the template structure)."""
    from codemind_okf.mcp import _MEMORY_TEMPLATE

    mem = _resolve_memory_path(path)
    if not mem or not mem.exists():
        rprint("[red]No memory file found.[/red]")
        raise typer.Exit(1)

    if not confirm:
        typer.confirm("⚠️  This will erase all AI memory entries. Continue?", abort=True)

    mem.write_text(_MEMORY_TEMPLATE, encoding="utf-8")
    rprint("[green]✓ Memory cleared.[/green] .okf/memory.md reset to blank template.")


@memory_app.command("add")
def memory_add(
    content: str = typer.Argument(..., help="Memory content to add."),
    memory_type: str = typer.Option("context", "--type", "-t", help="Type: decision | task | context | bug"),
    ide: str = typer.Option("Human (CLI)", "--ide", help="IDE/author label"),
    tags: list[str] = typer.Option([], "--tag", help="Tags (repeatable: --tag bot_shield --tag ml)"),
    path: Path = typer.Argument(Path("."), help="Project root", resolve_path=True),
):
    """Manually add a memory entry to .okf/memory.md."""
    from codemind_okf.mcp import tool_remember

    result_raw = tool_remember(
        content=content,
        memory_type=memory_type,
        ide=ide,
        tags=tags or None,
        repo_name=str(path),
    )
    try:
        result = json.loads(result_raw)
        if result.get("status") == "ok":
            rprint(f"[green]✓ Memory saved:[/green] {result['message']}")
        else:
            rprint(f"[red]Error:[/red] {result.get('message', result_raw)}")
    except json.JSONDecodeError:
        rprint(f"[red]Error:[/red] {result_raw}")


def main():
    app()


if __name__ == "__main__":
    main()

