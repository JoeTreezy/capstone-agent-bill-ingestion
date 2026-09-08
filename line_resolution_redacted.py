"""Bill line resolution: classifies staged_bill_lines rows (line_type is NULL
until this runs), pairs switch+tonnage and ticket-grouped lines per hauler,
and matches service-type lines against Coyote's rate_based_services/
cost_based_services catalog. Ported directly from the real, current Retool
blocks (resolve_lines, match_services) -- the rule packs, pairing logic, and
near-miss diagnostics are unchanged from production; only the Retool-specific
wiring (reading blocks named get_unresolved_lines.data etc.) was rewritten
into plain function arguments and real DB queries.
"""
"""
Retool code block "resolve_lines"

Reads unresolved rows from Block 1 (get_unresolved_lines) + bill context from
Block 2 (get_bill_contexts), groups/classifies them per hauler, and hands off
an UPDATE payload for every touched line_id -- both "primary" rows (which get
the full resolved field set) and "secondary" rows absorbed into a merge
(which get only merged_into_line_id set, per Joe's call to keep the merge
event as an audit trail / future training data rather than deleting the row).

NOTE: no work_order_number/ticket_number-based grouping can run for any bill
where those raw columns are null -- that's expected for existing/backfilled
rows extracted before the schema change; new extractions should populate them.
"""

import calendar
import re
from datetime import date
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Static reference data (unchanged from the design spec)
# ---------------------------------------------------------------------------
STREAM_TYPE_IDS = {"MSW": 1, "SSRY": 2, "OCC": 3, "Wood": 17, "Medical Waste": 23}

MEDICAL_WASTE_PATTERN = re.compile(r"med(?:ical)?\s*wa?st?e", re.I)
STREAM_KEYWORD_FALLBACK = [
    (re.compile(r"\btrash\b", re.I), "MSW"),
    (re.compile(r"\bwaste\b", re.I), "MSW"),
    (re.compile(r"\brecycl", re.I), "SSRY"),
    (re.compile(r"\bcardboard\b", re.I), "OCC"),
    (re.compile(r"\bocc\b", re.I), "OCC"),
    (re.compile(r"\bwood\b", re.I), "Wood"),
]


def fallback_stream_type_id(description: str) -> Optional[int]:
    if MEDICAL_WASTE_PATTERN.search(description):
        return STREAM_TYPE_IDS["Medical Waste"]
    for pattern, stream_name in STREAM_KEYWORD_FALLBACK:
        if pattern.search(description):
            return STREAM_TYPE_IDS[stream_name]
    return None


# stream_type (distinct from `stream`) is a coarse Waste/Recycling binary used
# for tax purposes on Charge/Credit lines only. When a charge/credit can be
# tied to a resolved service line (same work order), it inherits that
# service's stream, mapped down to the binary. Otherwise: keyword guess,
# defaulting to "Waste" when undeterminable (per Joe).
STREAM_ID_TO_TAX_CATEGORY = {
    STREAM_TYPE_IDS["MSW"]: "Waste",
    STREAM_TYPE_IDS["SSRY"]: "Recycling",
    STREAM_TYPE_IDS["OCC"]: "Recycling",
    STREAM_TYPE_IDS["Wood"]: "Waste",
    STREAM_TYPE_IDS["Medical Waste"]: "Waste",
}

# What each line_type needs before it's considered fully resolved (not just
# classified). Note tonnage_rate/tons_billed are deliberately NOT required for
# RateBasedBillService -- a standalone switch with no ton-charge pairing
# (no_haul=False, missing_tons_serviced=True) is a valid, complete resolution,
# not a gap.
REQUIRED_FIELDS_BY_LINE_TYPE = {
    "Fee": ["line_fee_type", "line_amount"],
    "Charge": ["line_adjustment_type", "line_amount"],
    "Credit": ["line_adjustment_type", "line_amount"],
    "CostBasedBillService": ["removal", "bin_size", "stream", "line_amount"],
    "RateBasedBillService": ["removal", "bin_size", "stream", "haul_rate", "line_amount"],
}


def infer_stream_type_category(description: str) -> str:
    if re.search(r"recycl|cardboard|\bocc\b", description, re.I):
        return "Recycling"
    return "Waste"


