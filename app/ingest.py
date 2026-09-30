import argparse
import logging

from app.clients import MetadataClient, RemoteClient
from app.config import get_settings
from app.db import connect
from app.operations import configure_logging, tracked_run
from app.pipeline import discover_threads, extract_and_resolve, fetch_thread, refresh_aggregates


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest HN book discussions; save raw items first")
    commands = parser.add_subparsers(dest="command")
    single = commands.add_parser("thread", help="Ingest a complete comment tree")
    single.add_argument("id", type=int)
    commands.add_parser(
        "backfill", help="Discover historical Ask HN threads using configured queries"
    )
    commands.add_parser("discover", help="Discover the first page of each configured query")
    args = parser.parse_args()
    settings = get_settings()
    configure_logging()
    with tracked_run(settings, args.command or "daily") as metrics:
        remote = RemoteClient(settings)
        try:
            with connect() as conn:
                if args.command == "thread":
                    thread_ids = [args.id]
                elif args.command in {"backfill", "discover"}:
                    thread_ids = discover_threads(remote, backfill=args.command == "backfill")
                    metrics.threads_discovered = len(thread_ids)
                else:
                    # Daily ingestion starts with an explicit corpus. Discovery is opt-in.
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
                # The entire raw stage is committed before any derived processing.
                extract_and_resolve(conn, MetadataClient(remote, conn, metrics), metrics)
                refresh_aggregates(conn)
        finally:
            remote.close()
    return 1 if metrics.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
