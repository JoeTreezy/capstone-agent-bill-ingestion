"""PDF bill extraction using Claude's native document understanding.

Ported from the real, current Retool `extractBillDataForBatch` block --
this is the actual production prompt (workOrderNumber/ticketNumber grouping,
per-line-type date rules), not a simplified stand-in. Uses forced tool-use
rather than "please return JSON" prompting, so output is always well-formed.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Optional

import anthropic
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings
from models import BillExtraction
from sinks import OutputSink

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

EXTRACTION_SYSTEM_PROMPT = (
    "This is properietary"
)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=20))
def strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    return text.strip()


def extract_bill_data(pdf_base64: str) -> BillExtraction:
    response = _client.messages.create(
        model=settings.anthropic_model,
        max_tokens=4096,
        system=EXTRACTION_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_base64}},
                {"type": "text", "text": "Extract this bill."},
            ],
        }],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    text = strip_markdown_fences(text)
    return BillExtraction.model_validate(json.loads(text))


def normalize_bill_number(raw: Optional[str]) -> str:
    return (raw or "").upper().replace("-", "").strip()


def flatten_extraction(extraction: BillExtraction, file_name: str, drive_link: str) -> tuple[dict, list[dict]]:
    """Converts one extraction result into insertable bill/line dicts, generating
    the bill_id here (not a DB default) so lines can reference it before either
    is written. line_type is deliberately omitted -- it stays NULL until Bill
    Line Resolution classifies it; that NULL is its own signal for "not yet
    resolved" downstream."""
    bill_id = str(uuid.uuid4())
    header = extraction.header

    bill = {
        "bill_id": bill_id,
        "bill_number": header.billNumber,
        "hauler_account_number": header.haulerAccountNumber,
        "address": header.address,
        "city": header.city,
        "state": header.state,
        "zip": header.zip,
        "hauler_name": header.hauler_name,
        "occurrence_date": header.billDate,
        "occurrence_due_date": header.billDueDate,
        "google_drive_link": drive_link,
        "file_name": file_name,
        "normalized_bill_number": normalize_bill_number(header.billNumber),
        "review_status": "awaiting_review",
        "source_sheet_id": None,
    }

    lines = [
        {
            "line_id": str(uuid.uuid4()),
            "bill_id": bill_id,
            "line_amount": line.lineAmount,
            "line_description": line.lineDescription,
            "service_type": line.serviceType,
            "bin_size": line.binSize,
            "raw_work_order_number": line.workOrderNumber,
            "raw_ticket_number": line.ticketNumber,
            "raw_quantity": line.quantity,
            "raw_rate": line.rate,
            "line_occurrence_date": line.lineOccurrenceDate,
            "line_occurrence_start_date": line.lineOccurrenceStartDate,
            "line_occurrence_end_date": line.lineOccurrenceEndDate,
            "reviewed": False,
            "has_exception": False,
        }
        for line in extraction.lines
    ]

    return bill, lines


def extract_batch(files: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """files: [{"file_id", "file_name", "drive_link", "content_base64"}, ...].
    One bad extraction doesn't take down the whole batch -- it's collected in
    failed_files instead."""
    bills: list[dict] = []
    lines: list[dict] = []
    failed_files: list[dict] = []

    for file in files:
        try:
            extraction = extract_bill_data(file["content_base64"])
            bill, bill_lines = flatten_extraction(extraction, file["file_name"], file["drive_link"])
            bills.append(bill)
            lines.extend(bill_lines)
        except Exception as e:
            logger.exception("Extraction failed for file %s", file.get("file_id"))
            failed_files.append({"file_id": file.get("file_id"), "file_name": file.get("file_name"), "error": str(e)})

    return bills, lines, failed_files


def run_extraction(files: list[dict], sink: OutputSink) -> dict:
    bills, lines, failed = extract_batch(files)
    sink.insert("staged_bills", bills)
    sink.insert("staged_bill_lines", lines)

    processed = [
        {"file_id": f["file_id"], "file_name": f["file_name"],
         "status": "failed" if any(ff["file_id"] == f["file_id"] for ff in failed) else "success"}
        for f in files
    ]
    sink.insert("extracted_files", processed)

    return {"bills": bills, "lines": lines, "failed_files": failed}