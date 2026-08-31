"""
``precise`` — top-level CLI entry point.

Each subcommand is a thin wrapper that imports the relevant module and calls
its implementation.  All business logic lives in the module, not here.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import rich_argparse
from rich.console import Console

from .logging_setup import setup_logging
from .pipeline import TENTATIVE_PROJECT

console = Console()
log = logging.getLogger(__name__)


def _fail(message: str) -> None:
    """Print *message* as an error and exit non-zero."""
    console.print(f"[red]Error:[/red] {message}")
    sys.exit(1)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="precise",
        description="PRECISE Observation Scheduler — autonomous VLBI scheduling tool.",
        formatter_class=rich_argparse.RawDescriptionRichHelpFormatter,
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        default=None,
        help="Override the default config file location.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Debug mode: print all antenna/upload actions to terminal; never send anything.",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # add-detection
    add_det_parser = subparsers.add_parser(
        "add-detection",
        help="Record a new FRB detection in the activity database.",
        formatter_class=rich_argparse.RawDescriptionRichHelpFormatter,
    )
    add_det_parser.add_argument(
        "--source",
        required=True,
        metavar="CANONICAL_NAME",
        help="Canonical source name (e.g. FRB20201124A).",
    )
    add_det_parser.add_argument(
        "--utc",
        required=True,
        metavar="DATETIME",
        help="Detection time as 'DD-MM-YYYY HH:MM[:SS.ffffff]' (UTC).",
    )
    add_det_parser.add_argument(
        "-f", "--facility",
        default="CHIME",
        help="Detecting facility. (default: CHIME)",
    )

    # activity
    activity_parser = subparsers.add_parser(
        "activity",
        help="Show FRB detections from the activity database.",
        formatter_class=rich_argparse.RawDescriptionRichHelpFormatter,
    )
    activity_parser.add_argument(
        "--since",
        default="30d",
        metavar="WINDOW",
        help="Look-back window, e.g. 15d, 2month, 1yr. (default: 30d)",
    )
    activity_parser.add_argument(
        "--source",
        default=None,
        metavar="CANONICAL_NAME",
        help="Filter to a single source.",
    )

    # delete-detection
    del_det_parser = subparsers.add_parser(
        "delete-detection",
        help="Remove a detection record from the activity database.",
        formatter_class=rich_argparse.RawDescriptionRichHelpFormatter,
    )
    del_det_parser.add_argument(
        "--source",
        required=True,
        metavar="CANONICAL_NAME",
        help="Canonical source name.",
    )
    del_det_parser.add_argument(
        "--utc",
        required=True,
        metavar="DATETIME",
        help="Detection time as 'DD-MM-YYYY HH:MM[:SS.ffffff]' (UTC).",
    )

    # calendar
    calendar_parser = subparsers.add_parser(
        "calendar",
        help="List upcoming observing runs from the Google Sheets calendar.",
        description=(
            "Read the scheduling spreadsheet and print a table of the upcoming "
            "observing runs: project, date, time range (UT) and the stations "
            "scheduled to participate."
        ),
        formatter_class=rich_argparse.RawDescriptionRichHelpFormatter,
    )
    calendar_parser.add_argument(
        "--days",
        type=int,
        default=20,
        metavar="N",
        help="How many days ahead to look. (default: 20)",
    )
    calendar_parser.add_argument(
        "--no-vlbeer",
        action="store_true",
        default=False,
        help=(
            "Skip the vlbeer check, which needs an ssh connection. The column "
            "is shown as unknown instead."
        ),
    )

    # daemon
    daemon_parser = subparsers.add_parser(
        "daemon",
        help="Start the scheduling daemon (polls Google Sheets every N hours).",
        formatter_class=rich_argparse.RawDescriptionRichHelpFormatter,
    )

    # plan
    plan_parser = subparsers.add_parser(
        "plan",
        help="Report which FRBs can be observed in the upcoming run.",
        description=(
            "Work out which FRBs are observable in the next scheduled run.\n\n"
            "Takes the observation window from the Google Sheets calendar (or "
            "--epoch) and the pre-approved sources from targets.toml: priority 2 "
            "always, priority 1 only while the activity database shows a recent "
            "detection. planobs is then asked which of those the array can "
            "observe, and only the ones every antenna -- or all but one -- can "
            "see are kept.\n\n"
            "Prints the observation summary, the activity summary, PRECISE's "
            "previous runs and a table of the observable FRBs with their "
            "visibility grid -- the same report the daemon posts to Slack. "
            "Pass --key to also write the .key schedule file; nothing is ever "
            "committed, pushed or posted from here."
        ),
        formatter_class=rich_argparse.RawDescriptionRichHelpFormatter,
    )
    plan_parser.add_argument(
        "--epoch",
        default=None,
        metavar="'YYYY-MM-DD HH:MM DURATION'",
        help=(
            "Plan this window instead of the next calendar entry, where DURATION "
            "is in hours, e.g. --epoch '2026-08-31 18:00 10'. Requires --antennas."
        ),
    )
    plan_parser.add_argument(
        "--antennas", nargs="+", default=None, metavar="ANTENNA",
        help="Participating antennas, e.g. --antennas Ef Wb Tr Ir. Required with --epoch.",
    )
    plan_parser.add_argument(
        "--project", default=None, metavar="PROJECT",
        help=f"Project code for an --epoch run (default: {TENTATIVE_PROJECT}).",
    )
    plan_parser.add_argument(
        "--targets", nargs="+", default=None, metavar="TARGET",
        help=(
            "Check only these sources, bypassing the priority and activity "
            "pre-approval. Accepts internal names (R200223_D) or canonical "
            "names (FRB20200223B)."
        ),
    )
    plan_parser.add_argument(
        "--show-dropped", action="store_true", default=False,
        help="Also list the candidates that fewer antennas can observe.",
    )
    plan_parser.add_argument(
        "--key", action="store_true", default=False,
        help=(
            "Also write the .key schedule file for the observable sources. "
            "Without it the report is printed and nothing is written."
        ),
    )
    plan_parser.add_argument(
        "--fringe-finders", nargs="+", default=None, metavar="SPEC",
        help=(
            "Fringe finders, passed straight to planobs: a count "
            "(--fringe-finders 3) or explicit source names "
            "(--fringe-finders J0927+3902 J2253+1608). Only used with --key. "
            "Default: planobs picks 2 scans on the source it judges best."
        ),
    )
    plan_parser.add_argument(
        "--pulsars", nargs="+", default=None, metavar="SPEC",
        help=(
            "Pulsar check sources, same form: a count (--pulsars 2), pulsar "
            "names from targets.toml (--pulsars B0329+54_D), or --pulsars 0 "
            "for none. Only used with --key. (default: 1, the one up longest)"
        ),
    )

    # distribute
    distribute_parser = subparsers.add_parser(
        "distribute",
        help="Compile, upload to vlbeer, and push the final schedule for PROJECT.",
        description=(
            "Finalise a schedule: compile it, upload the artefacts to vlbeer, "
            "commit the final version, and announce it on Slack and Mattermost.\n\n"
            "The notification texts are echoed to the terminal."
        ),
        formatter_class=rich_argparse.RawDescriptionRichHelpFormatter,
    )
    distribute_parser.add_argument(
        "--project",
        required=True,
        metavar="PROJECT",
        help="Project code (e.g. RP036A).",
    )

    return parser


def parse_datetime(utc_str: str) -> datetime:
    """Parse datetime string in format 'DD-MM-YYYY HH:MM[:SS.ffffff]' (UTC)."""
    for fmt in ("%d-%m-%Y %H:%M:%S.%f", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M"):
        try:
            return datetime.strptime(utc_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(
        f"Cannot parse '{utc_str}'. Use 'DD-MM-YYYY HH:MM[:SS.ffffff]'."
    )


def cmd_add_detection(config_path: str | None, source: str, utc: str, facility: str, debug: bool) -> None:
    from .activity import Activity
    from .config import Config

    cfg = Config.load(Path(config_path) if config_path else None)
    if debug:
        cfg.debug = True
    act = Activity(cfg.activity_db)
    try:
        dt = parse_datetime(utc)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    act.add_detection(source, dt, facility)
    console.print(f"[green]✓[/green] Recorded {source} @ {dt.isoformat()} ({facility})")


def cmd_activity(config_path: str | None, since: str, source: str | None) -> None:
    from .activity import Activity, _parse_since
    from .config import Config
    from rich.table import Table

    cfg = Config.load(Path(config_path) if config_path else None)
    act = Activity(cfg.activity_db)
    since_days = _parse_since(since)

    if source:
        records = act.get_detections(source, since_days)
        if not records:
            console.print(f"No detections for [bold]{source}[/bold] in the last {since}.")
            return
        table = Table(title=f"Detections: {source} (last {since})")
        table.add_column("UTC datetime", style="cyan")
        table.add_column("Facility")
        for r in records:
            table.add_row(r.utc_datetime.isoformat(), r.facility)
        console.print(table)
    else:
        sources = act.get_active_sources(since_days)
        if not sources:
            console.print(f"No active sources in the last {since}.")
            return
        table = Table(title=f"Active sources (last {since})")
        table.add_column("Canonical name", style="cyan")
        for s in sources:
            table.add_row(s)
        console.print(table)


def cmd_delete_detection(config_path: str | None, source: str, utc: str) -> None:
    from .activity import Activity
    from .config import Config

    cfg = Config.load(Path(config_path) if config_path else None)
    act = Activity(cfg.activity_db)
    try:
        dt = parse_datetime(utc)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    deleted = act.delete_detection(source, dt)
    if deleted:
        console.print(f"[green]✓[/green] Deleted detection {source} @ {dt.isoformat()}")
    else:
        console.print(f"[yellow]⚠[/yellow] No matching detection found.")
        sys.exit(1)


def _format_obs_dates(start: datetime, end: datetime) -> str:
    """Format an observation's date(s) like the spreadsheet, e.g. ``3-4 Aug 2026``."""
    if start.date() == end.date():
        return f"{start.day} {start:%b %Y}"
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day}-{end.day} {start:%b %Y}"
    if start.year == end.year:
        return f"{start.day} {start:%b} - {end.day} {end:%b %Y}"
    return f"{start.day} {start:%b %Y} - {end.day} {end:%b %Y}"