# ---------------------------------------------------------------------------
# Per-hauler rule packs -- PascalCase line_type values now, matching the
# real column's enum. credit_type folds into line_adjustment_type since
# there's no separate credit_type column.
# ---------------------------------------------------------------------------
@dataclass
class HaulerRulePack:
    fee_patterns: List[Tuple[re.Pattern, str]]
    charge_keyword_map: List[Tuple[re.Pattern, str]]
    credit_keyword_map: List[Tuple[re.Pattern, str]]
    cost_based_service_pattern: re.Pattern
    cost_based_removal_type: str = "Rear Load"
    switch_pattern: Optional[re.Pattern] = None
    ton_charge_pattern: Optional[re.Pattern] = None
    switch_removal_type_map: Optional[Dict[str, str]] = None
    pairing_group_key: str = "work_order_number"
    ticket_grouped_pattern: Optional[re.Pattern] = None
    ticket_group_config: Optional[dict] = None
    ticket_group_quantity_pattern: Optional[re.Pattern] = None


HAULER_ONE = HaulerRulePack(
    # proprietary
    fee_patterns=[]
)

HAULER_TWO = HaulerRulePack(
    #properietary
    fee_patterns=[]
)

HAULER_RULE_PACKS = {"hauler_one": HAULER_ONE, "hauker_two": HAULER_TWO}

HAULER_NAME_TO_RULE_PACK = {
    "hauler one": "hauler_one",
    "hauler two": "hauler_two"
}


def pick_rule_pack_key(hauler_name: str) -> str:
    name = (hauler_name or "").lower()
    for needle, key in HAULER_NAME_TO_RULE_PACK.items():
        if needle in name:
            return key
    raise ValueError(f"No rule pack mapped for hauler name: {hauler_name!r}")


def _to_float(value) -> Optional[float]:
    # Retool's Postgres resource returns numeric columns as strings (to avoid
    # JS float precision loss), not JSON numbers -- cast defensively rather
    # than assuming a type.
    if value is None or value == "":
        return None
    return float(value)


@dataclass
class RawLine:
    line_id: str
    bill_id: str
    work_order_number: Optional[str]
    ticket_number: Optional[str]
    description: str
    amount: float
    quantity: Optional[float]
    rate: Optional[float]
    service_type: Optional[str]


def infer_cost_based_end_date(start_date):
    """CostBasedBillService lines often only print a start date on the bill --
    per Joe, the end date defaults to the end of that same month unless the
    bill explicitly states a different end date (in which case extraction
    would have already populated it, and this function is never called for
    that line)."""
    if start_date is None:
        return None
    last_day = calendar.monthrange(start_date.year, start_date.month)[1]
    return date(start_date.year, start_date.month, last_day)


def blank_update() -> dict:
    """Every field a resolved update might set, defaulted to None so Block 6's
    UPDATE always sees every column explicitly rather than relying on partial
    JSON keys."""
    return {
        "line_type": None, "line_adjustment_type": None, "line_fee_type": None,
        "removal": None, "bin_size": None, "bin_quantity": None, "stream": None,
        "stream_type": None,
        "resolved_service_id": None, "service_match_status": None,
        "haul_rate": None, "tonnage_rate": None, "tons_billed": None,
        "tons_serviced": None, "no_haul": None, "missing_tons_serviced": None,
        "line_amount": None, "has_exception": False, "exception_note": None,
        "merged_into_line_id": None,
    }


