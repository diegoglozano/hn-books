import argparse

from app.config import get_settings
from app.db import connect
from app.operations import configure_logging, tracked_run
from app.pipeline import refresh_aggregates


def main() -> None:
    parser = argparse.ArgumentParser(description="Recompute library scores and lexical index")
    parser.add_argument("stage", choices=["rankings"])
    args = parser.parse_args()
    configure_logging()
    with tracked_run(get_settings(), f"recompute {args.stage}"), connect() as conn:
        refresh_aggregates(conn)


if __name__ == "__main__":
    main()
