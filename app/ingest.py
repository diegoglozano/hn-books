import argparse
import calendar
import json
import logging
from datetime import UTC, date, datetime

from app.backfill import completed_threads, ingest_backfill
from app.clients import MetadataClient, RemoteClient
from app.config import get_settings
from app.db import connect
from app.discovery import discover_reading_threads
from app.operations import configure_logging, tracked_run
from app.pipeline import (
    discover_threads,
    extract_and_resolve,
    fetch_thread,
    processor_version,
    refresh_aggregates,
)


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Must be a positive integer")
    return number


def backfill_period(args: argparse.Namespace, now: datetime | None = None) -> tuple[int, int]:
    now = now or datetime.now(UTC)
    until = datetime.combine(args.until, datetime.min.time(), UTC) if args.until else now
    if args.since:
        since = datetime.combine(args.since, datetime.min.time(), UTC)
    else:
        year = until.year - (args.years or 5)
        if year < 1:
            raise ValueError("The requested year range is too large")
        day = min(until.day, calendar.monthrange(year, until.month)[1])
        since = until.replace(year=year, day=day)
    start, end = int(since.timestamp()), int(until.timestamp())
    if start >= end:
        raise ValueError("--since must be earlier than --until")
    return start, end


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest HN book discussions; save raw items first")
    commands = parser.add_subparsers(dest="command")
    single = commands.add_parser("thread", help="Ingest a complete comment tree")
    single.add_argument("id", type=int)
    historical = commands.add_parser("backfill", help="Resume historical reading-thread ingestion")
    period = historical.add_mutually_exclusive_group()
    period.add_argument("--years", type=positive_int, help="Years before the end date (default: 5)")
    period.add_argument("--since", type=date.fromisoformat, help="Inclusive start date: YYYY-MM-DD")
    historical.add_argument(
        "--until", type=date.fromisoformat, help="Exclusive end date: YYYY-MM-DD (default: now UTC)"
    )
    historical.add_argument(
        "--scope",
        choices=["ask-hn", "stories"],
        default="ask-hn",
        help="Search Ask HN titles or all HN story titles",
    )
    historical.add_argument("--limit", type=positive_int, help="Process at most N pending threads")
    historical.add_argument(
        "--dry-run", action="store_true", help="Discover and report; do not ingest"
    )
    historical.add_argument("--refresh", action="store_true", help="Refetch completed threads too")
    commands.add_parser("discover", help="Discover the first page of each configured query")
    args = parser.parse_args()
    bounds = None
    if args.command == "backfill":
        try:
            bounds = backfill_period(args)
        except ValueError as exc:
            parser.error(str(exc))
    settings = get_settings()
    configure_logging()
    if args.command == "backfill" and args.dry_run:
        assert bounds is not None
        remote = RemoteClient(settings)
        try:
            candidates = discover_reading_threads(remote, *bounds, scope=args.scope)
            done: set[int] = set()
            if settings.database_path.exists():
                with connect() as conn:
                    if conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='thread_checkpoints'",
                    ).fetchone():
                        done = completed_threads(conn)
            pending = [c for c in candidates if args.refresh or c.id not in done]
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "scope": args.scope,
                        "since": datetime.fromtimestamp(bounds[0], UTC).isoformat(),
                        "until": datetime.fromtimestamp(bounds[1], UTC).isoformat(),
                        "threads_discovered": len(candidates),
                        "threads_pending": len(pending),
                        "threads_to_process": min(len(pending), args.limit or len(pending)),
                        "estimated_comments": sum(c.comments for c in pending[: args.limit]),
                        "preview": [
                            {"id": c.id, "title": c.title, "comments": c.comments}
                            for c in pending[:10]
                        ],
                    },
                    indent=2,
                )
            )
        finally:
            remote.close()
        return 0
    run_command = args.command or "daily"
    if bounds:
        run_command += f" since={bounds[0]} until={bounds[1]} scope={args.scope}"
    with tracked_run(settings, run_command) as metrics:
        remote = RemoteClient(settings)
        try:
            with connect() as conn:
                if args.command == "backfill":
                    assert bounds is not None
                    candidates = discover_reading_threads(remote, *bounds, scope=args.scope)
                    metrics.threads_discovered = len(candidates)
                    ingest_backfill(
                        conn,
                        remote,
                        [c.id for c in candidates],
                        metrics,
                        limit=args.limit,
                        refresh=args.refresh,
                    )
                else:
                    if args.command == "thread":
                        thread_ids = [args.id]
                    elif args.command == "discover":
                        thread_ids = discover_threads(remote)
                        metrics.threads_discovered = len(thread_ids)
                    else:
                        thread_ids = sorted(
                            set(settings.thread_ids)
                            | {row[0] for row in conn.execute("SELECT id FROM hn_threads")}
                        )
                    if not thread_ids:
                        logging.getLogger(__name__).info(
                            "no_threads_configured; use: python -m app.ingest thread <HN_ID>",
                        )
                    for thread_id in thread_ids:
                        try:
                            fetch_thread(conn, remote, thread_id, metrics)
                        except Exception as exc:
                            metrics.failures += 1
                            logging.getLogger(__name__).exception(
                                "thread_fetch_failed",
                                extra={"item_id": thread_id, "detail": str(exc)},
                            )
                    extract_and_resolve(
                        conn,
                        MetadataClient(remote, conn, metrics),
                        metrics,
                        thread_id=args.id if args.command == "thread" else None,
                    )
                    refresh_aggregates(conn)
                    if metrics.failures == 0:
                        conn.execute(
                            """UPDATE thread_checkpoints SET processed_version=?,completed_at=?
                            WHERE raw_complete=1 AND (? IS NULL OR thread_id=?)""",
                            (
                                processor_version(settings),
                                datetime.now(UTC).isoformat(),
                                args.id if args.command == "thread" else None,
                                args.id if args.command == "thread" else None,
                            ),
                        )
        finally:
            remote.close()
    return 1 if metrics.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