def cmd_calendar(
    config_path: str | None, days: int, check_vlbeer: bool = True
) -> None:
    from .calendar import CalendarError, ObservationCalendar
    from .config import Config, ConfigError
    from .publisher import local_key_files, month_dir, vlbeer_uploaded
    from rich.table import Table

    try:
        cfg = Config.load(Path(config_path) if config_path else None)
    except ConfigError as exc:
        _fail(str(exc))
        return

    if days < 1:
        _fail("--days must be a positive number of days.")
        return

    cal = ObservationCalendar(cfg.google_spreadsheet_id)
    try:
        windows = cal.get_upcoming_observations(lookahead_days=days)
    except CalendarError as exc:
        _fail(str(exc))
        return

    if not windows:
        console.print(f"No observations scheduled in the next {days} days.")
        return

    scheduled = local_key_files(cfg.schedules_repo)
    uploaded: set[str] | None = None
    if check_vlbeer:
        uploaded = vlbeer_uploaded(cfg, {month_dir(w.start) for w in windows})

    def _tick(done: bool) -> str:
        return "[green]\u2713[/green]" if done else "[red]\u2717[/red]"

    table = Table(title=f"Upcoming observations (next {days} days)")
    table.add_column("Project", style="cyan")
    table.add_column("Date")
    table.add_column("Time (UT)")
    table.add_column("Stations")
    table.add_column(".key", justify="center")
    table.add_column("vlbeer", justify="center")
    for w in windows:
        table.add_row(
            w.project,
            _format_obs_dates(w.start, w.end),
            f"{w.start:%H:%M}-{w.end:%H:%M}",
            ", ".join(w.antennas),
            _tick(w.project.upper() in scheduled),
            "[dim]?[/dim]" if uploaded is None
            else _tick(w.project.upper() in uploaded),
        )
    console.print(table)
    console.print(
        "[dim].key: a schedule exists in the schedules repository.  "
        "vlbeer: a .key or .vex is already uploaded.[/dim]"
    )
    if uploaded is None:
        console.print(
            "[yellow]![/yellow] [dim]vlbeer could not be reached, so its "
            "column is unknown rather than empty.[/dim]"
            if check_vlbeer
            else "[dim]vlbeer was not checked (--no-vlbeer).[/dim]"
        )


