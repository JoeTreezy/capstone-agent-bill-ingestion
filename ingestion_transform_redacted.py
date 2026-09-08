"""
Ingestion transform — Retool Workflow Python code step.

Takes the raw rows read from a Google Sheet (whether from the watched Drive
folder or a user-supplied sheet) and turns them into staged bill header +
line records ready to insert into `staged_bills` / `staged_bill_lines`.

Expected input in the Retool workflow: `sheet_rows`, a list of dicts, one
per spreadsheet row, with keys matching the template's column headers
exactly (e.g. from a prior "Read Google Sheet" query step's `.data`,
similar to how getQueryExport.data carried the AQ export text).

This step only builds header/line groupings and does light normalization
and validation. It deliberately does NOT call out to:
  - location/hauler resolution lookups
  - the service match query
  - the duplicate bill check
These stay as separate query steps in the workflow (existing queries you
already have), run per bill_key after this transform, since they hit other
resources and are easier to reason about, retry, and reuse independently.
"""

import re
from collections import OrderedDict


# ---- Template contract -----------------------------------------------------
# properietary
EXPECTED_COLUMNS = [
    "billNumber"
]
# proprietary
HEADER_FIELDS = [
    "billNumber"
]
# properietary
LINE_FIELDS = [
    "lineType"
]


class TemplateValidationError(Exception):
    """Raised when the sheet's header row doesn't match the expected template."""
    pass


def validate_headers(sheet_rows):
    """
    Confirms the sheet's columns match the template exactly (order doesn't
    matter, presence does). Raises with a specific diff rather than letting
    misaligned columns silently corrupt data downstream — this is the main
    risk point for a user-supplied sheet.
    """
    if not sheet_rows:
        raise TemplateValidationError("Sheet has no data rows to validate against.")

    actual_columns = set(sheet_rows[0].keys())
    expected_columns = set(EXPECTED_COLUMNS)

    missing = expected_columns - actual_columns
    unexpected = actual_columns - expected_columns

    if missing or unexpected:
        parts = []
        if missing:
            parts.append(f"missing columns: {sorted(missing)}")
        if unexpected:
            parts.append(f"unexpected columns: {sorted(unexpected)}")
        raise TemplateValidationError(
            "Sheet does not match the expected template — " + "; ".join(parts)
        )


# ---- Normalization ----------------------------------------------------------

def normalize_bill_number(raw_bill_number):
    """
    Strips whitespace and dashes so '73289-300672' and '73289300672' resolve
    to the same key. Used for grouping, and must be applied identically
    wherever bill_number is compared later (duplicate check, batch resolve).
    """
    if raw_bill_number is None:
        return None
    return re.sub(r"[\s\-]", "", str(raw_bill_number))


def normalize_key_part(value):
    """Light normalization for location_id / hauler_account_id used in the grouping key."""
    if value is None:
        return None
    return str(value).strip()


def make_bill_key(row):
    """The composite key a bill is grouped and later matched by."""
    return (
        normalize_bill_number(row.get("billNumber")),
        normalize_key_part(row.get("locationId")),
        normalize_key_part(row.get("haulerAccountId")),
    )


def _truthy(value):
    """Sheet booleans can arrive as actual bools, or as strings like 'TRUE'/'FALSE'."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("true", "1", "yes")


# ---- Core transform -----------------------------------------------------------

def build_line_record(row):
    """Extracts and lightly types one line-level record from a sheet row."""
    line = {field: row.get(field) for field in LINE_FIELDS}

    # Rename/clean up a couple of fields for the staging schema
    exception_flag = bool(row.get("Exception"))
    line["has_exception"] = exception_flag
    line["exception_note"] = row.get("TS Note") or None
    line["exception_resolved"] = False
    line["reviewed"] = False
    line["is_edited"] = False
    line.pop("Exception", None)
    line.pop("TS Note", None)

    line["noHaul"] = _truthy(row.get("noHaul"))
    line["missingTonsServiced"] = _truthy(row.get("missingTonsServiced"))

    # Numeric fields: coerce blanks to None rather than empty strings
    for numeric_field in (
        "lineAmount", "binQuantity", "haulRate", "tonnageRate",
        "tonsBilled", "tonsServiced",
    ):
        val = line.get(numeric_field)
        line[numeric_field] = None if val in ("", None) else val

    return line


def build_header_record(row, bill_key):
    """Extracts the header-level record from the first row seen for a bill."""
    header = {field: row.get(field) for field in HEADER_FIELDS}
    normalized_bill_number, normalized_location_id, normalized_hauler_account_id = bill_key

    header["normalized_bill_number"] = normalized_bill_number
    header["normalized_location_id"] = normalized_location_id
    header["normalized_hauler_account_id"] = normalized_hauler_account_id

    # Fields populated later by other workflow steps, left as sensible defaults here
    header["resolved_location_id"] = None
    header["location_match_status"] = None
    header["resolved_hauler_account_id"] = None
    header["hauler_match_status"] = None
    header["review_status"] = "awaiting_review"
    header["checked_out_by"] = None
    header["checked_out_at"] = None
    header["duplicate_status"] = None
    header["duplicate_of_bill"] = None
    header["submission_batch_id"] = None
    header["system_bill_id"] = None

    return header


def transform_sheet_rows(sheet_rows, source_sheet_id):
    """
    Main entry point. Groups raw sheet rows into bills, and returns a list of

        {"header": {...}, "lines": [...]}

    ready for the next workflow steps (resolution/match/duplicate queries,
    then insert into staged_bills / staged_bill_lines).

    Rows that fail to resolve a usable bill_key (e.g. missing billNumber)
    are collected separately and returned alongside so they can be surfaced
    as ingestion errors rather than silently dropped or mis-grouped.
    """
    validate_headers(sheet_rows)

    bills = OrderedDict()  # bill_key -> {"header": {...}, "lines": [...]}
    skipped_rows = []

    for row in sheet_rows:
        bill_key = make_bill_key(row)

        if not all(bill_key):
            skipped_rows.append({
                "row": row,
                "reason": "Missing billNumber, locationId, or haulerAccountId — cannot group.",
            })
            continue

        if bill_key not in bills:
            bills[bill_key] = {
                "header": build_header_record(row, bill_key),
                "lines": [],
            }

        bills[bill_key]["lines"].append(build_line_record(row))

    result = []
    for bill_key, bill in bills.items():
        bill["header"]["source_sheet_id"] = source_sheet_id
        bill["header"]["line_count"] = len(bill["lines"])
        result.append(bill)

    return {
        "bills": result,
        "skipped_rows": skipped_rows,
        "bill_count": len(result),
        "line_count": sum(b["header"]["line_count"] for b in result),
    }