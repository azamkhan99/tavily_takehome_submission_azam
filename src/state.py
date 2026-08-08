"""Shared models and LangGraph state for KYC public-domain investigation."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field


UboStatus = Literal["absent", "inferred"]
Recommendation = Literal["Proceed", "Review", "Escalate"]
EvidenceCategory = Literal[
    "company",
    "ubo",
    "adverse_media",
    "litigation",
    "reputation",
    "ownership",
]
Provenance = Literal[
    "document_extracted",
    "public_corroboration",
    "inferred",
]


DOC_TYPE_TO_LABEL: dict[str, str] = {
    "passport": "Proof of Identity",
    "kyc_form": "KYC Form",
    "trade_licence": "Trade Licence",
    "commercial_license": "Trade Licence",
    "certificate_of_incorporation": "Certificate of Incorporation",
    "register_of_shareholders": "Shareholding Structure",
    "group_structure_chart": "Shareholding Structure",
    "shareholding_structure": "Shareholding Structure",
}


class DocumentFile(BaseModel):
    doc_type: str
    label: str
    path: str
    content: str


class PersonEntity(BaseModel):
    name: str
    role: str = "signatory"
    nationality: str | None = None
    id_number: str | None = None
    aliases: list[str] = Field(default_factory=list)
    ownership_pct: float | None = None
    ownership_source: str | None = None
    ubo_basis: str | None = None


class CompanyEntity(BaseModel):
    legal_name: str
    aliases: list[str] = Field(default_factory=list)
    registration_number: str | None = None
    jurisdiction: str | None = None
    address: str | None = None
    industry: str | None = None
    trade_licence_number: str | None = None


class EntityBundle(BaseModel):
    company: CompanyEntity
    persons: list[PersonEntity] = Field(default_factory=list)
    corporate_shareholders: list[str] = Field(default_factory=list)
    ubo_status: UboStatus = "absent"
    inconsistency_flags: list[str] = Field(default_factory=list)
    ownership_gaps: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    entity: str
    category: EvidenceCategory
    claim: str
    confidence: float = 0.5
    source: str
    published_date: str | None = None
    url: str
    provenance: Provenance | None = None
    adverse: bool | None = None
    objective_id: str | None = None


class SearchQuery(BaseModel):
    objective_id: str
    entity: str
    category: EvidenceCategory
    query: str
    topic: Literal["general", "news", "finance"] = "general"
    rationale: str = ""


class InvestigationObjective(BaseModel):
    id: str
    label: str
    category: EvidenceCategory
    entity: str
    required: bool = True
    covered: bool = False
    notes: str = ""


class CoverageReport(BaseModel):
    percent: float = 0.0
    items: list[dict[str, Any]] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    sufficient: bool = False


class TavilyTrace(BaseModel):
    timestamp: str
    objective_id: str
    query: str
    params: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float | None = None
    answer: str | None = None
    result_count: int = 0
    sources: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class Finding(BaseModel):
    title: str
    detail: str
    confidence: str
    evidence_count: int
    sources: list[dict[str, str]] = Field(default_factory=list)
    category: str = ""


class InvestigationReport(BaseModel):
    executive_summary: str = ""
    company_overview: str = ""
    management_investigation: str = ""
    ultimate_beneficial_ownership: str = ""
    adverse_media: str = ""
    litigation: str = ""
    reputation: str = ""
    risk_findings: list[Finding] = Field(default_factory=list)
    recommendation: Recommendation = "Review"
    recommendation_rationale: str = ""
    coverage: CoverageReport = Field(default_factory=CoverageReport)
    missing_documents: list[str] = Field(default_factory=list)
    document_requests: list[str] = Field(default_factory=list)
    next_manual_actions: list[str] = Field(default_factory=list)
    ubo_status: UboStatus = "absent"


def _merge_lists(left: list[Any] | None, right: list[Any] | None) -> list[Any]:
    return list(left or []) + list(right or [])


class InvestigationState(TypedDict, total=False):
    pack_id: str
    docs_dir: str
    documents: list[dict[str, Any]]
    document_extractions: list[dict[str, Any]]
    present_doc_types: list[str]
    missing_documents: list[str]
    document_requests: list[str]
    entities: dict[str, Any]
    ubo_status: UboStatus
    inconsistency_flags: list[str]
    objectives: list[dict[str, Any]]
    pending_queries: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    tavily_traces: Annotated[list[dict[str, Any]], _merge_lists]
    coverage: dict[str, Any]
    round: int
    max_rounds: int
    recommendation: Recommendation
    recommendation_rationale: str
    cra: dict[str, Any]
    synthesized_edd: dict[str, Any]
    report: dict[str, Any]
    error: str | None
    investigation_plan_context: str