def classify_single(line: RawLine, rules: HaulerRulePack) -> dict:
    """Fee / credit / charge / cost-based classification for a line that
    isn't part of a switch+ton or ticket-grouped pairing."""
    u = blank_update()

    for pattern, fee_type in rules.fee_patterns:
        if pattern.search(line.description):
            u.update(line_type="Fee", line_fee_type=fee_type, line_amount=line.amount)
            return u

    for pattern, credit_type in rules.credit_keyword_map:
        if pattern.search(line.description):
            u.update(line_type="Credit", line_adjustment_type=credit_type, line_amount=line.amount,
                      stream_type=infer_stream_type_category(line.description))
            return u

    for pattern, adjustment_type in rules.charge_keyword_map:
        if pattern.search(line.description):
            u.update(line_type="Charge", line_adjustment_type=adjustment_type, line_amount=line.amount,
                      stream_type=infer_stream_type_category(line.description))
            return u

    service_match = rules.cost_based_service_pattern.search(line.description)
    if service_match:
        bin_size = float(service_match.group(1))
        stream_id = fallback_stream_type_id(line.description) or STREAM_TYPE_IDS["MSW"]
        u.update(
            line_type="CostBasedBillService",
            removal=rules.cost_based_removal_type,
            bin_size=bin_size,
            bin_quantity=line.quantity,
            stream=stream_id,
            line_amount=line.amount,
        )
        return u

    # unrecognized -- don't guess, flag for review
    u.update(
        line_type="Charge", line_adjustment_type="Other", line_amount=line.amount,
        stream_type=infer_stream_type_category(line.description),
        has_exception=True, exception_note="unrecognized_charge_description",
    )
    return u


def group_and_resolve(raw_lines: List[RawLine], rules: HaulerRulePack) -> Dict[str, dict]:
    """Returns {line_id: update_dict} for every line_id touched, including
    both primary (fully resolved) and secondary (merged_into_line_id only)
    rows from any pairing/grouping."""
    updates: Dict[str, dict] = {}
    remaining = list(raw_lines)

    # -- ticket grouping --
    if rules.ticket_grouped_pattern:
        by_ticket: Dict[str, List[RawLine]] = {}
        leftover = []
        for line in remaining:
            if line.ticket_number and rules.ticket_grouped_pattern.search(line.description):
                by_ticket.setdefault(line.ticket_number, []).append(line)
            else:
                leftover.append(line)
        cfg = rules.ticket_group_config
        for ticket_lines in by_ticket.values():
            # primary = the line with the nonzero amount (the real charge);
            # everything else in the ticket group becomes secondary
            primary = max(ticket_lines, key=lambda l: l.amount)
            total = sum(l.amount for l in ticket_lines)
            tons_billed = None
            for l in ticket_lines:
                if rules.ticket_group_quantity_pattern:
                    m = rules.ticket_group_quantity_pattern.search(l.description)
                    if m:
                        tons_billed = float(m.group(1))
                        break
            u = blank_update()
            u.update(
                line_type="RateBasedBillService",
                removal=cfg["removal_type"], bin_size=cfg["bin_size"], stream=cfg["stream_type_id"],
                haul_rate=0.0, tonnage_rate=None, tons_billed=tons_billed, tons_serviced=tons_billed,
                no_haul=False, missing_tons_serviced=True, line_amount=total,
            )
            updates[primary.line_id] = u
            for l in ticket_lines:
                if l.line_id != primary.line_id:
                    updates[l.line_id] = {**blank_update(), "merged_into_line_id": primary.line_id}
        remaining = leftover

    # -- switch+ton pairing, keyed by whichever field this hauler groups on
    # --
    if rules.switch_pattern:
        group_key_field = rules.pairing_group_key
        by_group: Dict[Optional[str], List[RawLine]] = {}
        leftover = []
        for line in remaining:
            by_group.setdefault(getattr(line, group_key_field), []).append(line)
        for group_lines in by_group.values():
            switch_lines = [l for l in group_lines if rules.switch_pattern.search(l.description)]
            ton_lines = [l for l in group_lines if rules.ton_charge_pattern.search(l.description)]
            used = set()
            for switch in switch_lines:
                m = rules.switch_pattern.search(switch.description)
                bin_size = float(m.group(1))
                removal_type = rules.switch_removal_type_map[m.group(2).upper()]
                stream_id = fallback_stream_type_id(switch.description) or STREAM_TYPE_IDS["MSW"]
                paired_ton = next((t for t in ton_lines if t.line_id not in used), None)

                u = blank_update()
                if paired_ton:
                    used.update({switch.line_id, paired_ton.line_id})
                    # tonnage_rate: use raw_rate if the bill printed one; else
                    # derive rate = amount / quantity from the ton line's own
                    # printed figures (not extraction back-deriving a guess --
                    # this is resolution doing arithmetic over two numbers the
                                       # separate Rate column at all)
                    tonnage_rate = paired_ton.rate
                    if tonnage_rate is None and paired_ton.quantity:
                        tonnage_rate = paired_ton.amount / paired_ton.quantity
                    u.update(
                        line_type="RateBasedBillService", removal=removal_type, bin_size=bin_size,
                        stream=stream_id,
                        haul_rate=switch.amount, tonnage_rate=tonnage_rate,
                        tons_billed=paired_ton.quantity, tons_serviced=paired_ton.quantity,
                        no_haul=False, missing_tons_serviced=False,
                        line_amount=switch.amount + paired_ton.amount,
                    )
                    updates[switch.line_id] = u
                    updates[paired_ton.line_id] = {**blank_update(), "merged_into_line_id": switch.line_id}
                else:
                    used.add(switch.line_id)
                    u.update(
                        line_type="RateBasedBillService", removal=removal_type, bin_size=bin_size,
                        stream=stream_id,
                        haul_rate=switch.amount, no_haul=False, missing_tons_serviced=True,
                        line_amount=switch.amount,
                    )
                    updates[switch.line_id] = u
            for l in group_lines:
                if l.line_id not in used:
                    leftover.append(l)
        remaining = leftover

    # -- everything left: single-line classification --
    for line in remaining:
        updates[line.line_id] = classify_single(line, rules)

    # -- inherit stream_type from a same-group service line, where one
    # exists, overriding the keyword-only guess made above. Groups on
    # whichever field pairing used (work_order_number or ticket_number) --
    # this only applies to Charge/Credit lines; service lines set `stream`
    # directly, not `stream_type` (different columns/purposes).
    group_key_field = rules.pairing_group_key
    group_key_by_line_id = {l.line_id: getattr(l, group_key_field) for l in raw_lines}
    service_stream_by_group = {}
    for line_id, u in updates.items():
        gk = group_key_by_line_id.get(line_id)
        if gk and u.get("line_type") in ("CostBasedBillService", "RateBasedBillService") and u.get("stream") is not None:
            service_stream_by_group[gk] = STREAM_ID_TO_TAX_CATEGORY.get(u["stream"], "Waste")

    for line_id, u in updates.items():
        if u.get("line_type") in ("Charge", "Credit"):
            gk = group_key_by_line_id.get(line_id)
            if gk and gk in service_stream_by_group:
                u["stream_type"] = service_stream_by_group[gk]

    # -- systematic required-fields check, keyed by line_type. Runs on every
    # resolved (non-merged-away) line rather than relying on ad hoc flags
    # scattered through classification. Mirrors the intent of the
    # line_type_required_fields table.
    for line_id, u in updates.items():
        if u.get("merged_into_line_id") is not None:
            continue  # secondary rows aren't independently resolved -- skip
        missing = [f for f in REQUIRED_FIELDS_BY_LINE_TYPE.get(u.get("line_type"), []) if u.get(f) is None]
        if missing:
            u["has_exception"] = True
            note = f"missing_required_fields: {', '.join(missing)}"
            u["exception_note"] = f"{u['exception_note']}; {note}" if u.get("exception_note") else note

    return updates


