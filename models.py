"""Domain models for the vendor bill mapping agent."""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class BillLineExtraction(BaseModel):
    lineAmount: Optional[float] = None
    lineDescription: Optional[str] = None
    serviceType: Optional[str] = None
    binSize: Optional[str] = None
    quantity: Optional[float] = None
    rate: Optional[float] = None
    workOrderNumber: Optional[str] = None
    ticketNumber: Optional[str] = None
    lineOccurrenceDate: Optional[str] = None
    lineOccurrenceStartDate: Optional[str] = None
    lineOccurrenceEndDate: Optional[str] = None


class BillHeaderExtraction(BaseModel):
    billNumber: Optional[str] = None
    haulerAccountId: Optional[str] = None
    haulerAccountNumber: Optional[str] = None
    locationId: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    hauler_name: Optional[str] = None
    billDate: Optional[str] = None
    billDueDate: Optional[str] = None


class BillExtraction(BaseModel):
    header: BillHeaderExtraction
    lines: list[BillLineExtraction] = Field(default_factory=list)


class LocationResolution(BaseModel):
    bill_id: str
    hauler_name: Optional[str] = None
    resolved_location_id: Optional[str] = None
    location_match_status: str  # perfect_match | likely_match | possible_match | no_match | learned_match
    top_candidates: list[dict] = Field(default_factory=list)
    tot_reasoning: Optional[str] = None


class HaulerResolution(BaseModel):
    bill_id: str
    resolved_hauler_account_id: Optional[str] = None
    hauler_match_status: str
    top_candidates: list[dict] = Field(default_factory=list)
    tot_reasoning: Optional[str] = None


class ToTDecision(BaseModel):
    chosen_id: Optional[str] = None
    reasoning: str