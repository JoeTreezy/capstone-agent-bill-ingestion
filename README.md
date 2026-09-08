# billIngestion

Automated pipeline for turning vendor waste-hauling bills (PDFs) into structured, validated data ready for review and submission into the billing system. An AI agent (Claude) reads each bill directly, matches it to the right customer location and hauler account, classifies every line item, and matches service charges against the real service catalog — flagging anything it's not confident about for a human to review, rather than guessing.

## What the pipeline actually does, in order

**1. Extraction** — Given a bill PDF, Claude reads it directly (no separate OCR step — Claude has native document understanding) and returns structured data: the bill's header info (bill number, address, hauler name, dates) and every line item (amount, description, quantities). At this point, each line's `line_type` (Fee, Charge, Credit, or a service charge) is still unknown — that gets figured out later, in Line Resolution.

**2. Location & Hauler Resolution** — Takes the bill's raw address and hauler name and figures out which real customer location and hauler account it corresponds to. Uses fuzzy text matching (not exact string matching — bills are never formatted exactly like your reference data) against real reference data, with three fallback layers when the match isn't obvious:
   - **Known corrections**: if a human has previously corrected this exact address/hauler before, reuse that answer instead of re-guessing.
   - **Confidence scoring**: matches are labeled `perfect_match`, `likely_match`, `possible_match`, or `no_match` based on how close the fuzzy match is and whether there's a clear winner.
   - **AI-adjudicated tie-breaking**: when a match is ambiguous (e.g., two very similar addresses, like a building and its Suite 2), a second Claude call reasons through the ambiguity using extra context (does this candidate have a hauler account matching the bill? has this exact bill resolved here before?) — and if the evidence genuinely doesn't settle it, it says so rather than guessing.

**3. Line Resolution** — Now that the bill's location and hauler are known, this classifies every line: is it a Fee, a Charge, a Credit, or an actual service charge? Different haulers format their bills differently (a "rule pack" per hauler encodes this), so a rolloff-and-tonnage pair on one hauler's bill might need pairing together by work order number, while another hauler's medical-waste ticket lines get grouped differently. Once classified, service-charge lines get matched against the real service catalog — and if a match fails, the system explains *why* (wrong bin size? wrong stream type? discontinued?) instead of just saying "no match."

Every stage is built around one principle: **a confident-but-wrong answer reaching the billing system silently is worse than an honest "I'm not sure, a human should look at this."** Every resolution step has a path to flag uncertainty rather than force a guess.

## Where to start reading

If you're new to this codebase, read the files in this order:

1. **`main.py`** — the clearest picture of how everything connects. `run_full_pipeline()` shows the whole flow: extract → resolve locations/haulers → resolve lines.
2. **`models.py`** — the shapes of data flowing through the pipeline (what a bill extraction looks like, what a resolution result looks like).
3. Then the three stage files, in pipeline order: `extraction.py` → `resolution.py` → `line_resolution.py`.

## File-by-file guide

### Entry points (the files you actually run)

- **`run_first_test.py`** — the simplest way to test the pipeline: point it at one local PDF, see what gets extracted. Doesn't touch any database by default if you set `OUTPUT_MODE=csv` or `excel` — just writes the results to a file so you can inspect them. Good for verifying Claude's extraction quality on a new bill before trusting it in the full pipeline.
- **`debug_extraction.py`** — a lower-level diagnostic than `run_first_test.py`. Calls Claude directly and prints the *raw* response (stop reason, token usage, every content block) with nothing parsed or interpreted. Use this when extraction is failing and you need to see exactly what Claude actually returned, not just that something went wrong.
- **`poll_and_process.py`** — the real, ongoing entry point for production use. Watches a Google Drive folder for new bill PDFs, downloads anything not already processed, and runs the full pipeline on it. Can run as a standalone continuous loop (`python poll_and_process.py`), or you can call its `check_for_new_files_and_process()` function once per invocation from an external scheduler (cron, Airflow, etc.) if you have one.

