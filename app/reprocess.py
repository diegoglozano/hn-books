import argparse

from app.clients import MetadataClient, RemoteClient
from app.config import get_settings
from app.db import connect
from app.operations import configure_logging, tracked_run
from app.pipeline import classify_mentions, extract_and_resolve, refresh_aggregates


def main() -> int:
    parser = argparse.ArgumentParser(description="Reprocess stored HN data without fetching HN")
    parser.add_argument("stage", choices=["mentions", "tags"])
    parser.add_argument("--offline", action="store_true", help="Use only cached metadata")
    parser.add_argument("--thread", type=int, help="Re-extract mentions in one stored HN thread")
    args = parser.parse_args()
    if args.thread is not None and (args.thread <= 0 or args.stage != "mentions"):
        parser.error("--thread requires a positive HN ID and the mentions stage")
    configure_logging()
    settings = get_settings()
    with tracked_run(settings, f"reprocess {args.stage}") as metrics, connect() as conn:
        if (
            args.thread is not None
            and not conn.execute("SELECT 1 FROM hn_threads WHERE id=?", (args.thread,)).fetchone()
        ):
            raise ValueError(f"HN thread {args.thread} has not been ingested")
        if args.stage == "mentions" or settings.extraction_backend == "luna":
            remote = RemoteClient(settings)
            try:
                extract_and_resolve(
                    conn,
                    MetadataClient(remote, conn, metrics, args.offline),
                    metrics,
                    force=True,
                    thread_id=args.thread,
                )
            finally:
                remote.close()
        else:
            metrics.classifications_performed = classify_mentions(conn)
        refresh_aggregates(conn)
    return 1 if metrics.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