def resolve_lines(unresolved_lines: List[dict], bill_contexts: List[dict]) -> dict:
    """Direct port of the real Retool wiring section -- same logic, now a
    proper function taking plain arguments instead of reading Retool block
    references directly."""
    bill_contexts_by_id = {row["bill_id"]: row for row in bill_contexts}

    rows_by_bill: Dict[str, List[dict]] = {}
    for row in unresolved_lines:
        rows_by_bill.setdefault(row["bill_id"], []).append(row)

    all_updates = []

    for bill_id, rows in rows_by_bill.items():
        context = bill_contexts_by_id.get(bill_id)
        if context is None:
            for row in rows:
                u = blank_update()
                u.update(has_exception=True, exception_note="missing_bill_context")
                all_updates.append({"line_id": row["line_id"], **u})
            continue

        try:
            rule_pack_key = pick_rule_pack_key(context["hauler_name"])
        except ValueError:
            for row in rows:
                u = blank_update()
                u.update(has_exception=True, exception_note=f"no_rule_pack_for_hauler: {context['hauler_name']!r}")
                all_updates.append({
                    "line_id": row["line_id"], "bill_id": bill_id,
                    "location_id": context["location_id"], "hauler_account_id": context["hauler_account_id"],
                    "bill_date": context["bill_date"], **u,
                })
            continue

        rules = HAULER_RULE_PACKS[rule_pack_key]

        raw_lines = [
            RawLine(
                line_id=row["line_id"], bill_id=bill_id,
                work_order_number=row["raw_work_order_number"], ticket_number=row["raw_ticket_number"],
                description=row["line_description"], amount=_to_float(row["line_amount"]),
                quantity=_to_float(row["raw_quantity"]), rate=_to_float(row["raw_rate"]),
                service_type=row["service_type"],
            )
            for row in rows
        ]
        raw_row_by_line_id = {row["line_id"]: row for row in rows}

        updates = group_and_resolve(raw_lines, rules)
        for line_id, u in updates.items():
            raw_row = raw_row_by_line_id.get(line_id, {})
            entry = {
                "line_id": line_id, "bill_id": bill_id, "location_id": context["location_id"],
                "hauler_account_id": context["hauler_account_id"], "bill_date": context["bill_date"],
                "line_occurrence_date": raw_row.get("line_occurrence_date"),
                "line_occurrence_start_date": raw_row.get("line_occurrence_start_date"),
                "line_occurrence_end_date": raw_row.get("line_occurrence_end_date"),
                **u,
            }
            # Only infer the end date if the bill genuinely didn't state one --
            # respects an explicit end date from extraction rather than
            # overriding it, and only applies to CostBasedBillService, which
            # is the line_type Joe specifically described this rule for.
            if (entry.get("line_type") == "CostBasedBillService"
                    and entry.get("line_occurrence_start_date") is not None
                    and entry.get("line_occurrence_end_date") is None):
                entry["line_occurrence_end_date"] = infer_cost_based_end_date(entry["line_occurrence_start_date"])
            all_updates.append(entry)

    return {"resolved_lines": all_updates}


