import fcntl
import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

from app.config import Settings
from app.db import connect, initialize, utc_now
from app.models import RunMetrics


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"timestamp": utc_now(), "level": record.levelname, "event": record.getMessage()}
        for key in ("item_id", "detail", "path", "metrics", "run_id"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)


@contextmanager
def writer_lock(settings: Settings) -> Iterator[None]:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    with settings.database_path.with_suffix(".lock").open("a") as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another ingestion/reprocessing command is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


@contextmanager
def tracked_run(settings: Settings, command: str) -> Iterator[RunMetrics]:
    with writer_lock(settings):
        initialize(settings.database_path)
        metrics = RunMetrics()
        start = time.monotonic()
        with connect(settings.database_path) as conn:
            cursor = conn.execute(
                "INSERT INTO ingestion_runs(command,started_at,status) VALUES (?, ?, 'running')",
                (command, utc_now()),
            )
            run_id = cursor.lastrowid
        outcome: dict[str, str | None] = {"error": None}
        try:
            yield metrics
        except BaseException as exc:
            metrics.failures += 1
            outcome["error"] = str(exc)
            raise
        finally:
            metrics.runtime_seconds = round(time.monotonic() - start, 3)
            error = outcome["error"]
            status = run_status(error, metrics.failures)
            with connect(settings.database_path) as conn:
                conn.execute(
                    """UPDATE ingestion_runs SET finished_at=?,status=?,metrics_json=?,error=?
                    WHERE id=?""",
                    (utc_now(), status, metrics.model_dump_json(), error, run_id),
                )
            logging.getLogger(__name__).info(
                "run_finished",
                extra={
                    "run_id": run_id,
                    "metrics": metrics.model_dump(),
                    "detail": status,
                },
            )


def run_status(error: str | None, failures: int) -> str:
    if error is not None:
        return "failed"
    return "partial" if failures else "completed"