### The three pipeline stages

- **`extraction.py`** — Calls Claude with the bill PDF, gets back structured JSON (header + line items), and converts that into the shape needed for the `staged_bills`/`staged_bill_lines` database tables. Handles Claude occasionally wrapping its JSON response in markdown code fences despite being told not to (`strip_markdown_fences`) — a real thing that happens on real bills, not a hypothetical edge case.
- **`resolution.py`** — Two resolvers: `LocationResolver` (address → location) and `HaulerResolver` (hauler name → hauler account, narrowed down to just the accounts at the already-resolved location). Both follow the same pattern: check known corrections first, then fuzzy-match, then escalate to Claude if the match is ambiguous. `get_reference_data()` is where the real customer location/hauler account data comes from — a live database query, scoped to only the haulers this pipeline actually knows how to handle.
- **`line_resolution.py`** — The most complex file, and the one with the most business logic packed in. `resolve_lines()` classifies each line item using per-hauler rules (regex patterns matching how each hauler formats charges) and pairs related lines together (e.g., a dumpster switch charge and its tonnage charge, which arrive as two separate lines on the bill but represent one real event). `match_services()` then takes anything classified as a real service charge and matches it against the actual service catalog, with a diagnostic function (`diagnose_near_miss`) that explains why a near-match didn't qualify.

### Supporting infrastructure

- **`models.py`** — Data shape definitions (using Pydantic, which validates the shape automatically) for what comes out of extraction and what comes out of resolution. If you're trying to understand "what fields does a resolved bill actually have," look here.
- **`config.py`** — Every setting the pipeline needs, read from environment variables: API keys, both database connections, the output mode toggle, matching thresholds. If you need to change how something behaves without touching code, check here first.
- **`db.py`** — Handles the two separate database connections this pipeline uses: `retool` (where bill data actually lives, read AND written) and `coyote` (the production system holding real hauler/location/service data, read-only — this pipeline never writes there). Every database call elsewhere in the codebase says explicitly which of the two it means.
- **`sinks.py`** — Controls *where* results get written. Instead of every stage calling the database directly, they all go through a `sink`, which is either a real database write, or — if you set `OUTPUT_MODE=csv`/`excel` — writes to a file instead. Useful for testing without touching the database, or for cases where direct database write access isn't available.
- **`google_drive.py`** — Talks to Google Drive: lists files in the watched folder, downloads a given file's content. Used by `poll_and_process.py`.
- **`requirements.txt`** — Every external package the code actually imports (verified by installing into a clean environment and confirming everything still imports — not just written from memory).

## Configuration

Everything is controlled by environment variables (see `config.py` for the full list and defaults). The essentials:

```
ANTHROPIC_API_KEY=          # your Claude API key

RETOOL_DB_HOST=              # where staged_bills/staged_bill_lines live
RETOOL_DB_NAME=
RETOOL_DB_USER=
RETOOL_DB_PASSWORD=
RETOOL_DB_SSLMODE=require     # Retool DB requires this

COYOTE_DB_HOST=               # read-only: real hauler/location/service data
COYOTE_DB_NAME=
COYOTE_DB_USER=
COYOTE_DB_PASSWORD=

OUTPUT_MODE=database          # or: csv, excel
OUTPUT_DIR=./output           # only used if OUTPUT_MODE isn't database

DRIVE_FOLDER_ID=               # only needed for poll_and_process.py
DRIVE_SERVICE_ACCOUNT_JSON_PATH=
```

## Running it

**First test on a single bill**, no database needed:
```
OUTPUT_MODE=excel OUTPUT_DIR=./output python run_first_test.py /path/to/bill.pdf
```
Check `./output/output.xlsx` afterward — one sheet per table that would have been written.

**Something's failing during extraction and you need to see why**:
```
python debug_extraction.py /path/to/bill.pdf
```

**Ongoing, continuous processing** (needs all the database and Drive settings configured):
```
python poll_and_process.py
```
