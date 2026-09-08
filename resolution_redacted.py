"""Location and hauler-account resolution: structured pre-filter + rapidfuzz
ranking, known-corrections short-circuit, and Tree-of-Thought escalation on
ambiguous cases. Matching logic ported directly from the real, current Retool
blocks (resolveLocationsForBatch / resolveHaulersForBatch) -- including the
zip-truncation fix and the token_set_ratio switch for hauler names, both of
which fixed real match failures in production.
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict

import anthropic
from rapidfuzz import fuzz
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings
from models import LocationResolution, HaulerResolution, ToTDecision
from sinks import OutputSink
import db

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

ABBREVIATIONS = {
    r"\bMT\b": "MOUNT", r"\bAVE\b": "AVENUE", r"\bST\b": "STREET", r"\bSTE\b": "SUITE",
    r"\bBLVD\b": "BOULEVARD", r"\bDR\b": "DRIVE", r"\bRD\b": "ROAD",
    r"\bLN\b": "LANE", r"\bPKWY\b": "PARKWAY", r"\bCT\b": "COURT",
}


def normalize_full_address(address, city, state, zip_code) -> str:
    text = f"{address or ''} {city or ''} {state or ''} {zip_code or ''}".upper().strip()
    text = re.sub(r"[.,]", "", text)
    for abbr, full in ABBREVIATIONS.items():
        text = re.sub(abbr, full, text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_zip(zip_code) -> str:
    """Truncate to the base 5-digit zip -- '28208-1131' and '28208' must
    produce the same key regardless of which side carries the ZIP+4 suffix."""
    if not zip_code:
        return ""
    match = re.match(r"(\d{5})", str(zip_code).strip())
    return match.group(1) if match else str(zip_code).strip()


def normalize_text(text) -> str:
    return (text or "").upper().strip()


def _bucket_confidence(is_exact: bool, best_score: float, second_score: float) -> str:
    if is_exact:
        return "perfect_match"
    if best_score >= settings.fuzzy_match_high_threshold and (best_score - second_score) >= settings.fuzzy_match_gap_threshold:
        return "likely_match"
    if best_score >= settings.fuzzy_match_low_threshold:
        return "possible_match"
    return "no_match"


def get_reference_data() -> list[dict]:
    """Real query, provided directly -- the same one Mode's report used
    (getModeQueryRuns -> filterRuns -> getQueryExport -> normalizeAddress in
    the Retool version). Replaces my earlier inferred join with the actual
    schema: hauler_accounts_to_locations (junction table), placemarks
    (polymorphic location data, filtered to placemarkable_type='Location'),
    hauler_accounts, haulers, and hauler_orgs.

    Deliberately scoped to short_name in  -- only
    the two haulers with rule packs in line_resolution.py's HAULER_RULE_PACKS.
    A bill from any other hauler wouldn't resolve to a rule pack downstream
    anyway, so there's no reason to even attempt location/hauler matching
    against haulers this pipeline can't classify lines for."""
    rows = db.fetch_all(
        # proprietary
        "redacted",
        pool_name="coyote",
    )
    for row in rows:
        # Coyote returns location_id/hauler_account_id as real integers, but
        # everywhere downstream (models, staged_bills.resolved_location_id
        # which is a text column, dict-key comparisons in line_resolution.py)
        # treats these as strings. Normalize to str here, once, at the source
        # -- confirmed necessary by a real ValidationError on a real bill,
        # not a hypothetical type mismatch.
        row["location_id"] = str(row["location_id"])
        row["hauler_account_id"] = str(row["hauler_account_id"])
        row["normalized_address"] = normalize_full_address(row["address"], row["city"], row["state"], row["zip"])
        row["normalized_hauler_name"] = normalize_text(row["hauler_org"])
    return rows