# Retool code block "match_services"
# Matches every RateBasedBillService/CostBasedBillService line against its
# own (location_id, hauler_id) candidate services, on (removal_type, bin_size,
# bin_quantity, stream_type_id, rates). Skips lines with no line_type set here
# (fees/charges/credits/secondary-merged rows never need a service match).
# service_type from extraction (Joe: "rolloff" -> RateBasedBillService,
# "front load" -> CostBasedBillService) is a useful cross-check but the
# line_type Block 3 already assigned from description patterns is authoritative
# here -- this block only runs the match, it doesn't re-classify.

RATE_MATCH_TOLERANCE = 0.01
# tonnage_rate is often derived as amount/quantity (haulers don't print
# a rate column directly), which carries real floating-point and business
# rounding noise proportional to the rate's own size -- a fixed cents-based
# tolerance doesn't scale for that the way a percentage does. haul_rate is
# usually printed directly on the bill, so it stays on the tighter fixed
# tolerance above.
TONNAGE_RATE_RELATIVE_TOLERANCE = 0.005  # 0.5%


def _id(value):
    # Retool's Postgres driver returns some numeric columns as strings and
    # others (e.g. values explicitly cast with ::int in SQL) as native JS
    # numbers, depending on the underlying Postgres type -- inconsistent
    # across the different sources this block reads from (staged_bills via
    # get_bill_contexts vs. cast expressions in get_hauler_id_mapping vs.
    # native integer columns on rate_based_services/cost_based_services).
    # Normalize every id to int the moment it's read, so dict lookups and
    # equality checks never silently fail on "124357" != 124357.
    if value is None or value == "":
        return None
    return int(value)


def rate_matches(bill_value, catalog_value, relative_tolerance=None):
    if bill_value is None and catalog_value is None:
        return True
    if bill_value is None or catalog_value is None:
        return False
    tolerance = RATE_MATCH_TOLERANCE
    if relative_tolerance is not None:
        tolerance = max(tolerance, abs(catalog_value) * relative_tolerance)
    return abs(bill_value - catalog_value) <= tolerance


