"""Example orchestration wiring extraction, location/hauler resolution, and
line resolution together. Written as plain functions -- call these from
whatever scheduler your infrastructure already uses (cron, Airflow, Prefect,
Dagster), not tied to any particular one.
"""
import logging

from config import settings
from sinks import get_output_sink
from extraction import run_extraction
from resolution import LocationResolver, HaulerResolver, get_reference_data
from line_resolution import run_line_resolution, get_bill_ids_ready_for_line_resolution

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def run_full_pipeline(downloaded_files: list[dict]) -> None:
    """downloaded_files: [{"file_id", "file_name", "drive_link", "content_base64"}, ...]
    (fetching these from Drive is intentionally out of scope here -- see README)."""
    sink = get_output_sink()
    logger.info("Output mode: %s", settings.output_mode)

    # --- Extraction ---
    extraction_result = run_extraction(downloaded_files, sink)
    bill_ids = [b["bill_id"] for b in extraction_result["bills"]]
    logger.info("Extracted %d bills, %d failures", len(bill_ids), len(extraction_result["failed_files"]))

    if settings.output_mode != "database":
        logger.info("Output mode is '%s' -- stopping after extraction, since location/hauler and "
                     "line resolution both depend on reading back what extraction just wrote, "
                     "which only happens in database mode.", settings.output_mode)
        return

    # --- Location & hauler resolution ---
    reference_data = get_reference_data()

    location_resolver = LocationResolver()
    location_results = location_resolver.resolve_batch(reference_data)
    location_resolver.write_resolutions(location_results, sink)
    logger.info("Resolved %d bills' locations", len(location_results))

    ambiguous_locations = location_resolver.resolve_ambiguous_with_tot(location_results, reference_data)
    location_resolver.write_tot_resolutions(ambiguous_locations, sink)
    logger.info("ToT-resolved %d ambiguous locations", len(ambiguous_locations))

    hauler_resolver = HaulerResolver()
    hauler_results = hauler_resolver.resolve_batch(reference_data)
    hauler_resolver.write_resolutions(hauler_results, sink)
    logger.info("Resolved %d bills' hauler accounts", len(hauler_results))

    ambiguous_hauler_ids = [r.bill_id for r in hauler_results if r.hauler_match_status == "possible_match"]
    hauler_context = hauler_resolver.get_context_for_bills(ambiguous_hauler_ids)
    ambiguous_haulers = hauler_resolver.resolve_ambiguous_with_tot(hauler_results, hauler_context, reference_data)
    hauler_resolver.write_tot_resolutions(ambiguous_haulers, sink)
    logger.info("ToT-resolved %d ambiguous hauler accounts", len(ambiguous_haulers))

    # --- Line resolution ---
    # Queries the database directly for bills with both location and hauler
    # resolved -- NOT computed from this run's in-memory location_results/
    # hauler_results, since a bill resolved across two different runs (or
    # entirely in a prior run) is just as ready, and batch-local computation
    # silently misses that. See get_bill_ids_ready_for_line_resolution's
    # docstring.
    resolvable_bill_ids = get_bill_ids_ready_for_line_resolution()
    line_result = run_line_resolution(resolvable_bill_ids, sink)
    logger.info("Resolved %d lines", len(line_result["final_lines"]))


if __name__ == "__main__":
    run_full_pipeline(downloaded_files=[])