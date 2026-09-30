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
    args = parser.parse_args()
    configure_logging()
    settings = get_settings()
    with tracked_run(settings, f"reprocess {args.stage}") as metrics, connect() as conn:
        if args.stage == "mentions":
            remote = RemoteClient(settings)
            try:
                extract_and_resolve(
                    conn, MetadataClient(remote, conn, metrics, args.offline), metrics, force=True
                )
            finally:
                remote.close()
        else:
            metrics.classifications_performed = classify_mentions(conn)
        refresh_aggregates(conn)
    return 1 if metrics.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