def find_matches(line, candidates):
    kind = "rate" if line["line_type"] == "RateBasedBillService" else "cost"
    matches = []
    for c in candidates:
        if c["service_kind"] != kind:
            continue
        if c["discontinued"]:
            continue

        if kind == "rate":
            # rate-based: the line's own occurrence date must fall within the
            # service's active [start_date, end_date] range
            line_date = line.get("line_occurrence_date")
            if line_date is None:
                continue  # can't date-match without it -- falls to one_off, not a guess
            if c["start_date"] and c["start_date"] > line_date:
                continue
            if c["end_date"] and c["end_date"] < line_date:
                continue
        else:
            # cost-based: the SERVICE's start_date must fall within the
            # LINE's own occurrence window (per Joe -- not a general overlap
            # check, the service must have started during this specific
            # billing period)
            line_start = line.get("line_occurrence_start_date")
            line_end = line.get("line_occurrence_end_date")
            if line_start is None or line_end is None or c["start_date"] is None:
                continue
            if not (line_start <= c["start_date"] <= line_end):
                continue

        if c["removal_type"] != line["removal"]:
            continue
        if c["bin_size"] != line["bin_size"]:
            continue
        if line.get("bin_quantity") is not None and c["bin_quantity"] != line["bin_quantity"]:
            continue
        if c["stream_type_id"] != line["stream"]:
            continue
        if kind == "rate":
            if not rate_matches(line.get("haul_rate"), c.get("bill_haul_rate")):
                continue
            if not rate_matches(line.get("tonnage_rate"), c.get("bill_tonnage_rate"), relative_tolerance=TONNAGE_RATE_RELATIVE_TOLERANCE):
                continue
        matches.append(c)
    return matches


def diagnose_near_miss(line, candidates):
    """When find_matches returns zero results, explain why the closest
    candidate(s) got rejected -- checked in the same order find_matches uses,
    so the first reason listed is the actual rejection reason, not just any
    mismatch. Returns a short string for exception_note, or None if there
    were no same-kind candidates to compare against at all."""
    kind = "rate" if line["line_type"] == "RateBasedBillService" else "cost"
    pool = [c for c in candidates if c["service_kind"] == kind and not c["discontinued"]]
    if not pool:
        return "no active candidate services found for this location/hauler"

    best_id, best_reasons = None, None
    for c in pool:
        reasons = []
        if kind == "rate":
            line_date = line.get("line_occurrence_date")
            if line_date is None:
                reasons.append("line_occurrence_date missing")
            elif c["start_date"] and c["start_date"] > line_date:
                reasons.append(f"line date {line_date} is before service start_date {c['start_date']}")
            elif c["end_date"] and c["end_date"] < line_date:
                reasons.append(f"line date {line_date} is after service end_date {c['end_date']}")
        else:
            line_start = line.get("line_occurrence_start_date")
            line_end = line.get("line_occurrence_end_date")
            if line_start is None or line_end is None:
                reasons.append("line_occurrence_start/end_date missing")
            elif c["start_date"] is None:
                reasons.append("candidate service start_date missing")
            elif not (line_start <= c["start_date"] <= line_end):
                reasons.append(f"service start_date {c['start_date']} is outside line window [{line_start}, {line_end}]")

        if c["removal_type"] != line["removal"]:
            reasons.append(f"removal: line={line['removal']!r} vs service={c['removal_type']!r}")
        if c["bin_size"] != line["bin_size"]:
            reasons.append(f"bin_size: line={line['bin_size']!r} vs service={c['bin_size']!r}")
        if line.get("bin_quantity") is not None and c["bin_quantity"] != line["bin_quantity"]:
            reasons.append(f"bin_quantity: line={line['bin_quantity']!r} vs service={c['bin_quantity']!r}")
        if c["stream_type_id"] != line["stream"]:
            reasons.append(f"stream: line={line['stream']!r} vs service={c['stream_type_id']!r}")
        if kind == "rate":
            if not rate_matches(line.get("haul_rate"), c.get("bill_haul_rate")):
                reasons.append(f"haul_rate: line={line.get('haul_rate')!r} vs service={c.get('bill_haul_rate')!r}")
            if not rate_matches(line.get("tonnage_rate"), c.get("bill_tonnage_rate"), relative_tolerance=TONNAGE_RATE_RELATIVE_TOLERANCE):
                reasons.append(f"tonnage_rate: line={line.get('tonnage_rate')!r} vs service={c.get('bill_tonnage_rate')!r}")

        # "closest" = fewest mismatching fields
        if best_reasons is None or len(reasons) < len(best_reasons):
            best_id, best_reasons = c["id"], reasons

    return f"nearest candidate service id={best_id} rejected on: {'; '.join(best_reasons)}"



