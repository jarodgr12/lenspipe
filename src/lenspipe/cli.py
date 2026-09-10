"""The ``lenspipe`` command line."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer

from lenspipe import __version__
from lenspipe.config import CONFIG_FILENAME, LenspipeConfig, load_config
from lenspipe.progress import default_reporter
from lenspipe.project import Layout, inventory

app = typer.Typer(
    help="DifMAP spectral pipeline: self-cal (stage1), per-channel fits (stage2), science plots (stage3).",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
)

ProjectArg = Annotated[
    Path, typer.Argument(help="Project root containing inputs/. Default: current directory.")
]
ConfigOpt = Annotated[
    Path | None, typer.Option("--config", help=f"Config file (default <project>/{CONFIG_FILENAME}).")
]
EpochOpt = Annotated[
    list[str] | None, typer.Option("--epoch", "-e", help="Process only this epoch; repeatable.")
]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"lenspipe {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool | None,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = None,
) -> None:
    """lenspipe command line."""


def _load(project: Path, config_path: Path | None) -> tuple[Path, LenspipeConfig]:
    import tomllib

    from pydantic import ValidationError

    root = Layout.at(project).root
    try:
        return root, load_config(root, config_path)
    except tomllib.TOMLDecodeError as exc:
        typer.echo(f"ERROR: {config_path or root / CONFIG_FILENAME} is not valid TOML: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except ValidationError as exc:
        typer.echo(f"ERROR: invalid configuration in {config_path or root / CONFIG_FILENAME}:", err=True)
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"])
            typer.echo(f"  {location}: {error['msg']}", err=True)
        raise typer.Exit(code=1) from exc


def _exit_for(results: list, *, dry_run: bool = False) -> int:
    failed = [r for r in results if not r.ok and r.status not in {"dry_run"}]
    if dry_run:
        return 0
    return 0 if not failed else 2


# ---------------------------------------------------------------------------


@app.command()
def init(
    project: ProjectArg = Path("."),
    inputs: Annotated[Path | None, typer.Option("--inputs", help="Directory holding <source>.gmod and <source>.<epoch>.uvfits files to link into inputs/.")] = None,
    copy: Annotated[bool, typer.Option("--copy", help="Copy input files instead of symlinking them.")] = False,
    source: Annotated[str | None, typer.Option("--source", help="Source name recorded in the config.")] = None,
    detect: Annotated[bool, typer.Option("--detect/--no-detect", help="Detect DifMAP and CASA and write them into the config.")] = True,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing config file.")] = False,
) -> None:
    """Create a project: lenspipe.toml with detected tools, inputs/ populated from --inputs."""
    from lenspipe.config import render_toml
    from lenspipe.doctor import detect_casa, detect_difmap

    layout = Layout.at(project)
    layout.root.mkdir(parents=True, exist_ok=True)
    layout.inputs.mkdir(exist_ok=True)
    if layout.config_path.exists() and not force:
        typer.echo(f"{layout.config_path} already exists (use --force to replace).")
        raise typer.Exit(code=1)

    linked = 0
    if inputs is not None:
        inputs = inputs.expanduser().resolve()
        if not inputs.is_dir():
            typer.echo(f"ERROR: --inputs is not a directory: {inputs}", err=True)
            raise typer.Exit(code=1)
        for path in sorted(list(inputs.glob("*.uvfits")) + list(inputs.glob("*.gmod"))):
            target = layout.inputs / path.name
            if target.exists() or target.is_symlink():
                continue
            if copy:
                shutil.copy2(path, target)
            else:
                target.symlink_to(path)
            linked += 1
        if source is None:
            models = sorted(inputs.glob("*.gmod"))
            if len(models) == 1:
                source = models[0].stem

    difmap_found = detect_difmap() if detect else None
    casa_found = detect_casa() if detect else None
    repo_casa = Path(__file__).resolve().parents[2] / "casa"
    text = render_toml(
        source=source,
        difmap_executable=difmap_found,
        casa_interpreter=casa_found,
        casa_script=str(repo_casa / "calibration.py") if casa_found and repo_casa.is_dir() else None,
    )
    layout.config_path.write_text(text, encoding="utf-8")
    typer.echo(f"Wrote {layout.config_path}")
    if inputs is not None:
        typer.echo(f"{'Copied' if copy else 'Linked'} {linked} file(s) into {layout.inputs}")
    else:
        typer.echo(f"Place <source>.gmod and <source>.<epoch>.uvfits files in {layout.inputs}")
    if detect:
        typer.echo(f"DifMAP: {difmap_found or 'not found (set [project.difmap] executable)'}")
        typer.echo(f"CASA:   {casa_found or 'not found (calibration disabled)'}")
    typer.echo(f"Next: lenspipe doctor {layout.root}   then   lenspipe ui {layout.root}")


@app.command()
def doctor(
    project: Annotated[Path | None, typer.Argument(help="Project root to check as well as the machine.")] = None,
    config: ConfigOpt = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check Python, DifMAP, CASA, fonts, memory and the project; say how to fix what is missing."""
    from lenspipe.doctor import run_checks

    cfg = None
    root = None
    if project is not None:
        root = Layout.at(project).root
        try:
            cfg = load_config(root, config)
        except Exception as exc:  # noqa: BLE001 - doctor must still report
            typer.echo(f"config: could not load ({exc}); checking with defaults", err=True)
    checks = run_checks(root, cfg)
    if as_json:
        typer.echo(json.dumps([c.to_dict() for c in checks], indent=2))
    else:
        marks = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
        for check in checks:
            typer.echo(f"{marks[check.status]}  {check.name:<10} {check.detail}")
            if check.fix:
                typer.echo(f"      fix: {check.fix}")
    if any(c.status == "fail" for c in checks):
        raise typer.Exit(code=1)