def cmd_daemon(config_path: str | None, debug: bool) -> None:
    from .daemon import main as daemon_main
    from .config import Config

    cfg = Config.load(Path(config_path) if config_path else None)
    if debug:
        cfg.debug = True
    if cfg.debug:
        console.print("[yellow][DEBUG] Debug mode active — no data will be sent to antenna server.[/yellow]")
    console.print("[bold green]PRECISE Scheduler daemon starting…[/bold green]")
    daemon_main(cfg)


def cmd_plan(
    config_path: str | None,
    epoch: str | None,
    antennas: list[str] | None,
    project: str | None,
    targets: list[str] | None,
    show_dropped: bool,
    write_key: bool,
    fringe_finders: list[str] | None,
    pulsars: list[str] | None,
) -> None:
    from rich.table import Table

    from .calendar import CalendarError
    from .config import Config, ConfigError
    from .pipeline import ScheduleInputError, run_plan
    from .render import render_blocks
    from .slack_scheduler import build_plan_messages
    from .targets import TargetSchemaError
    from .visibility import VisibilityError

    try:
        cfg = Config.load(Path(config_path) if config_path else None)
        plan = run_plan(
            cfg,
            epoch=epoch,
            antennas=antennas,
            project=project,
            candidate_names=targets,
            fringe_finders=fringe_finders,
            pulsars=pulsars,
            write_key=write_key,
        )
    except (
        ConfigError,
        ScheduleInputError,
        CalendarError,
        TargetSchemaError,
        VisibilityError,
    ) as exc:
        _fail(str(exc))
        return

    # The very same messages the daemon posts to Slack, rendered here instead.
    # highlight=False stops rich recolouring numbers and dates; soft_wrap=True
    # stops it re-wrapping the visibility grid.
    console.print()
    for message in build_plan_messages(plan):
        console.print(render_blocks(message), highlight=False, soft_wrap=True)
        console.print()

    if show_dropped and plan.dropped:
        dropped = Table(header_style="bold dim", box=None, pad_edge=False)
        dropped.add_column("dropped")
        dropped.add_column("FRB")
        dropped.add_column("antennas", justify="right")
        for s in plan.dropped:
            dropped.add_row(
                s.target.name, s.target.canonical_name or "-",
                f"{s.n_antennas}/{plan.n_array}", style="dim",
            )
        console.print(dropped)
        console.print()

    if plan.key_file is not None:
        console.print(
            f"[green]OK[/green] Schedule written to {plan.key_file}",
            highlight=False,
        )
        console.print(
            "[dim]Nothing was committed and nothing was posted to Slack. "
            f"Run 'precise distribute --project {plan.obs_window.project}' "
            "to finalise.[/dim]",
            highlight=False,
        )
    else:
        console.print(
            "[dim]Report only -- pass --key to write the .key schedule "
            "file.[/dim]",
            highlight=False,
        )


