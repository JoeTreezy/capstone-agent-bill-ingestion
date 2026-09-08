"""First-run smoke test: extract one local bill PDF, write results to Excel.

Run from the command line with a path:
    OUTPUT_MODE=excel OUTPUT_DIR=./output python run_first_test.py /path/to/your/bill.pdf

Or just hit Debug/Run in PyCharm with no arguments -- edit DEFAULT_TEST_PDF_PATH
below to point at a real bill on your machine first.
"""
import base64
import sys
import uuid
from pathlib import Path

from extraction import run_extraction
from sinks import get_output_sink

# Edit this to point at a real bill PDF on your machine, so PyCharm's Debug
# button works with zero configuration -- no need to set script parameters
# in a Run Configuration just to test something quickly.
DEFAULT_TEST_PDF_PATH = ""


def load_pdf_as_file_dict(pdf_path: str) -> dict:
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(
            f"No file at {path.resolve()} -- pass a real path as a command-line "
            f"argument, or edit DEFAULT_TEST_PDF_PATH at the top of this script."
        )
    raw_bytes = path.read_bytes()
    return {
        "file_id": str(uuid.uuid4()),       # stands in for a real Drive file ID
        "file_name": path.name,
        "drive_link": f"file://{path.resolve()}",  # stands in for a real Drive share link
        "content_base64": base64.b64encode(raw_bytes).decode("utf-8"),
    }


if __name__ == "__main__":
    pdf_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TEST_PDF_PATH

    file_dict = load_pdf_as_file_dict(pdf_path)
    sink = get_output_sink()

    result = run_extraction([file_dict], sink)

    print(f"Extracted {len(result['bills'])} bill(s), {len(result['lines'])} line(s)")
    if result["failed_files"]:
        print("Failures:", result["failed_files"])
    print("Check your OUTPUT_DIR for output.xlsx (or the bill/line CSVs, if OUTPUT_MODE=csv)")