@app.command()
def compare(
    left: Annotated[Path, typer.Argument(help="Reference project (for example the legacy run).")],
    right: Annotated[Path, typer.Argument(help="Project to compare against it (the v2 run).")],
    epoch: EpochOpt = None,
    product: Annotated[list[str] | None, typer.Option("--product", "-p")] = None,
    stages: Annotated[str, typer.Option("--stages", help="Comma-separated subset of 1,2,3.")] = "1,2,3",
    rtol: Annotated[float, typer.Option("--rtol")] = 1e-10,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Compare Stage 1-3 products of two projects; exit 4 if anything differs."""
    from lenspipe.compare import compare_projects

    diffs = compare_projects(
        left, right, epochs=set(epoch) if epoch else None,
        products=set(product) if product else None,
        stages={int(s) for s in stages.split(",") if s.strip()}, rtol=rtol,
    )
    if as_json:
        typer.echo(json.dumps([d.to_dict() for d in diffs], indent=2))
    else:
        if not diffs:
            typer.echo("Nothing to compare: the reference project has no stage products.")
        for diff in diffs:
            line = f"stage{diff.stage}  {diff.status:<10} {diff.item}"
            if diff.detail:
                line += f"  ({diff.detail})"
            if diff.columns:
                line += "  columns: " + ", ".join(diff.columns[:8]) + (" ..." if len(diff.columns) > 8 else "")
            typer.echo(line)
        counts: dict[str, int] = {}
        for diff in diffs:
            counts[diff.status] = counts.get(diff.status, 0) + 1
        typer.echo("summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if any(not d.ok for d in diffs):
        raise typer.Exit(code=4)


@app.command()
def describe(project: ProjectArg = Path("."), config: ConfigOpt = None) -> None:
    """Print the effective configuration as JSON."""
    root, cfg = _load(project, config)
    typer.echo(json.dumps({"project_root": str(root), **cfg.dump()}, indent=2))


@app.command("inventory")
def inventory_cmd(
    project: ProjectArg = Path("."),
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Show which epochs have inputs, Stage 1, Stage 2 and Stage 3 products."""
    summary = inventory(Layout.at(project).root)
    if as_json:
        typer.echo(json.dumps(summary, indent=2))
        return
    typer.echo(f"Project: {summary['root']}  (config: {'yes' if summary['config_present'] else 'defaults'})")
    typer.echo(f"{'epoch':<22}{'input':<8}{'stage1':<10}{'stage2':<28}stage3")
    for row in summary["epochs"]:
        s2 = ",".join(p["product"] for p in row["stage2"]) or "-"
        s3 = ",".join(
            (p["product"] or "?") + (f"/{p['analysis_tag']}" if p["analysis_tag"] else "")
            for p in row["stage3"]
        ) or "-"
        typer.echo(
            f"{row['source'] + '.' + row['epoch']:<22}"
            f"{'yes' if row['input'] else '-':<8}"
            f"{'yes' if row['stage1'] else '-':<10}"
            f"{s2:<28}{s3}"
        )
    for item in summary["combined"]:
        typer.echo(f"combined  {item['source']}.{item['product']}  visits={item['n_visits']}  {item['path']}")


# ---------------------------------------------------------------------------


@app.command()
def stage1(
    project: ProjectArg = Path("."),
    config: ConfigOpt = None,
    epoch: EpochOpt = None,
    overwrite: Annotated[bool, typer.Option("--overwrite")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print DifMAP commands only.")] = False,
    workers: Annotated[int | None, typer.Option("--workers", help="Epochs in parallel.")] = None,
    difmap: Annotated[str | None, typer.Option("--difmap", help="DifMAP executable.")] = None,
    final_if_selfcal: Annotated[
        bool | None, typer.Option("--final-if-selfcal/--no-final-if-selfcal")
    ] = None,
) -> None:
    """Self-calibrate, model-fit and image each epoch (inputs/ -> stage1/)."""
    from lenspipe.stage1 import run_stage1

    root, cfg = _load(project, config)
    if difmap:
        cfg = cfg.with_overrides(project={"difmap": {**cfg.project.difmap.model_dump(), "executable": difmap}})
    if final_if_selfcal is not None:
        cfg = cfg.with_overrides(
            stage1={"final_if_selfcal": {**cfg.stage1.final_if_selfcal.model_dump(), "enabled": final_if_selfcal}}
        )
    try:
        results = run_stage1(
            root, cfg, epochs=set(epoch) if epoch else None, overwrite=overwrite,
            dry_run=dry_run, workers=workers, reporter=default_reporter(),
        )
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _summary("Stage 1", results)
    raise typer.Exit(code=_exit_for(results, dry_run=dry_run))


@app.command()
def stage2(
    project: ProjectArg = Path("."),
    config: ConfigOpt = None,
    epoch: EpochOpt = None,
    mode: Annotated[str | None, typer.Option("--mode", help="'channel' or 'if'.")] = None,
    channels_per_if: Annotated[int | None, typer.Option("--channels-per-if")] = None,
    exclude_edge_channels: Annotated[int | None, typer.Option("--exclude-edge-channels")] = None,
    channels: Annotated[str | None, typer.Option("--channels", help="e.g. '1-10,15' (channel mode).")] = None,
    shards: Annotated[str | None, typer.Option("--shards", help="DifMAP processes per epoch or 'auto'.")] = None,
    modelfit_iterations: Annotated[int | None, typer.Option("--modelfit-iterations")] = None,
    keep_models: Annotated[bool | None, typer.Option("--keep-models/--no-keep-models")] = None,
    plots: Annotated[bool | None, typer.Option("--plots/--no-plots", help="Write the quick-look PNGs.")] = None,
    error_bars: Annotated[bool | None, typer.Option("--error-bars/--no-error-bars", help="Error bars on the quick-look plots.")] = None,
    recover_from_log: Annotated[bool, typer.Option("--recover-from-log", help="Rebuild products from the existing log.")] = False,
    resume: Annotated[bool, typer.Option("--resume", help="Continue an interrupted run; finished shards are kept.")] = False,
    overwrite: Annotated[bool, typer.Option("--overwrite")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    workers: Annotated[int | None, typer.Option("--workers", help="Epochs in parallel.")] = None,
    difmap: Annotated[str | None, typer.Option("--difmap")] = None,
) -> None:
    """Fit the frozen model per channel or per IF (stage1/ -> stage2/)."""
    from lenspipe.stage2 import run_stage2

    root, cfg = _load(project, config)
    overrides = {
        "mode": mode,
        "channels_per_if": channels_per_if,
        "exclude_edge_channels": exclude_edge_channels,
        "channels": channels,
        "modelfit_iterations": modelfit_iterations,
        "keep_models": keep_models,
        "plot_spectrum": plots,
        "plot_error_bars": error_bars,
    }
    if shards is not None:
        overrides["shards"] = "auto" if shards == "auto" else int(shards)
    if mode == "channel" and channels is None and cfg.stage2.channels is None:
        overrides["channels"] = None
    if mode == "if":
        overrides["channels"] = None
    try:
        cfg = cfg.with_overrides(stage2=overrides)
        if mode == "if":
            data = cfg.stage2.model_dump()
            data["channels"] = None
            cfg = cfg.model_copy(update={"stage2": type(cfg.stage2).model_validate(data)})
        if difmap:
            cfg = cfg.with_overrides(project={"difmap": {**cfg.project.difmap.model_dump(), "executable": difmap}})
    except ValueError as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    try:
        results = run_stage2(
            root, cfg, epochs=set(epoch) if epoch else None, overwrite=overwrite, dry_run=dry_run,
            recover_from_log=recover_from_log, resume=resume, workers=workers,
            reporter=default_reporter(),
        )
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _summary("Stage 2", results)
    raise typer.Exit(code=_exit_for(results, dry_run=dry_run))


@app.command()
def stage3(
    project: ProjectArg = Path("."),
    config: ConfigOpt = None,
    epoch: EpochOpt = None,
    product: Annotated[list[str] | None, typer.Option("--product", "-p", help="Stage 2 product tag; repeatable.")] = None,
    error_source: Annotated[str | None, typer.Option("--error-source", help="'rms' or 'difmap'.")] = None,
    fit_method: Annotated[str | None, typer.Option("--fit-method", help="'least_squares' or 'emcee'.")] = None,
    reference_frequency: Annotated[float | None, typer.Option("--reference-frequency", help="GHz.")] = None,
    exclude_channels: Annotated[str | None, typer.Option("--exclude-channels")] = None,
    exclude_epoch_channels: Annotated[list[str] | None, typer.Option("--exclude-epoch-channels", help="EPOCH:SPEC; repeatable.")] = None,
    annotations: Annotated[bool | None, typer.Option("--annotations/--no-annotations")] = None,
    error_bars: Annotated[bool | None, typer.Option("--error-bars/--no-error-bars", help="Error bars on every figure.")] = None,
    formats: Annotated[str | None, typer.Option("--formats", help="Figure formats, e.g. 'png' or 'pdf,png'.")] = None,
    use_tex: Annotated[bool | None, typer.Option("--use-tex/--no-use-tex")] = None,
    overwrite: Annotated[bool, typer.Option("--overwrite")] = False,
    workers: Annotated[int | None, typer.Option("--workers", help="Plot processes.")] = None,
    list_products: Annotated[bool, typer.Option("--list-products", help="List Stage 2 products and exit.")] = False,
) -> None:
    """Fit spectra and flux ratios, write per-visit and combined plots (stage2/ -> stage3/)."""
    from lenspipe.stage3 import run_stage3
    from lenspipe.stage3.io import available_stage2_products

    root, cfg = _load(project, config)
    if list_products:
        try:
            products = available_stage2_products(root, set(epoch) if epoch else None)
        except Exception as exc:  # noqa: BLE001
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        for observation, tags in products.items():
            typer.echo(f"{observation}: {', '.join(tags)}")
        return
    try:
        cfg = cfg.with_overrides(
            stage3={
                "error_source": error_source,
                "fit_method": fit_method,
                "reference_frequency_ghz": reference_frequency,
                "exclude_channels": exclude_channels,
                "exclude_epoch_channels": exclude_epoch_channels,
                "annotations": annotations,
                "plot_error_bars": error_bars,
                "figure_formats": (
                    [f.strip() for f in formats.split(",") if f.strip()] if formats else None
                ),
                "use_tex": use_tex,
            }
        )
        summary = run_stage3(
            root, cfg, epochs=set(epoch) if epoch else None,
            products=set(product) if product else None, overwrite=overwrite,
            workers=workers, reporter=default_reporter(),
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    raise typer.Exit(code=0 if summary.ok else 2)


@app.command()
def run(
    project: ProjectArg = Path("."),
    config: ConfigOpt = None,
    stages: Annotated[str, typer.Option("--stages", help="Comma-separated subset of 1,2,3.")] = "1,2,3",
    epoch: EpochOpt = None,
    overwrite: Annotated[bool, typer.Option("--overwrite")] = False,
) -> None:
    """Run stages in order with the project configuration; stops at the first failing stage."""
    from lenspipe.stage1 import run_stage1
    from lenspipe.stage2 import product_tag, run_stage2
    from lenspipe.stage3 import run_stage3

    root, cfg = _load(project, config)
    wanted = [int(s) for s in stages.split(",") if s.strip()]
    epochs = set(epoch) if epoch else None
    reporter = default_reporter()
    try:
        if 1 in wanted:
            results = run_stage1(root, cfg, epochs=epochs, overwrite=overwrite, reporter=reporter)
            _summary("Stage 1", results)
            if _exit_for(results):
                raise typer.Exit(code=2)
        if 2 in wanted:
            results = run_stage2(root, cfg, epochs=epochs, overwrite=overwrite, reporter=reporter)
            _summary("Stage 2", results)
            if _exit_for(results):
                raise typer.Exit(code=2)
        if 3 in wanted:
            summary = run_stage3(
                root, cfg, epochs=epochs, products={product_tag(cfg.stage2)},
                overwrite=overwrite, reporter=reporter,
            )
            if not summary.ok:
                raise typer.Exit(code=2)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        typer.echo(f"ERROR: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command()
def update(
    check_only: Annotated[bool, typer.Option("--check", help="Show the installation source without changing anything.")] = False,
) -> None:
    """Reinstall lenspipe from where it was installed from (a refreshed checkout or a git remote), then run doctor."""
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    if not Path(uv).is_file():
        typer.echo("ERROR: uv is not installed; run scripts/install.sh instead.", err=True)
        raise typer.Exit(code=1)
    listing = subprocess.run([uv, "tool", "list", "--show-paths"], text=True, capture_output=True, check=False)
    installed = [line for line in listing.stdout.splitlines() if line.startswith("lenspipe ")]
    if not installed:
        typer.echo(
            "lenspipe is not installed as a uv tool (you are probably running a development checkout "
            "with 'uv run'). Pull or unzip the new version and run 'uv sync' instead.",
            err=True,
        )
        raise typer.Exit(code=1)
    typer.echo(f"Installed: {installed[0].strip()}")
    typer.echo(f"Current version: {__version__}")
    if check_only:
        return
    upgrade = subprocess.run([uv, "tool", "upgrade", "--reinstall", "lenspipe"], check=False)
    if upgrade.returncode != 0:
        typer.echo("ERROR: uv tool upgrade failed; see the output above.", err=True)
        raise typer.Exit(code=upgrade.returncode)
    fresh = subprocess.run(["lenspipe", "--version"], text=True, capture_output=True, check=False)
    typer.echo(f"Now: {fresh.stdout.strip() or 'unknown'}")
    subprocess.run(["lenspipe", "doctor"], check=False)


@app.command()
def verify(
    project: ProjectArg = Path("."),
    hash_all: Annotated[bool, typer.Option("--hash", help="Re-hash every input even when size and mtime are unchanged.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check every product against the inputs it was made from; exit 3 if any is stale."""
    from lenspipe.provenance import verify_project

    checks = verify_project(Layout.at(project).root, force_hash=hash_all)
    if as_json:
        typer.echo(json.dumps([check.to_dict() for check in checks], indent=2))
    else:
        if not checks:
            typer.echo("No products with metadata found.")
        for check in checks:
            product = f" {check.product}" if check.product else ""
            line = f"stage{check.stage}  {check.source}.{check.epoch}{product:<26} {check.status}"
            if check.reasons:
                line += "  (" + "; ".join(check.reasons) + ")"
            typer.echo(line)
    stale = [c for c in checks if c.status in {"stale", "upstream-stale", "missing-input"}]
    if stale:
        typer.echo(f"{len(stale)} stale product(s); re-run the affected stage(s).", err=True)
        raise typer.Exit(code=3)


@app.command()
def calibrate(
    project: ProjectArg = Path("."),
    config: ConfigOpt = None,
    steps: Annotated[str | None, typer.Option("--steps", help="Step selection passed to the CASA script.")] = None,
    list_steps: Annotated[bool, typer.Option("--list-steps")] = False,
) -> None:
    """Run the CASA calibration script configured under [casa] as a subprocess."""
    root, cfg = _load(project, config)
    casa = cfg.casa
    if not casa.interpreter or not casa.script:
        typer.echo("ERROR: set [casa] interpreter and script in lenspipe.toml first.", err=True)
        raise typer.Exit(code=1)
    script = (root / casa.script).resolve() if not Path(casa.script).is_absolute() else Path(casa.script)
    argv = casa.interpreter.split() + [str(script)]
    if casa.observation:
        observation = root / casa.observation if not Path(casa.observation).is_absolute() else Path(casa.observation)
        argv += ["--config", str(observation)]
    if steps:
        argv += ["--steps", steps]
    if list_steps:
        argv += ["--list-steps"]
    typer.echo("Running: " + " ".join(argv))
    completed = subprocess.run(argv, cwd=str(root), check=False)
    raise typer.Exit(code=completed.returncode)


@app.command()
def jobs(
    project: ProjectArg = Path("."),
    limit: Annotated[int, typer.Option("--limit")] = 20,
    cancel: Annotated[str | None, typer.Option("--cancel", help="Cancel this job id.")] = None,
    tail: Annotated[str | None, typer.Option("--tail", help="Show the log tail of this job id.")] = None,
) -> None:
    """List, tail or cancel jobs recorded under <project>/.jobs/."""
    from lenspipe.jobs import JobManager

    manager = JobManager(project)
    if cancel:
        record = manager.cancel(cancel)
        typer.echo(f"{cancel}: {record.status if record else 'not found'}")
        return
    if tail:
        record = manager.get(tail)
        if record is None:
            typer.echo("not found", err=True)
            raise typer.Exit(code=1)
        typer.echo(record.tail())
        return
    manager.pump()
    for record in manager.list(limit):
        typer.echo(f"{record.id:<34}{record.status:<11}{record.command_line}")


@app.command()
def ui(
    project: Annotated[Path | None, typer.Argument(help="Project to open. Default: the last project used, else the current directory; switch projects on the Project page.")] = None,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8080,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Do not open a browser window.")] = False,
) -> None:
    """Start the local web console. Stop it later with `lenspipe stop`."""
    from lenspipe.console_registry import find_consoles, port_in_use

    if port_in_use(host, port):
        running = find_consoles(port=port)
        if running:
            record = running[0]
            typer.echo(
                f"A console is already listening on port {port} (pid {record.pid}, project {record.root}).\n"
                f"Open {record.url}, or run 'lenspipe stop --port {port}' first, or choose another --port.",
                err=True,
            )
        else:
            typer.echo(f"Port {port} is already in use by another program; choose another --port.", err=True)
        raise typer.Exit(code=1)
    try:
        from lenspipe.ui.app import serve
    except ImportError as exc:  # pragma: no cover - depends on optional install
        typer.echo(f"ERROR: the console needs nicegui: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    serve(Layout.at(project).root if project is not None else None, host=host, port=port, open_browser=not no_browser)


@app.command()
def stop(
    project: Annotated[Path | None, typer.Argument(help="Only stop consoles serving this project.")] = None,
    port: Annotated[int | None, typer.Option("--port", help="Only stop the console on this port.")] = None,
    with_jobs: Annotated[bool, typer.Option("--with-jobs", help="Also cancel that project's running jobs.")] = False,
    list_only: Annotated[bool, typer.Option("--list", help="Show running consoles without stopping them.")] = False,
) -> None:
    """Stop running consoles (started by `lenspipe ui`), even from a terminal that has since closed.

    Jobs keep running unless --with-jobs is given: they are separate processes by design.
    """
    from lenspipe.console_registry import find_consoles, stop_console
    from lenspipe.jobs import JobManager

    root = Layout.at(project).root if project is not None else None
    consoles = find_consoles(root=root, port=port)
    if not consoles:
        typer.echo("No running console found" + (f" for {root}" if root else "") + (f" on port {port}" if port else "") + ".")
        if root is None and port is None:
            raise typer.Exit(code=0)
        raise typer.Exit(code=3)
    for record in consoles:
        origin = "" if record.source == "registry" else "  (found by process scan)"
        typer.echo(f"console pid {record.pid}  {record.url}  {record.root or '?'}{origin}")
    if list_only:
        return
    failed = 0
    for record in consoles:
        if stop_console(record):
            typer.echo(f"stopped pid {record.pid}")
        else:
            failed += 1
            typer.echo(f"could not stop pid {record.pid}", err=True)
    if with_jobs:
        roots = {r.root for r in consoles if r.root} if root is None else {str(root)}
        for job_root in sorted(roots):
            manager = JobManager(job_root)
            manager.pump()
            for job in manager.list():
                if job.status in {"running", "queued"}:
                    manager.cancel(job.id)
                    typer.echo(f"cancelled job {job.id}")
    else:
        for job_root in sorted({r.root for r in consoles if r.root}):
            try:
                manager = JobManager(job_root)
                manager.pump()
                active = [j for j in manager.list() if j.status in {"running", "queued"}]
            except OSError:
                continue
            if active:
                typer.echo(
                    f"{len(active)} job(s) still running for {job_root}; they continue on their own. "
                    "Use --with-jobs or 'lenspipe jobs --cancel <id>' to stop them."
                )
    if failed:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------


def _summary(title: str, results: list) -> None:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    typer.echo(f"{title}: " + ", ".join(f"{status}={n}" for status, n in sorted(counts.items())))


def main() -> int:
    """Entry point that also records the outcome when run inside a job directory."""
    job_dir = os.environ.get("LENSPIPE_JOB_DIR")
    code = 0
    error: str | None = None
    try:
        app(standalone_mode=True)
    except SystemExit as exc:
        code = int(exc.code or 0) if isinstance(exc.code, int) or exc.code is None else 1
    except Exception as exc:  # noqa: BLE001 - last resort, surfaced in the job record
        code = 1
        error = f"{type(exc).__name__}: {exc}"
        print(error, file=sys.stderr)
    if job_dir:
        from lenspipe.jobs import write_job_result

        write_job_result(Path(job_dir), code, error)
    return code