class LocationResolver:
    def get_bills_needing_resolution(self) -> list[dict]:
        return db.fetch_all(
            "select bill_id, address, city, state, zip, hauler_name from staged_bills "
            "where resolved_location_id is null"
        )

    def get_known_corrections(self) -> dict[str, str]:
        rows = db.fetch_all(
            "select address, city, state, zip, resolved_location_id from staged_bills "
            "where review_status = 'review_complete' "
            "and agent_proposed_location_id is distinct from resolved_location_id"
        )
        return {
            normalize_full_address(r["address"], r.get("city"), r.get("state"), r.get("zip")): r["resolved_location_id"]
            for r in rows
        }

    def resolve_batch(self, reference_data: list[dict]) -> list[LocationResolution]:
        bills = self.get_bills_needing_resolution()
        if not bills:
            return []

        known_corrections = self.get_known_corrections()

        location_candidates = {}
        for row in reference_data:
            location_candidates.setdefault(row["location_id"], row)
        location_candidates = list(location_candidates.values())

        candidates_by_key = defaultdict(list)
        for c in location_candidates:
            candidates_by_key[(c["state"], normalize_zip(c["zip"]))].append(c)

        results = []
        for bill in bills:
            addr_norm = normalize_full_address(bill["address"], bill.get("city"), bill["state"], bill["zip"])

            if addr_norm in known_corrections:
                results.append(LocationResolution(
                    bill_id=bill["bill_id"], hauler_name=bill.get("hauler_name"),
                    resolved_location_id=known_corrections[addr_norm],
                    location_match_status="learned_match", top_candidates=[],
                ))
                continue

            key_candidates = candidates_by_key.get((bill["state"], normalize_zip(bill["zip"])), [])
            scored = sorted(
                ({**c, "score": fuzz.token_sort_ratio(addr_norm, c["normalized_address"])} for c in key_candidates),
                key=lambda c: c["score"], reverse=True,
            )
            top = scored[:5]

            if not top:
                results.append(LocationResolution(
                    bill_id=bill["bill_id"], hauler_name=bill.get("hauler_name"),
                    resolved_location_id=None, location_match_status="no_match", top_candidates=[],
                ))
                continue

            best, second = top[0], (top[1]["score"] if len(top) > 1 else 0)
            status = _bucket_confidence(addr_norm == best["normalized_address"], best["score"], second)
            resolved_id = best["location_id"] if status != "no_match" else None

            results.append(LocationResolution(
                bill_id=bill["bill_id"], hauler_name=bill.get("hauler_name"),
                resolved_location_id=resolved_id, location_match_status=status, top_candidates=top,
            ))

        return results

    def write_resolutions(self, resolutions: list[LocationResolution], sink: OutputSink) -> None:
        sink.update(
            "staged_bills",
            [{"bill_id": r.bill_id, "resolved_location_id": r.resolved_location_id,
              "agent_proposed_location_id": r.resolved_location_id,
              "location_match_status": r.location_match_status} for r in resolutions],
            key_column="bill_id",
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=20))
    def _ask_claude_to_pick_branch(self, bill_number: str, hauler_name: str, branches: list[dict]) -> ToTDecision:
        tool = {
            "name": "record_location_decision",
            "description": "Report which candidate location is correct, or that the evidence is insufficient.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "chosen_location_id": {"type": ["string", "null"]},
                    "reasoning": {"type": "string"},
                },
                "required": ["reasoning"],
            },
        }
        response = _client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1024,
            # proprietary
            system=(
                "redacted"
            ),
            tools=[tool],
            tool_choice={"type": "tool", "name": "record_location_decision"},
            messages=[{"role": "user", "content": json.dumps({
                "bill_number": bill_number, "hauler_name": hauler_name, "candidate_locations": branches,
            })}],
        )
        tool_use = next(b for b in response.content if b.type == "tool_use")
        return ToTDecision(chosen_id=tool_use.input.get("chosen_location_id"), reasoning=tool_use.input["reasoning"])

    def resolve_ambiguous_with_tot(self, resolutions: list[LocationResolution], reference_data: list[dict]) -> list[LocationResolution]:
        ambiguous = [r for r in resolutions if r.location_match_status == "possible_match"]
        if not ambiguous:
            return []

        bill_ids = [r.bill_id for r in ambiguous]
        context_rows = {
            row["bill_id"]: row
            for row in db.fetch_all(
                "select bill_id, bill_number, applied_to from staged_bills where bill_id = any(%s::uuid[])", (bill_ids,)
            )
        }

        haulers_by_location = defaultdict(list)
        for h in reference_data:
            haulers_by_location[h["location_id"]].append(h)

        candidate_location_ids = list({c["location_id"] for r in ambiguous for c in r.top_candidates[:settings.tot_beam_width]})
        precedent = {
            (row["bill_number"], row["resolved_location_id"]): row["occurrence_count"]
            for row in db.fetch_all(
                "select bill_number, resolved_location_id, count(*) as occurrence_count from staged_bills "
                "where resolved_location_id = any(%s) and review_status = 'review_complete' "
                "group by bill_number, resolved_location_id",
                (candidate_location_ids,),
            )
        }

        resolved = []
        for r in ambiguous:
            context = context_rows.get(r.bill_id)
            if not context:
                continue

            branches = []
            for c in r.top_candidates[:settings.tot_beam_width]:
                bill_hauler_norm = normalize_text(r.hauler_name)
                haulers_here = haulers_by_location.get(c["location_id"], [])
                hauler_match = any(
                    bill_hauler_norm == normalize_text(h.get("hauler_org_abbreviation")) or bill_hauler_norm == h["normalized_hauler_name"]
                    for h in haulers_here
                )
                branches.append({
                    "location_id": c["location_id"], "address": c["normalized_address"], "fuzzy_score": c["score"],
                    "has_matching_hauler_account": hauler_match,
                    "prior_bills_resolved_here": precedent.get((context["bill_number"], c["location_id"]), 0),
                })

            decision = self._ask_claude_to_pick_branch(context["bill_number"], r.hauler_name, branches)
            resolved.append(LocationResolution(
                bill_id=r.bill_id, resolved_location_id=decision.chosen_id,
                location_match_status="likely_match" if decision.chosen_id else "possible_match",
                top_candidates=[], tot_reasoning=decision.reasoning,
            ))

        return resolved

    def write_tot_resolutions(self, resolutions: list[LocationResolution], sink: OutputSink) -> None:
        sink.update(
            "staged_bills",
            [{"bill_id": r.bill_id, "resolved_location_id": r.resolved_location_id,
              "agent_proposed_location_id": r.resolved_location_id,
              "location_match_status": r.location_match_status,
              "tot_reasoning": r.tot_reasoning} for r in resolutions],
            key_column="bill_id",
        )


