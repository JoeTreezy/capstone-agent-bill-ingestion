"""The real, ongoing entry point -- replaces Retool's schedule-triggered
Extraction workflow (getAlreadyProcessedFileIds -> listBillFilesInFolder ->
getNewFiles -> downloadEachNewFile) plus a call into run_full_pipeline.

Two ways to use this:

1. Single-run, for an external scheduler (cron, Airflow, Prefect, a systemd
   timer, Windows Task Scheduler) -- call check_for_new_files_and_process()
   once per invocation, same pattern as main.py's run_full_pipeline. This is
   the better fit if you already have something scheduling jobs.

2. Simple internal loop, if you don't have an external scheduler and just
   want this to run continuously on its own -- run this file directly.
"""
import logging
import time

from config import settings
import db
from google_drive import list_files_in_folder, download_files
from main import run_full_pipeline

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_already_processed_file_ids() -> set[str]:
    rows = db.fetch_all("select file_id from extracted_files")
    return {row["file_id"] for row in rows}


def check_for_new_files_and_process() -> dict:
    """One polling cycle: list the Drive folder, diff against what's already
    been processed, download and run the full pipeline on whatever's new.
    Returns a summary dict -- useful for logging or an external scheduler
    that wants to know what happened."""
    all_files = list_files_in_folder()
    already_processed = get_already_processed_file_ids()
    new_files = [f for f in all_files if f["file_id"] not in already_processed]

    if not new_files:
        logger.info("No new files in Drive folder.")
        return {"new_file_count": 0}

    logger.info("Found %d new file(s): %s", len(new_files), [f["file_name"] for f in new_files])
    downloaded = download_files(new_files)
    run_full_pipeline(downloaded)

    return {"new_file_count": len(new_files), "file_names": [f["file_name"] for f in new_files]}


def run_continuous_loop() -> None:
    """For when there's no external scheduler yet -- polls on an interval,
    forever, until the process is killed. Prefer check_for_new_files_and_process()
    called from cron/Airflow/etc. if you already have one of those available;
    this is the simplest possible fallback, not the most robust option (no
    retry-with-backoff on repeated failures, no health check endpoint, etc.)."""
    logger.info("Starting continuous polling loop, interval=%ds", settings.poll_interval_seconds)
    while True:
        try:
            check_for_new_files_and_process()
        except Exception:
            logger.exception("Error during polling cycle -- will retry next interval")
        time.sleep(settings.poll_interval_seconds)


if __name__ == "__main__":
    run_continuous_loop()