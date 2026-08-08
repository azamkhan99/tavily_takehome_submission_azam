"""EDD synthesis, PDF rendering, and report graph nodes."""

from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from kyc_agent.cra_matrix import cra_table_rows
from kyc_agent.llm import structured_invoke
from kyc_agent.runtime import emit
from kyc_agent.state import (
    CoverageReport,
    Finding,
    InvestigationReport,
    InvestigationState,
)

_CITE_GROUP_RE = re.compile(r"\[([\d,\s]+)\]")


def build_cite_url_map(appendix: list[dict[str, Any]]) -> dict[str, str]:
    """Map citation id strings to source URLs."""
    cite_map: dict[str, str] = {}
    for entry in appendix:
        cite_id = entry.get("id")
        url = (entry.get("url") or "").strip()
        if cite_id is not None and url:
            cite_map[str(cite_id)] = url
    return cite_map


def _safe_href(url: str) -> str:
    return (
        url.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def linkify_citation_markers(text: str, cite_map: dict[str, str]) -> str:
    """Turn [1] or [1, 3, 4] markers into ReportLab anchor tags when URLs exist."""

    def replace_group(match: re.Match[str]) -> str:
        numbers = re.findall(r"\d+", match.group(1))
        if not numbers:
            return match.group(0)
        parts: list[str] = []
        for number in numbers:
            url = cite_map.get(number, "")
            if url:
                parts.append(
                    f'<a href="{_safe_href(url)}" color="#1d4ed8">[{number}]</a>'
                )
            else:
                parts.append(f"[{number}]")
        return "".join(parts)

    return _CITE_GROUP_RE.sub(replace_group, text)


def citation_appendix_line(cite: dict[str, Any]) -> str:
    """Format one appendix bibliography entry with a clickable source link."""
    cite_id = cite.get("id")
    title = cite.get("title") or "Source"
    url = (cite.get("url") or "").strip()
    published = cite.get("published_date") or "n/d"
    safe_title = (
        str(title)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    if url:
        linked = f'<a href="{_safe_href(url)}" color="#1d4ed8">{safe_title}</a>'
        return f"{cite_id}. {linked} ({published})"
    return f"{cite_id}. {safe_title} ({published})"


EDD_SYNTHESIS_PROMPT = """
Your job is to produce structured JSON consumed by an EDD report renderer.

The workflow simulates the open-source investigation of a prospective corporate tenant.
The underlying company and individuals may be real publicly documented entities, but the
leasing scenario itself is fictional and for demonstration purposes only.

Transform the supplied KYC-document extraction results and Tavily research findings into a
concise, source-grounded Enhanced Due Diligence report.

Populate every field from the supplied inputs. Do not invent facts, relationships, risk
findings, ownership percentages, dates, or sources.

# CORE PRINCIPLE

This is an investigation report, not a compliance clearance decision.

Distinguish clearly between:
- facts established by KYC documents;
- facts established by authoritative public sources;
- allegations or adverse media;
- unresolved or ambiguous information;
- conclusions supported by the available evidence.

Do not state that an entity is "cleared" merely because no adverse information was found.
Use language such as "No relevant adverse findings were identified in the sources searched."
rather than "The entity is clear."

# FIELD RULES

## General sourcing
Every material factual statement should be traceable to the supplied inputs.
Prefer primary sources where available (government/regulatory, official filings, courts,
reputable news). Do not treat search snippets or aggregators as strong evidence when a
primary source is available.

## Company details
Populate company_detail_rows from KYC documents first; supplement from Tavily only when
documents lack the field. Do not overwrite a documented KYC fact with an unsupported web
result. If KYC and public sources conflict, preserve the documented value and note the
discrepancy in investigation_findings.

## Ownership
Build ownership from the strongest available evidence. Do not infer UBO status solely from
CEO/director/founder role. Only identify an individual as UBO when evidence supports ownership
or control. If the chain cannot be established, set identified_ubo to "Not identified" and
explain the limitation.

## Entity resolution
Treat common-name matches as unresolved until corroborated. Do not merge people solely
because names match. Use "Potential match — identity could not be conclusively resolved"
when appropriate.

## Adverse media
Only report adverse media relevant to the investigated entity or individual. Distinguish
allegation, investigation, regulatory action, charge, conviction, litigation, and reporting
about another person with the same name. Do not turn allegations into facts.
If none: state that no relevant adverse media was identified in the sources searched.

## Narrative sections (inline citations)
Integrate ownership, adverse media, litigation, regulatory, and reputational material
directly into the narrative fields:
company_overview, ownership_structure, adverse_media, litigation, reputation_management.

Use inline citation markers [n] matching the cite numbers in tavily_research / tavily_sources.
Every material factual claim in those narratives should include at least one [n] marker when a
source exists. Example: "Incorporated in England and Wales [3]."

Do NOT write a separate risk-findings chapter. investigation_findings is optional structured
metadata for your own reasoning; any material point must also appear in the appropriate narrative
with [n] citations.

## Investigation findings (internal metadata only)
If populated, use exact keys: subject, category, severity, finding, evidence, confidence,
supporting_sources. These are not rendered as a standalone report section.

## Company detail rows
Each row must use exact keys: field, value (not label/name). Include [n] markers in values when
supported by Tavily sources.

## Overview key findings
Provide exactly four concise strings:
1. Company activity, jurisdiction, structure, key business characteristics.
2. Shareholders, ownership chain, identified UBO, ownership limitations.
3. Material Tavily findings (adverse media, regulatory, litigation, sanctions). If none, use
   the standard no-findings sentence from the adverse media rule.
4. Unresolved issues, entity-resolution limits, coverage gaps, whether further investigation
   is warranted. Do not issue definitive compliance clearance.

## Source coverage
Record entities investigated, research categories searched, sources that materially supported
findings, and areas that could not be conclusively researched. Do not claim comprehensive
coverage unless inputs establish it.

## Overall assessment
Use one of: "No material findings identified", "Findings identified",
"Further investigation required".
"No material findings identified" does not mean the company has been cleared.

## Privacy
Do not include passport numbers, national ID numbers, personal addresses, personal phone or
email, bank accounts, dates of birth, signatures, or other sensitive PII.

## Rendering
Produce clean analyst-facing prose. Do not dump raw JSON, search queries, internal workflow
details, or duplicate the same finding across sections unnecessarily. Reference sources via
citation numbers [n] where supplied in tavily_research items.

# INPUTS (JSON)

{inputs_json}
"""


class EddCompanyDetailRow(BaseModel):
    field: str
    value: str

    @model_validator(mode="before")
    @classmethod
    def normalize_row(cls, data: Any) -> Any:
        return normalize_company_detail_row(data)


class EddInvestigationFinding(BaseModel):
    subject: str
    category: str
    severity: Literal["Low", "Medium", "High"] = "Medium"
    finding: str
    evidence: str = ""
    confidence: Literal["Low", "Medium", "High"] = "Medium"
    supporting_sources: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_finding(cls, data: Any) -> Any:
        return normalize_investigation_finding(data)


class EddSourceCoverage(BaseModel):
    entities_investigated: list[str] = Field(default_factory=list)
    research_categories_searched: list[str] = Field(default_factory=list)
    sources_supporting_findings: list[str] = Field(default_factory=list)
    unresolved_areas: list[str] = Field(default_factory=list)


class SynthesizedEddReport(BaseModel):
    purpose: str
    executive_summary: str
    company_detail_rows: list[EddCompanyDetailRow] = Field(default_factory=list)
    company_overview: str
    ownership_structure: str
    identified_ubo: str
    adverse_media: str
    litigation: str
    reputation_management: str
    investigation_findings: list[EddInvestigationFinding] = Field(default_factory=list)
    overview_key_findings: list[str] = Field(default_factory=list)
    source_coverage: EddSourceCoverage = Field(default_factory=EddSourceCoverage)
    overall_assessment: Literal[
        "No material findings identified",
        "Findings identified",
        "Further investigation required",
    ]

    @field_validator("overview_key_findings")
    @classmethod
    def ensure_four_key_findings(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        while len(cleaned) < 4:
            cleaned.append("Not available from supplied inputs.")
        return cleaned[:4]


def _first_str(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def coerce_confidence_label(value: Any) -> Literal["Low", "Medium", "High"]:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"low", "l"}:
            return "Low"
        if normalized in {"medium", "med", "m"}:
            return "Medium"
        if normalized in {"high", "h"}:
            return "High"
        try:
            value = float(normalized)
        except ValueError:
            return "Medium"
    if isinstance(value, (int, float)):
        score = float(value)
        if score > 1.0:
            score = score / 100.0 if score <= 100.0 else score
        if score >= 0.75:
            return "High"
        if score >= 0.45:
            return "Medium"
        return "Low"
    return "Medium"


def coerce_severity_label(value: Any) -> Literal["Low", "Medium", "High"]:
    return coerce_confidence_label(value)


def _coerce_source_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        sources: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                sources.append(item.strip())
            elif isinstance(item, dict):
                label = (
                    item.get("title")
                    or item.get("name")
                    or item.get("source")
                    or item.get("url")
                )
                if label:
                    sources.append(str(label).strip())
        return sources
    return [str(value).strip()]


def normalize_company_detail_row(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"field": "Unknown", "value": str(item)}
    field = _first_str(item, "field", "label", "name", "key", "attribute", "title")
    value = _first_str(item, "value", "content", "text", "detail", "answer", "description")
    return {
        "field": field or "Unknown",
        "value": value or "Not available",
    }


def normalize_investigation_finding(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {
            "subject": "Unknown",
            "category": "general",
            "severity": "Medium",
            "finding": str(item),
            "evidence": "",
            "confidence": "Medium",
            "supporting_sources": [],
        }
    row = dict(item)
    finding = _first_str(
        row,
        "finding",
        "description",
        "summary",
        "fact",
        "detail",
        "details",
        "narrative",
        "text",
        "statement",
    )
    evidence = _first_str(row, "evidence", "evidence_summary", "supporting_evidence")
    if not finding:
        finding = evidence or "Not available from supplied inputs."
    return {
        "subject": _first_str(row, "subject", "entity", "name", "title") or "Unknown",
        "category": _first_str(row, "category", "type", "topic") or "general",
        "severity": coerce_severity_label(row.get("severity", "Medium")),
        "finding": finding,
        "evidence": evidence,
        "confidence": coerce_confidence_label(row.get("confidence", "Medium")),
        "supporting_sources": _coerce_source_list(
            row.get("supporting_sources")
            or row.get("sources")
            or row.get("citations")
            or row.get("references")
        ),
    }


def normalize_synthesized_payload(data: dict[str, Any]) -> dict[str, Any]:
    payload = dict(data)
    payload["company_detail_rows"] = [
        normalize_company_detail_row(row)
        for row in payload.get("company_detail_rows") or []
    ]
    payload["investigation_findings"] = [
        normalize_investigation_finding(row)
        for row in payload.get("investigation_findings") or []
    ]
    source_coverage = payload.get("source_coverage")
    if isinstance(source_coverage, dict):
        payload["source_coverage"] = {
            "entities_investigated": _coerce_source_list(
                source_coverage.get("entities_investigated")
            ),
            "research_categories_searched": _coerce_source_list(
                source_coverage.get("research_categories_searched")
            ),
            "sources_supporting_findings": _coerce_source_list(
                source_coverage.get("sources_supporting_findings")
            ),
            "unresolved_areas": _coerce_source_list(source_coverage.get("unresolved_areas")),
        }
    return payload


def _redact_person(person: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(person)
    redacted.pop("id_number", None)
    return redacted


def build_synthesis_inputs(
    state: dict[str, Any],
    evidence: list[dict[str, Any]],
    appendix: list[dict[str, Any]],
) -> dict[str, Any]:
    entities = state.get("entities") or {}
    company = entities.get("company") or {}
    persons = [_redact_person(p) for p in (entities.get("persons") or [])]
    coverage = state.get("coverage") or {}
    cra = state.get("cra") or {}

    tavily_research = []
    for item in evidence[:48]:
        tavily_research.append(
            {
                "entity": item.get("entity"),
                "category": item.get("category"),
                "claim": (item.get("claim") or "")[:400],
                "confidence": item.get("confidence"),
                "source": item.get("source"),
                "published_date": item.get("published_date"),
                "url": item.get("url"),
                "provenance": item.get("provenance"),
                "adverse": item.get("adverse"),
                "cite": item.get("_cite"),
            }
        )

    extractions = []
    for item in state.get("document_extractions") or []:
        if isinstance(item, dict):
            row = dict(item)
            row["persons"] = [_redact_person(p) for p in (row.get("persons") or [])]
            extractions.append(row)

    return {
        "kyc_documents": extractions,
        "entity_resolution": {
            "company": company,
            "persons": persons,
            "corporate_shareholders": entities.get("corporate_shareholders") or [],
            "ownership_gaps": entities.get("ownership_gaps") or [],
            "inconsistency_flags": state.get("inconsistency_flags")
            or entities.get("inconsistency_flags")
            or [],
            "ubo_status": state.get("ubo_status") or entities.get("ubo_status"),
        },
        "ownership_structure": {
            "corporate_shareholders": entities.get("corporate_shareholders") or [],
            "ownership_gaps": entities.get("ownership_gaps") or [],
            "document_candidates": [
                p
                for p in persons
                if (p.get("role") or "").lower()
                in {"beneficial_owner_candidate", "shareholder"}
            ],
        },
        "tavily_research": tavily_research,
        "tavily_sources": appendix[:60],
        "investigation_summary": {
            "recommendation": state.get("recommendation"),
            "recommendation_rationale": state.get("recommendation_rationale"),
            "cra_risk_level": cra.get("risk_level"),
            "cra_weighted_score": cra.get("weighted_score"),
            "missing_documents": state.get("missing_documents") or [],
            "document_requests": state.get("document_requests") or [],
        },
        "coverage_assessment": {
            "percent": coverage.get("percent"),
            "gaps": coverage.get("gaps") or [],
            "suggested_actions": coverage.get("suggested_actions") or [],
            "sufficient": coverage.get("sufficient"),
            "items": coverage.get("items") or [],
        },
    }


def synthesize_edd_report(
    state: dict[str, Any],
    evidence: list[dict[str, Any]],
    appendix: list[dict[str, Any]],
) -> SynthesizedEddReport:
    inputs = build_synthesis_inputs(state, evidence, appendix)
    prompt = EDD_SYNTHESIS_PROMPT.format(
        inputs_json=json.dumps(inputs, indent=2, default=str)[:120_000]
    )
    return structured_invoke(
        prompt,
        SynthesizedEddReport,
        normalize=normalize_synthesized_payload,
    )


def format_executive_summary(
    synthesized: SynthesizedEddReport,
    *,
    cra: dict[str, Any],
) -> str:
    parts = [synthesized.executive_summary.strip()]
    if synthesized.overview_key_findings:
        parts.append("\n\nKey findings:")
        parts.extend(f"- {item}" for item in synthesized.overview_key_findings)
    parts.append(f"\n\nOverall assessment: {synthesized.overall_assessment}.")
    sc = synthesized.source_coverage
    if sc.unresolved_areas:
        parts.append("\nUnresolved areas: " + "; ".join(sc.unresolved_areas[:6]))
    parts.append(
        f"\n\nCRA risk level: {cra.get('risk_level', 'n/a')} "
        f"(weighted score {cra.get('weighted_score', 'n/a')})."
    )
    return "".join(parts).strip()


def format_ownership_section(synthesized: SynthesizedEddReport) -> str:
    ubo = (synthesized.identified_ubo or "").strip() or "Not identified"
    intro = (
        "UBO has not been formally verified unless explicitly supported by submitted "
        "documents. The following summarises ownership signals from available inputs.\n\n"
        f"Identified UBO (per available evidence): {ubo}\n\n"
    )
    body = (synthesized.ownership_structure or "").strip()
    if not body:
        body = "Ownership structure could not be established from the supplied inputs."
    return intro + body


def synthesized_findings_to_risk(findings: list[EddInvestigationFinding]) -> list[Finding]:
    mapped: list[Finding] = []
    for item in findings[:12]:
        sources = [
            {
                "title": source,
                "url": "",
                "published_date": "n/d",
                "cite": "",
            }
            for source in item.supporting_sources[:5]
        ]
        detail = item.finding.strip()
        if item.evidence:
            detail = f"{detail} Evidence: {item.evidence.strip()}"
        mapped.append(
            Finding(
                title=f"{item.category.replace('_', ' ').title()} — {item.subject} ({item.severity})",
                detail=detail[:600],
                confidence=item.confidence,
                evidence_count=len(item.supporting_sources),
                sources=sources,
                category=item.category,
            )
        )
    return mapped


def company_detail_rows_to_client(
    rows: list[EddCompanyDetailRow],
    fallback: dict[str, Any],
) -> tuple[dict[str, Any], list[list[str]]]:
    """Merge synthesized detail rows with entity bundle for renderer tables."""
    client = dict(fallback)
    table_rows = [["Field", "Value"]]
    field_map = {
        "legal name": "legal_name",
        "registration": "registration_number",
        "registration number": "registration_number",
        "trade licence": "trade_licence_number",
        "trade license": "trade_licence_number",
        "jurisdiction": "jurisdiction",
        "address": "address",
        "industry": "industry",
    }
    seen: set[str] = set()
    for row in rows:
        label = row.field.strip()
        value = row.value.strip() or "Not available"
        table_rows.append([label, value])
        key = field_map.get(label.lower())
        if key and value != "Not available":
            client[key] = value
            seen.add(key)
    if len(table_rows) == 1:
        table_rows.extend(
            [
                ["Legal name", str(client.get("legal_name") or "n/a")],
                ["Registration", str(client.get("registration_number") or "n/a")],
                ["Trade licence", str(client.get("trade_licence_number") or "n/a")],
                ["Jurisdiction", str(client.get("jurisdiction") or "n/a")],
                ["Address", str(client.get("address") or "n/a")],
                ["Industry", str(client.get("industry") or "n/a")],
            ]
        )
    return client, table_rows


def _safe_name(client_name: str | None) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (client_name or "edd"))
    return safe.strip("_")[:48] or "edd"


def _escape_xml(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def narrative_html(text: str, cite_map: dict[str, str] | None = None) -> str:
    """Escape text, linkify [n] citations, and apply light markdown."""
    cleaned = _escape_xml(text or "")
    if cite_map:
        cleaned = linkify_citation_markers(cleaned, cite_map)
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", cleaned)
    cleaned = re.sub(r"\*(.+?)\*", r"<i>\1</i>", cleaned)
    cleaned = re.sub(r"`(.+?)`", r"<font face='Courier'>\1</font>", cleaned)
    return cleaned.replace("\n", "<br/>")


def _p(text: str, style: ParagraphStyle, cite_map: dict[str, str] | None = None) -> Paragraph:
    return Paragraph(narrative_html(text, cite_map), style)


def _clean_prose_line(line: str) -> str | None:
    """Normalise one evidence/prose line for PDF rendering."""
    line = line.strip()
    if not line:
        return None
    if line.startswith("- "):
        line = line[2:].strip()
    if re.fullmatch(r"[\|\s:\-]+", line):
        return None
    if line.count("|") >= 3:
        cells = [cell.strip() for cell in line.split("|") if cell.strip() and cell.strip() != "---"]
        if not cells:
            return None
        line = " · ".join(cells[:10])
    line = re.sub(r"^#+\s*", "", line)
    line = re.sub(r"\s+", " ", line).strip()
    return line or None


def split_prose_blocks(text: str) -> tuple[str | None, list[str]]:
    """Split prose into an optional intro paragraph and bullet items."""
    intro: list[str] = []
    bullets: list[str] = []
    for raw in (text or "").splitlines():
        cleaned = _clean_prose_line(raw)
        if cleaned is None:
            continue
        if raw.strip().startswith("- ") or bullets:
            bullets.append(cleaned)
        elif not bullets:
            intro.append(cleaned)
        else:
            bullets.append(cleaned)
    intro_text = " ".join(intro).strip() or None
    return intro_text, bullets


def edd_to_pdf(
    report: dict[str, Any] | None,
    cra: dict[str, Any] | None = None,
    *,
    output_path: str | Path | None = None,
) -> str:
    """Write an EDD PDF and return the filesystem path."""
    report = report or {}
    cra = cra or report.get("cra_summary") or {}
    client = report.get("client") or {}
    sections = report.get("sections") or {}
    citations = report.get("appendix_a_citations") or []
    cite_map = build_cite_url_map(citations)
    client_name = client.get("legal_name") or "Unknown Company"
    path = Path(output_path) if output_path else Path(tempfile.gettempdir()) / (
        f"{_safe_name(client_name)}_edd_report.pdf"
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "EDDTitle",
        parent=styles["Heading1"],
        fontSize=16,
        alignment=TA_CENTER,
        spaceAfter=6,
    )
    h2 = ParagraphStyle(
        "EDDH2",
        parent=styles["Heading2"],
        fontSize=12,
        spaceBefore=10,
        spaceAfter=4,
    )
    body = ParagraphStyle(
        "EDDBody",
        parent=styles["BodyText"],
        fontSize=9,
        leading=13,
        alignment=TA_LEFT,
        spaceAfter=4,
    )
    bullet = ParagraphStyle(
        "EDDBullet",
        parent=body,
        leftIndent=14,
        bulletIndent=6,
        spaceAfter=3,
        leading=12,
    )
    subhead = ParagraphStyle(
        "EDDSubhead",
        parent=body,
        fontName="Helvetica-Bold",
        spaceBefore=6,
        spaceAfter=2,
    )
    reco_label = ParagraphStyle(
        "EDDRecoLabel",
        parent=body,
        fontName="Helvetica-Bold",
        fontSize=14,
        spaceAfter=4,
        textColor=colors.HexColor("#9a3412"),
    )
    muted = ParagraphStyle(
        "EDDMuted",
        parent=body,
        textColor=colors.HexColor("#555555"),
        fontSize=8,
    )
    table_cell = ParagraphStyle(
        "EDDTableCell",
        parent=body,
        fontSize=8,
        leading=10,
    )

    def append_prose_section(text: str, *, empty_message: str = "No material findings.") -> None:
        intro, bullets = split_prose_blocks(text)
        if not intro and not bullets:
            story.append(_p(empty_message, body, cite_map))
            return
        if intro:
            story.append(_p(intro, body, cite_map))
            story.append(Spacer(1, 2))
        for item in bullets:
            story.append(Paragraph(narrative_html(item, cite_map), bullet, bulletText="•"))

    def append_bullet_group(title: str, items: list[str]) -> None:
        if not items:
            return
        story.append(Paragraph(narrative_html(title, cite_map), subhead))
        for item in items:
            story.append(
                Paragraph(narrative_html(str(item), cite_map), bullet, bulletText="•")
            )

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=report.get("title") or "Enhanced Due Diligence Report",
    )
    story: list[Any] = []
    story.append(_p(report.get("title") or "Enhanced Due Diligence Report", title_style))
    if report.get("subtitle"):
        story.append(_p(str(report["subtitle"]), muted))
    if report.get("generated_at"):
        story.append(_p(f"Generated: {report['generated_at']}", muted))
    story.append(Spacer(1, 6))

    reco = sections.get("1_recommendation") or {}
    recommendation = str(reco.get("recommendation") or report.get("recommendation") or "Review")
    rationale = str(reco.get("rationale") or report.get("recommendation_rationale") or "").strip()
    story.append(_p("1. Recommendation", h2))
    story.append(Paragraph(narrative_html(recommendation, cite_map), reco_label))
    if rationale:
        story.append(_p(rationale, body, cite_map))
    else:
        story.append(_p("No additional rationale provided.", muted))

    story.append(_p("2. Purpose", h2))
    story.append(
        _p(
            sections.get("2_purpose")
            or sections.get("1_purpose")
            or "Public-domain Enhanced Due Diligence for analyst review.",
            body,
            cite_map,
        )
    )

    story.append(_p("3. Client particulars", h2))
    detail_rows = report.get("client_detail_rows")
    if detail_rows:
        client_rows = detail_rows
    else:
        client_rows = [
            ["Field", "Value"],
            ["Legal name", str(client.get("legal_name") or "n/a")],
            ["Registration", str(client.get("registration_number") or "n/a")],
            ["Trade licence", str(client.get("trade_licence_number") or "n/a")],
            ["Jurisdiction", str(client.get("jurisdiction") or "n/a")],
            ["Address", str(client.get("address") or "n/a")],
            ["Industry", str(client.get("industry") or "n/a")],
        ]
    if client_rows and client_rows[0] != ["Field", "Value"]:
        client_rows = [["Field", "Value"], *client_rows]
    client_table = Table(
        [
            [
                Paragraph(narrative_html(row[0], cite_map), table_cell),
                Paragraph(narrative_html(row[1], cite_map), table_cell),
            ]
            for row in client_rows
        ],
        colWidths=[45 * mm, 125 * mm],
    )
    client_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111111")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    story.append(client_table)

    story.append(_p("4. Executive summary", h2))
    append_prose_section(
        sections.get("3_executive_summary")
        or sections.get("2_executive_summary")
        or report.get("executive_summary")
        or "",
        empty_message="No executive summary available.",
    )

    story.append(_p("5. Client Risk Assessment summary", h2))
    story.append(
        _p(
            f"Risk level: {cra.get('risk_level') or 'n/a'} · "
            f"Weighted score: {cra.get('weighted_score') if cra.get('weighted_score') is not None else 'n/a'} · "
            f"Matrix: {cra.get('matrix_version') or 'n/a'}",
            body,
            cite_map,
        )
    )
    rows = cra.get("rows") or []
    if rows:
        cra_table_data: list[list[Any]] = [
            [
                Paragraph(narrative_html("Group", cite_map), table_cell),
                Paragraph(narrative_html("Factor", cite_map), table_cell),
                Paragraph(narrative_html("Selection", cite_map), table_cell),
                Paragraph(narrative_html("Rating", cite_map), table_cell),
                Paragraph(narrative_html("Score", cite_map), table_cell),
                Paragraph(narrative_html("Weight", cite_map), table_cell),
            ]
        ]
        for row in rows:
            cra_table_data.append(
                [
                    Paragraph(narrative_html(str(row.get("group") or ""), cite_map), table_cell),
                    Paragraph(narrative_html(str(row.get("factor") or ""), cite_map), table_cell),
                    Paragraph(narrative_html(str(row.get("selection") or ""), cite_map), table_cell),
                    Paragraph(narrative_html(str(row.get("risk_rating") or ""), cite_map), table_cell),
                    Paragraph(
                        narrative_html(
                            str(row.get("score") if row.get("score") is not None else ""),
                            cite_map,
                        ),
                        table_cell,
                    ),
                    Paragraph(
                        narrative_html(
                            str(row.get("weight") if row.get("weight") is not None else ""),
                            cite_map,
                        ),
                        table_cell,
                    ),
                ]
            )
        cra_table = Table(
            cra_table_data,
            colWidths=[28 * mm, 28 * mm, 52 * mm, 22 * mm, 16 * mm, 18 * mm],
        )
        cra_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#111111")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 7),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        story.append(Spacer(1, 4))
        story.append(cra_table)

    def add_section(title: str, text: str) -> None:
        story.append(_p(title, h2))
        append_prose_section(text)

    add_section(
        "6. Company overview",
        sections.get("4_client_overview")
        or sections.get("3_client_overview")
        or report.get("company_overview")
        or "",
    )
    add_section(
        "7. Ownership / UBO",
        sections.get("5_ownership_ubo")
        or sections.get("4_ownership_ubo")
        or report.get("ultimate_beneficial_ownership")
        or "",
    )
    add_section(
        "8. Adverse media",
        sections.get("6_adverse_media")
        or sections.get("5_adverse_media")
        or report.get("adverse_media")
        or "",
    )
    add_section(
        "9. Litigation",
        sections.get("7_litigation")
        or sections.get("6_litigation")
        or report.get("litigation")
        or "",
    )
    add_section(
        "10. Reputation / management",
        sections.get("8_reputation_management")
        or sections.get("7_reputation_management")
        or report.get("management_investigation")
        or "",
    )

    gaps = sections.get("9_document_gaps") or {}
    story.append(_p("11. Document gaps and next actions", h2))
    missing = list(gaps.get("missing_documents") or report.get("missing_documents") or [])
    requests = list(gaps.get("document_requests") or report.get("document_requests") or [])
    actions = list(gaps.get("next_manual_actions") or report.get("next_manual_actions") or [])
    if not (missing or requests or actions):
        story.append(_p("No outstanding document gaps flagged.", body))
    else:
        append_bullet_group("Missing documents", missing)
        append_bullet_group("Document requests", requests)
        append_bullet_group("Next manual actions", actions)

    story.append(_p("Appendix A — Sources", h2))
    if not citations:
        story.append(_p("No citations recorded.", muted))
    else:
        for cite in citations:
            story.append(
                Paragraph(citation_appendix_line(cite), bullet, bulletText="•")
            )

    story.append(Spacer(1, 8))
    story.append(
        _p(
            "Advisory only — not an automatic approval or clearance decision.",
            muted,
        )
    )

    doc.build(story)
    return str(path)


def annotate_evidence_citations(
    evidence: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Assign citation numbers and return (evidence_with_cites, appendix entries)."""
    appendix: list[dict] = []
    url_to_num: dict[str, int] = {}
    annotated: list[dict] = []
    for item in evidence:
        e = dict(item)
        url = (e.get("url") or "").strip()
        key = url or f"noid:{e.get('source')}:{e.get('claim', '')[:40]}"
        if key not in url_to_num:
            url_to_num[key] = len(appendix) + 1
            appendix.append(
                {
                    "id": url_to_num[key],
                    "title": e.get("source") or "Untitled source",
                    "url": url,
                    "published_date": e.get("published_date") or "n/d",
                    "provenance": e.get("provenance") or "",
                    "entity": e.get("entity") or "",
                    "category": e.get("category") or "",
                }
            )
        e["_cite"] = str(url_to_num[key])
        annotated.append(e)
    return annotated, appendix


def _sections_from_synthesis(
    synthesized: SynthesizedEddReport,
    *,
    recommendation: str,
    rationale: str,
    cra: dict,
    state: InvestigationState,
    ubo_status: str,
) -> InvestigationReport:
    return InvestigationReport(
        executive_summary=format_executive_summary(
            synthesized,
            cra=cra,
        ),
        company_overview=synthesized.company_overview,
        management_investigation=synthesized.reputation_management,
        ultimate_beneficial_ownership=format_ownership_section(synthesized),
        adverse_media=synthesized.adverse_media,
        litigation=synthesized.litigation,
        reputation=synthesized.reputation_management,
        risk_findings=[],
        recommendation=recommendation,  # type: ignore[arg-type]
        recommendation_rationale=rationale,
        coverage=CoverageReport.model_validate(state.get("coverage") or {}),
        missing_documents=state.get("missing_documents") or [],
        document_requests=state.get("document_requests") or [],
        next_manual_actions=list(
            dict.fromkeys(
                list((state.get("coverage") or {}).get("suggested_actions") or [])
                + list(state.get("document_requests") or [])[:3]
            )
        ),
        ubo_status=ubo_status,  # type: ignore[arg-type]
    )


def build_edd_report(state: InvestigationState) -> dict:
    synthesized_raw = state.get("synthesized_edd")
    if not synthesized_raw:
        raise RuntimeError("synthesized_edd is required before rendering the EDD report")

    entities = state.get("entities") or {}
    company = entities.get("company") or {}
    company_name = company.get("legal_name") or "Unknown Company"
    evidence_raw = state.get("evidence") or []
    _evidence, appendix = annotate_evidence_citations(evidence_raw)
    ubo_status = state.get("ubo_status") or "absent"
    recommendation = state.get("recommendation") or "Review"
    rationale = state.get("recommendation_rationale") or ""
    cra = state.get("cra") or {}
    persons = entities.get("persons") or []
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    synthesized = SynthesizedEddReport.model_validate(synthesized_raw)
    purpose = synthesized.purpose.strip() or (
        "This Enhanced Due Diligence report summarises public-domain investigation "
        "findings and the deterministic Client Risk Assessment (CRA) mapping used to "
        "support analyst review. It does not replace sanctions screening providers, "
        "identity verification, or formal CRA workbook sign-off."
    )
    base = _sections_from_synthesis(
        synthesized,
        recommendation=recommendation,
        rationale=rationale,
        cra=cra,
        state=state,
        ubo_status=ubo_status,
    )
    client, client_detail_rows = company_detail_rows_to_client(
        synthesized.company_detail_rows,
        {
            "legal_name": company_name,
            "aliases": company.get("aliases") or [],
            "registration_number": company.get("registration_number"),
            "jurisdiction": company.get("jurisdiction"),
            "address": company.get("address"),
            "industry": company.get("industry"),
            "trade_licence_number": company.get("trade_licence_number"),
        },
    )

    report = {
        "title": "Enhanced Due Diligence Report — Public Domain Investigation",
        "subtitle": "Co-working / serviced-office tenant onboarding",
        "generated_at": generated_at,
        "client": client,
        "persons": persons,
        "cra_summary": {
            "risk_level": cra.get("risk_level"),
            "weighted_score": cra.get("weighted_score"),
            "matrix_version": cra.get("matrix_version"),
            "unacceptable": cra.get("unacceptable"),
            "overrides": cra.get("overrides") or {},
            "rows": cra_table_rows(cra),
        },
        "recommendation": recommendation,
        "recommendation_rationale": rationale,
        "sections": {
            "1_recommendation": {
                "recommendation": recommendation,
                "rationale": rationale,
                "cra_risk_level": cra.get("risk_level"),
            },
            "2_purpose": purpose,
            "3_executive_summary": base.executive_summary,
            "4_client_overview": base.company_overview,
            "5_ownership_ubo": base.ultimate_beneficial_ownership,
            "6_adverse_media": base.adverse_media,
            "7_litigation": base.litigation,
            "8_reputation_management": base.management_investigation,
            "9_document_gaps": {
                "missing_documents": base.missing_documents,
                "document_requests": base.document_requests,
                "next_manual_actions": base.next_manual_actions,
            },
        },
        "appendix_a_citations": appendix,
        "synthesized_edd": synthesized_raw,
        "overall_assessment": synthesized_raw.get("overall_assessment"),
        "source_coverage": synthesized_raw.get("source_coverage"),
        **base.model_dump(),
    }
    if client_detail_rows:
        report["client_detail_rows"] = client_detail_rows
    return report


def render_report_node(state: InvestigationState) -> dict:
    edd = build_edd_report(state)
    return {
        "report": edd,
    }


def synthesize_report_node(state: InvestigationState) -> dict:
    evidence_raw = state.get("evidence") or []
    annotated, _appendix = annotate_evidence_citations(evidence_raw)

    emit(
        {
            "kind": "progress",
            "node": "synthesize_report",
            "message": "Synthesizing EDD narrative from documents and public-domain research…",
        }
    )

    synthesized = synthesize_edd_report(state, annotated, _appendix)
    emit(
        {
            "kind": "detail",
            "node": "synthesize_report",
            "message": (
                f"**Synthesis complete** — {synthesized.overall_assessment}; "
                f"{len(synthesized.investigation_findings)} investigation finding(s)."
            ),
        }
    )
    return {
        "evidence": annotated,
        "synthesized_edd": synthesized.model_dump(),
    }