class HaulerResolver:
    def get_bills_needing_resolution(self) -> list[dict]:
        return db.fetch_all(
            "select bill_id, hauler_name, hauler_account_number, resolved_location_id, bill_category, gl_code "
            "from staged_bills where resolved_location_id is not null and resolved_hauler_account_id is null"
        )

    def get_known_corrections(self) -> dict[tuple[str, str], str]:
        rows = db.fetch_all(
            "select hauler_name, resolved_location_id, resolved_hauler_account_id from staged_bills "
            "where review_status = 'review_complete' "
            "and agent_proposed_hauler_account_id is distinct from resolved_hauler_account_id"
        )
        return {(normalize_text(r["hauler_name"]), r["resolved_location_id"]): r["resolved_hauler_account_id"] for r in rows}

    def _hauler_match_score(self, bill_hauler_norm: str, candidate: dict) -> float:
        """token_set_ratio, not token_sort_ratio -- hauler names on bills carry
        boilerplate suffixes ('Disposal & Recycling Services', 'Inc.') that
        token_sort_ratio penalizes even when the canonical name is fully
        contained; token_set_ratio scores that kind of superset match at 100."""
        abbrev_score = fuzz.token_set_ratio(bill_hauler_norm, normalize_text(candidate.get("hauler_org_abbreviation")))
        full_name_score = fuzz.token_set_ratio(bill_hauler_norm, candidate["normalized_hauler_name"])
        return max(abbrev_score, full_name_score)

    def resolve_batch(self, reference_data: list[dict]) -> list[HaulerResolution]:
        bills = self.get_bills_needing_resolution()
        if not bills:
            return []

        known_corrections = self.get_known_corrections()
        candidates_by_location = defaultdict(list)
        for c in reference_data:
            candidates_by_location[c["location_id"]].append(c)

        results = []
        for bill in bills:
            bill_hauler_norm = normalize_text(bill.get("hauler_name"))
            correction_key = (bill_hauler_norm, bill["resolved_location_id"])

            if correction_key in known_corrections:
                results.append(HaulerResolution(
                    bill_id=bill["bill_id"], resolved_hauler_account_id=known_corrections[correction_key],
                    hauler_match_status="learned_match", top_candidates=[],
                ))
                continue

            location_candidates = candidates_by_location.get(bill["resolved_location_id"], [])
            scored = sorted(
                ({**c, "score": self._hauler_match_score(bill_hauler_norm, c)} for c in location_candidates),
                key=lambda c: c["score"], reverse=True,
            )
            top = scored[:5]

            if not top:
                results.append(HaulerResolution(
                    bill_id=bill["bill_id"], resolved_hauler_account_id=None,
                    hauler_match_status="no_match", top_candidates=[],
                ))
                continue

            best, second = top[0], (top[1]["score"] if len(top) > 1 else 0)
            is_exact = bill_hauler_norm in (normalize_text(best.get("hauler_org_abbreviation")), best["normalized_hauler_name"])
            status = _bucket_confidence(is_exact, best["score"], second)
            resolved_id = best["hauler_account_id"] if status != "no_match" else None

            results.append(HaulerResolution(
                bill_id=bill["bill_id"], resolved_hauler_account_id=resolved_id,
                hauler_match_status=status, top_candidates=top,
            ))

        return results

    def write_resolutions(self, resolutions: list[HaulerResolution], sink: OutputSink) -> None:
        sink.update(
            "staged_bills",
            [{"bill_id": r.bill_id, "resolved_hauler_account_id": r.resolved_hauler_account_id,
              "agent_proposed_hauler_account_id": r.resolved_hauler_account_id,
              "hauler_match_status": r.hauler_match_status} for r in resolutions],
            key_column="bill_id",
        )

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=20))
    def _ask_claude_to_pick_branch(self, bill_number: str, bill_category: str, branches: list[dict]) -> ToTDecision:
        tool = {
            "name": "record_hauler_decision",
            "description": "Report which candidate hauler account is correct, or that the evidence is insufficient.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "chosen_hauler_account_id": {"type": ["string", "null"]},
                    "reasoning": {"type": "string"},
                },
                "required": ["reasoning"],
            },
        }
        response = _client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1024,
            system=(
                "You resolve ambiguous hauler account matches where multiple accounts at the same "
                "location share the same or a very similar name. An exact account number match should "
                "almost always settle it. If nothing distinguishes the candidates, say so rather than guessing."
            ),
            tools=[tool],
            tool_choice={"type": "tool", "name": "record_hauler_decision"},
            messages=[{"role": "user", "content": json.dumps({
                "bill_number": bill_number, "bill_category": bill_category, "candidate_hauler_accounts": branches,
            })}],
        )
        tool_use = next(b for b in response.content if b.type == "tool_use")
        return ToTDecision(chosen_id=tool_use.input.get("chosen_hauler_account_id"), reasoning=tool_use.input["reasoning"])

    def get_context_for_bills(self, bill_ids: list[str]) -> list[dict]:
        if not bill_ids:
            return []
        return db.fetch_all(
            "select bill_id, bill_number, bill_category, hauler_account_number from staged_bills where bill_id = any(%s::uuid[])",
            (bill_ids,),
        )

    def resolve_ambiguous_with_tot(self, resolutions: list[HaulerResolution], bills_context: list[dict], reference_data: list[dict]) -> list[HaulerResolution]:
        ambiguous = [r for r in resolutions if r.hauler_match_status == "possible_match"]
        if not ambiguous:
            return []

        context_by_bill = {b["bill_id"]: b for b in bills_context}
        account_numbers = {h["hauler_account_id"]: h.get("hauler_account_number") for h in reference_data}

        resolved = []
        for r in ambiguous:
            context = context_by_bill.get(r.bill_id)
            if not context:
                continue

            branches = []
            for c in r.top_candidates[:settings.tot_beam_width]:
                candidate_number = account_numbers.get(c["hauler_account_id"], "")
                branches.append({
                    "hauler_account_id": c["hauler_account_id"],
                    "name_score": c["score"],
                    "account_number_match_score": fuzz.ratio(context.get("hauler_account_number") or "", candidate_number or ""),
                    "exact_account_number_match": bool(context.get("hauler_account_number")) and context.get("hauler_account_number") == candidate_number,
                })

            decision = self._ask_claude_to_pick_branch(context.get("bill_number"), context.get("bill_category"), branches)
            resolved.append(HaulerResolution(
                bill_id=r.bill_id, resolved_hauler_account_id=decision.chosen_id,
                hauler_match_status="likely_match" if decision.chosen_id else "possible_match",
                top_candidates=[], tot_reasoning=decision.reasoning,
            ))

        return resolved

    def write_tot_resolutions(self, resolutions: list[HaulerResolution], sink: OutputSink) -> None:
        # NOTE: sink.update overwrites tot_reasoning; the real Retool block appends
        # ('| '-joined) in case a bill went through both location and hauler ToT.
        # Preserving that requires a read-then-write, which the generic sink
        # interface doesn't support -- do the append explicitly here instead.
        for r in resolutions:
            existing = db.fetch_all("select tot_reasoning from staged_bills where bill_id = %s", (r.bill_id,))
            prior = existing[0]["tot_reasoning"] if existing else None
            combined = f"{prior} | {r.tot_reasoning}" if prior else r.tot_reasoning
            sink.update(
                "staged_bills",
                [{"bill_id": r.bill_id, "resolved_hauler_account_id": r.resolved_hauler_account_id,
                  "agent_proposed_hauler_account_id": r.resolved_hauler_account_id,
                  "hauler_match_status": r.hauler_match_status, "tot_reasoning": combined}],
                key_column="bill_id",
            )