def cmd_distribute(config_path: str | None, project: str, debug: bool) -> None:
    from .config import Config, ConfigError
    from .publisher import (
        PublisherError,
        SchedulePublisher,
        mattermost_message,
        slack_success_message,
    )

    try:
        cfg = Config.load(Path(config_path) if config_path else None)
    except ConfigError as exc:
        _fail(str(exc))
        return

    debug = cfg.debug or debug
    if debug:
        console.print(
            "[yellow][DEBUG] Debug mode active — no data will be sent to antenna server.[/yellow]"
        )

    pub = SchedulePublisher(cfg)
    console.print(f"[bold]Distributing schedule for {project}…[/bold]")

    try:
        pub.upload_and_report(Path(cfg.schedules_repo), project, debug=debug)
    except PublisherError as exc:
        _fail(str(exc))
        return

    suffix = " (debug mode)" if debug else ""
    console.print(f"[green]✓[/green] Schedule for {project} distributed{suffix}.")

    verb = "would be sent" if debug else "sent"
    console.rule(f"[bold]Notifications {verb}")
    console.print(f"[bold]Slack:[/bold] {slack_success_message(project)}")
    console.print(f"[bold]Mattermost:[/bold] {mattermost_message(project)}")
    console.rule()


def main() -> None:
    setup_logging()
    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == "add-detection":
        cmd_add_detection(args.config, args.source, args.utc, args.facility, args.debug)
    elif args.command == "activity":
        cmd_activity(args.config, args.since, args.source)
    elif args.command == "delete-detection":
        cmd_delete_detection(args.config, args.source, args.utc)
    elif args.command == "calendar":
        cmd_calendar(args.config, args.days, check_vlbeer=not args.no_vlbeer)
    elif args.command == "daemon":
        cmd_daemon(args.config, args.debug)
    elif args.command == "plan":
        cmd_plan(
            args.config,
            args.epoch,
            args.antennas,
            args.project,
            args.targets,
            args.show_dropped,
            args.key,
            args.fringe_finders,
            args.pulsars,
        )
    elif args.command == "distribute":
        cmd_distribute(args.config, args.project, args.debug)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