def _normalize_candidate(c):
    # Coyote's driver returns some numeric columns as strings and others as
    # native numbers depending on the underlying Postgres type -- normalize
    # every id/number to a consistent Python type here, once, rather than at
    # each comparison site, so equality checks never silently fail on
    # "0.33" != 0.33.
    c["id"] = _id(c["id"])
    c["location_id"] = _id(c["location_id"])
    c["hauler_id"] = _id(c["hauler_id"])
    c["stream_type_id"] = _id(c["stream_type_id"])
    c["bin_size"] = None if c["bin_size"] in (None, "") else float(c["bin_size"])
    c["bin_quantity"] = None if c["bin_quantity"] in (None, "") else float(c["bin_quantity"])
    c["bill_haul_rate"] = None if c.get("bill_haul_rate") in (None, "") else float(c["bill_haul_rate"])
    c["bill_tonnage_rate"] = None if c.get("bill_tonnage_rate") in (None, "") else float(c["bill_tonnage_rate"])
    return c


def match_services(resolved_lines: List[dict], candidate_services: List[dict], hauler_id_mapping: List[dict]) -> dict:
    """Direct port of match_services' real logic -- candidate normalization,
    hauler_account_id -> hauler_id translation, and the perfect/one_off/
    multiple-candidates matching decision, unchanged from production."""
    candidates = [_normalize_candidate(dict(c)) for c in candidate_services]

    account_to_hauler_id = {
        (_id(row["location_id"]), _id(row["hauler_account_id"])): _id(row["hauler_id"])
        for row in hauler_id_mapping
    }

    final_lines = []
    for line in resolved_lines:
        if line.get("line_type") in ("CostBasedBillService", "RateBasedBillService"):
            location_id = _id(line.get("location_id"))
            hauler_account_id = _id(line.get("hauler_account_id"))
            hauler_id = account_to_hauler_id.get((location_id, hauler_account_id))

            is_rate = line["line_type"] == "RateBasedBillService"
            missing_occurrence_data = (
                line.get("line_occurrence_date") is None if is_rate
                else line.get("line_occurrence_start_date") is None or line.get("line_occurrence_end_date") is None
            )

            if location_id is None or hauler_account_id is None or hauler_id is None:
                line["service_match_status"] = "one_off"
                line["has_exception"] = True
                line["exception_note"] = "location_or_hauler_not_yet_resolved"
            elif missing_occurrence_data:
                line["service_match_status"] = "one_off"
                line["has_exception"] = True
                line["exception_note"] = "line_occurrence_date_not_set" if is_rate else "line_occurrence_window_not_set"
            else:
                own_candidates = [c for c in candidates if c["location_id"] == location_id and c["hauler_id"] == hauler_id]
                matches = find_matches(line, own_candidates)
                if len(matches) == 1:
                    line["resolved_service_id"] = matches[0]["id"]
                    line["service_match_status"] = "perfect_match"
                else:
                    line["service_match_status"] = "one_off"
                    line["has_exception"] = True
                    if len(matches) > 1:
                        line["exception_note"] = "multiple_service_candidates"
                    else:
                        line["exception_note"] = diagnose_near_miss(line, own_candidates)
        final_lines.append(line)

    return {"final_lines": final_lines}


# ---------------------------------------------------------------------------
# Real DB-backed queries (Retool block equivalents) and orchestration
# ---------------------------------------------------------------------------
import db
from sinks import OutputSink


def get_unresolved_lines(bill_ids: list[str]) -> list[dict]:
    if not bill_ids:
        return []
    return db.fetch_all(
        "select sbl.line_id, sbl.bill_id, sbl.line_amount, sbl.line_description, "
        "sbl.service_type, sbl.raw_work_order_number, sbl.raw_ticket_number, "
        "sbl.raw_quantity, sbl.raw_rate, sbl.line_occurrence_date, "
        "sbl.line_occurrence_start_date, sbl.line_occurrence_end_date "
        "from staged_bill_lines sbl join staged_bills sb on sb.bill_id = sbl.bill_id "
        "where sbl.bill_id = any(%s::uuid[]) and sbl.line_type is null "
        "order by sbl.bill_id, sbl.line_id",
        (bill_ids,),
    )


