"""Pydantic mirrors of the two JSON contracts.

in : schema_initial_example.json   (from the upstream judge LLM)
out: grounded_schema_example.json  (what this module returns)

The output model is handed to Mistral as a strict response_format, so the
shape is enforced by the SDK rather than requested in a prompt.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- input


class Lookup(BaseModel):
    intent: str
    question: str
    presentation_terms: list[str] = Field(default_factory=list)
    prefer_document: Optional[str] = None
    vitals_read: dict = Field(default_factory=dict)
    answer_shape: Optional[str] = None
    priority: int = 99


class InitialAssessment(BaseModel):
    """Deliberately permissive - upstream is another LLM's output."""
    model_config = {"extra": "allow"}

    patient_id: Optional[str] = None
    concern: Optional[str] = None
    what_keeps_it_open: Optional[str] = None
    vitals_read: dict = Field(default_factory=dict)
    lookups: list[Lookup] = Field(default_factory=list)
    implied_esi: Optional[int] = None
    implied_esi_reasoning: Optional[str] = None


# ---------------------------------------------------------------- output


class Evidence(BaseModel):
    document: str = Field(description="Exact document name from the supplied evidence block")
    page: int = Field(description="Page number from the supplied evidence block. Never invent one.")
    criterion: str = Field(description="Verbatim sentence from the retrieved page text")


class Concern(BaseModel):
    concern: str = Field(description="Plain-language, no jargon - a nurse reads this")
    clinical_shorthand: str = Field(description="Short clinical tag, e.g. ?sepsis")
    implied_esi: int
    final_esi: int
    time_to_treatment_minutes: Optional[int] = None
    evidence: list[Evidence]
    nurse_summary: str = Field(description="One or two lines, actionable at the bedside")


class Grounded(BaseModel):
    patient_id: str
    concerns: list[Concern]
    provisional_esi: int
    grounded_esi: int
