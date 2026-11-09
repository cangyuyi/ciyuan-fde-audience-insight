from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from audience_agent.privacy import (
    contains_direct_identifier,
    metadata_contains_direct_identifier,
    metadata_has_sensitive_key,
    safe_json_size,
)

Domain = Literal["live_roi", "audience_profile", "experience", "fulfillment", "cross_domain"]
Decision = Literal["accepted", "edited", "rejected", "needs_validation"]
ClaimKind = Literal["observation", "inference", "recommendation", "hypothesis"]


class EvidenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=3, max_length=160)
    workspace_id: str = Field(min_length=1, max_length=100)
    dataset_version: str = Field(min_length=1, max_length=100)
    domain: Domain
    record_type: Literal["fact", "quote", "caveat", "recommendation", "opinion", "hypothesis"]
    content: str = Field(min_length=1, max_length=8_000)
    source_material: str = Field(min_length=1, max_length=80)
    source_file: str = Field(min_length=1, max_length=12_000)
    source_sheet: str = Field(min_length=1, max_length=120)
    source_field: str = Field(min_length=1, max_length=300)
    source_row: str | None = Field(default=None, max_length=200)
    confidence: Literal["high", "medium", "low"]
    metadata: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_direct_identifiers(self) -> EvidenceRecord:
        source_text = " ".join(
            (
                self.id,
                self.workspace_id,
                self.dataset_version,
                self.source_material,
                self.content,
                self.source_file,
                self.source_sheet,
                self.source_field,
                self.source_row or "",
            )
        )
        if contains_direct_identifier(source_text):
            raise ValueError(
                "direct personal identifiers must be redacted before evidence ingestion"
            )
        if metadata_has_sensitive_key(self.metadata):
            raise ValueError("metadata contains a prohibited direct-identifier field")
        if metadata_contains_direct_identifier(self.metadata):
            raise ValueError("metadata contains a direct personal identifier")
        if not safe_json_size(self.metadata):
            raise ValueError("metadata exceeds its maximum serialized size")
        if self.source_material == "M5" and self.record_type not in {"opinion", "hypothesis"}:
            raise ValueError("M5 records must remain opinions or hypotheses, never verified facts")
        return self


class GroundedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=700)
    kind: ClaimKind
    evidence_ids: list[str] = Field(min_length=1, max_length=4)
    confidence: Literal["high", "medium", "low"]


class GroundedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claims: list[GroundedClaim] = Field(default_factory=list, max_length=8)
    limitations: list[str] = Field(default_factory=list, max_length=6)
    next_steps: list[str] = Field(default_factory=list, max_length=5)


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=3, max_length=1_200)
    thread_id: str | None = Field(default=None, min_length=8, max_length=80)

    @field_validator("question")
    @classmethod
    def strip_question(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("question must contain at least 3 non-space characters")
        if contains_direct_identifier(value):
            raise ValueError("remove direct personal identifiers before submitting a question")
        return value

    @field_validator("thread_id")
    @classmethod
    def reject_identifiers_in_thread(cls, value: str | None) -> str | None:
        if value and contains_direct_identifier(value):
            raise ValueError("redact direct personal identifiers from thread_id")
        return value


class EvidenceCard(BaseModel):
    id: str
    source_material: str
    source_file: str
    source_sheet: str
    source_field: str
    source_row: str | None
    content: str
    confidence: str
    dataset_version: str


class AnalyzeResponse(BaseModel):
    run_id: str
    thread_id: str
    status: Literal["answered", "abstained"]
    dataset_version: str | None = None
    intent: str | None
    claims: list[GroundedClaim]
    evidence: list[EvidenceCard]
    limitations: list[str]
    next_steps: list[str]
    token_usage: dict[str, int | str] = Field(default_factory=dict)


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: UUID
    thread_id: str = Field(min_length=8, max_length=160)
    # Required nullable field: clients must explicitly bind the returned dataset snapshot.
    dataset_version: str | None = Field(..., max_length=100)
    decision: Decision
    notes: str = Field(default="", max_length=2_000)

    @field_validator("notes", "thread_id")
    @classmethod
    def reject_identifiers_in_notes(cls, value: str) -> str:
        if contains_direct_identifier(value):
            raise ValueError("redact direct personal identifiers from feedback fields")
        return value

    @field_validator("dataset_version")
    @classmethod
    def reject_identifiers_in_dataset(cls, value: str | None) -> str | None:
        if value and contains_direct_identifier(value):
            raise ValueError("redact direct personal identifiers from dataset_version")
        return value