def get_bill_contexts(bill_ids: list[str]) -> list[dict]:
    if not bill_ids:
        return []
    return db.fetch_all(
        "select bill_id, resolved_location_id as location_id, "
        "resolved_hauler_account_id as hauler_account_id, hauler_name, "
        "occurrence_date as bill_date "
        "from staged_bills where bill_id = any(%s::uuid[])",
        (bill_ids,),
    )


def get_hauler_id_mapping(bill_contexts: list[dict]) -> list[dict]:
    """Resolves each (location_id, hauler_account_id) PAIR from get_bill_contexts
    to Coyote's real hauler_id, via hauler_accounts.

    IMPORTANT: must preserve pairing between location_id and hauler_account_id --
    an earlier version of this function deduplicated the two id lists SEPARATELY
    and passed them to two independent unnest() calls, which silently drops any
    pair where a location_id or hauler_account_id repeats across different bills
    (e.g. two bills at the same location with different haulers). That's a
    silent-failure bug, not just an inefficiency: the dropped pair never raises
    an error, it just never gets a hauler_id, and downstream treats it as
    "not yet resolved" even though it should have resolved correctly.

    Matches the real Retool SQL's approach: build a JSON array of paired
    {location_id, hauler_account_id} objects and expand it with
    jsonb_array_elements, so the pairing survives all the way to the join.
    """
    import json as _json

    pairs = [
        {"location_id": row["location_id"], "hauler_account_id": row["hauler_account_id"]}
        for row in bill_contexts
        if row.get("location_id") not in (None, "") and row.get("hauler_account_id") not in (None, "")
    ]
    if not pairs:
        return []

    return db.fetch_all(
    # proprietary
        (_json.dumps(pairs),),
        pool_name="coyote",
    )


def get_candidate_services(hauler_id_mapping: list[dict]) -> list[dict]:
    pairs = [(row["location_id"], row["hauler_id"]) for row in hauler_id_mapping]
    if not pairs:
        return []
    location_ids = [p[0] for p in pairs]
    hauler_ids = [p[1] for p in pairs]
    return db.fetch_all(
    # proprietary
        (location_ids, hauler_ids),
        pool_name="coyote",
    )


def get_bill_ids_ready_for_line_resolution() -> list[str]:
    """Queries the actual current database state for bills ready for line
    resolution -- both location AND hauler resolved, and still has at least
    one line with line_type IS NULL (extraction's signal for "not yet
    classified"). Deliberately NOT based on this run's in-memory
    location_results/hauler_results -- a bill resolved in an earlier run
    (or resolved for location this run but hauler last run, or vice versa)
    is just as ready as one resolved fully in this exact invocation, and
    computing readiness from batch-local results alone silently misses it."""
    rows = db.fetch_all(
        "select distinct sb.bill_id from staged_bills sb "
        "join staged_bill_lines sbl on sbl.bill_id = sb.bill_id "
        "where sb.resolved_location_id is not null "
        "and sb.resolved_hauler_account_id is not null "
        "and sbl.line_type is null"
    )
    return [row["bill_id"] for row in rows]


def run_line_resolution(bill_ids: list[str], sink: OutputSink) -> dict:
    unresolved_lines = get_unresolved_lines(bill_ids)
    bill_contexts = get_bill_contexts(bill_ids)

    resolved = resolve_lines(unresolved_lines, bill_contexts)

    hauler_id_mapping = get_hauler_id_mapping(bill_contexts)
    candidate_services = get_candidate_services(hauler_id_mapping)

    matched = match_services(resolved["resolved_lines"], candidate_services, hauler_id_mapping)
    #properietary
    update_columns = [
        "line_type"
    ]
    updates = [{"line_id": line["line_id"], **{c: line.get(c) for c in update_columns}} for line in matched["final_lines"]]
    sink.update("staged_bill_lines", updates, key_column="line_id")

    return matched