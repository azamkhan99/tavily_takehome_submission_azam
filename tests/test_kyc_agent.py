"""Unit tests for KYC investigation helpers."""

from __future__ import annotations

import pytest
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from kyc_agent.app import serialize_result
from kyc_agent.graph import run_investigation

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "acme_holdings"
from kyc_agent.risk import assess_recommendation
from kyc_agent.investigate import (
    PlannerContext,
    build_followup_queries,
    build_gap_fill_objectives_and_queries,
    build_objectives_and_queries,
    evaluate_coverage,
    identify_information_gaps,
    summarize_cross_verify_context,
)
from kyc_agent.intake import (
    ExtractionResult,
    PerDocumentCompanyHints,
    PerDocumentExtraction,
    _normalize_extraction,
    cross_verify_document_extractions,
    evaluate_missing_documents,
    extract_documents_parallel,
    extract_entities_from_documents,
    infer_doc_type,
    load_documents,
    materialize_upload_dir,
)
from kyc_agent.cra_matrix import build_cra_assessment
from kyc_agent.reporting import (
    EddSourceCoverage,
    SynthesizedEddReport,
    build_edd_report,
    build_synthesis_inputs,
)
from kyc_agent.runtime import emit
from kyc_agent.tavily_client import (
    TavilyTrace,
    claim_hash,
    dedupe_evidence,
    redact_secrets,
)
from kyc_agent.state import CompanyEntity, Evidence, PersonEntity, SearchQuery


def minimal_synthesized_edd(**overrides) -> dict:
    base = SynthesizedEddReport(
        purpose="Purpose from synthesis.",
        executive_summary="Synthesized executive summary.",
        company_overview="Synthesized company overview.",
        ownership_structure="Ownership unresolved from available inputs.",
        identified_ubo="Not identified",
        adverse_media="No relevant adverse media findings were identified in the sources searched.",
        litigation="No material litigation identified in the sources searched.",
        reputation_management="No material management concerns identified.",
        investigation_findings=[],
        overview_key_findings=["A", "B", "C", "D"],
        source_coverage=EddSourceCoverage(),
        overall_assessment="Further investigation required",
    )
    data = base.model_dump()
    data.update(overrides)
    return data


def test_planner_formats_investigation_plan_summary():
    from kyc_agent.investigate import (
        InformationGap,
        InvestigationObjective,
        SearchQuery,
        format_investigation_plan_summary,
    )

    summary = format_investigation_plan_summary(
        context_summary="From documents: legal name `Acme`",
        information_gaps=[
            InformationGap(
                gap_id="missing_jurisdiction",
                category="company",
                description="Documents did not yield jurisdiction",
                search_entity="Acme",
                fill_target="jurisdiction",
            )
        ],
        objectives=[
            InvestigationObjective(
                id="company_profile",
                label="Company profile",
                category="company",
                entity="Acme",
            )
        ],
        queries=[
            SearchQuery(
                objective_id="company_profile",
                entity="Acme",
                category="company",
                query="Acme company profile",
                rationale="Establish public footprint",
            )
        ],
    )
    assert "deterministic planner" in summary
    assert "company_profile" in summary
    assert "Acme company profile" in summary


def test_entity_resolution_summary_notes_no_llm():
    from kyc_agent.intake import format_entity_resolution_summary

    text = format_entity_resolution_summary(
        legal_name="OCTOPUS ENERGY LIMITED",
        aliases=["Octopus Energy"],
        persons=[],
    )
    assert "no LLM" in text
    assert "OCTOPUS ENERGY LIMITED" in text


def test_linkify_citation_markers():
    from kyc_agent.reporting import linkify_citation_markers

    text = "Incorporated in England [3] with subsidiaries [1, 4]."
    linked = linkify_citation_markers(text, {"1": "https://example.com/1", "3": "https://example.com/3"})
    assert 'href="https://example.com/3"' in linked
    assert 'href="https://example.com/1"' in linked
    assert "[4]." in linked
    assert 'href="https://example.com/4"' not in linked


def test_normalize_synthesized_payload_accepts_llm_aliases():
    from kyc_agent.reporting import (
        SynthesizedEddReport,
        normalize_synthesized_payload,
    )

    raw = {
        "purpose": "Purpose",
        "executive_summary": "Summary",
        "company_detail_rows": [
            {"label": "Legal Name", "value": "Octopus Energy Limited"},
        ],
        "company_overview": "Overview",
        "ownership_structure": "Ownership",
        "identified_ubo": "Not identified",
        "adverse_media": "None",
        "litigation": "None",
        "reputation_management": "None",
        "investigation_findings": [
            {
                "subject": "Company Incorporation",
                "category": "company",
                "severity": 0.7,
                "description": "Incorporated in England and Wales.",
                "confidence": 0.8,
                "supporting_sources": ["Tavily Research [1, 3, 4]"],
            }
        ],
        "overview_key_findings": ["A", "B", "C", "D"],
        "source_coverage": {},
        "overall_assessment": "Further investigation required",
    }
    parsed = SynthesizedEddReport.model_validate(normalize_synthesized_payload(raw))
    assert parsed.company_detail_rows[0].field == "Legal Name"
    assert parsed.investigation_findings[0].finding == "Incorporated in England and Wales."
    assert parsed.investigation_findings[0].confidence == "High"


def test_extract_json_ignores_schema_echo():
    from kyc_agent.llm import extract_json, looks_like_json_schema

    schema_blob = {
        "$defs": {"EddCompanyDetailRow": {"properties": {"field": {"type": "string"}}}},
        "properties": {"purpose": {"type": "string"}},
        "title": "SynthesizedEddReport",
        "type": "object",
    }
    payload = {
        "purpose": "Purpose text",
        "executive_summary": "Summary",
        "company_overview": "Overview",
        "ownership_structure": "Ownership",
        "identified_ubo": "Not identified",
        "adverse_media": "None",
        "litigation": "None",
        "reputation_management": "None",
        "overall_assessment": "Further investigation required",
    }
    text = json.dumps(schema_blob) + "\n" + json.dumps(payload)
    assert looks_like_json_schema(schema_blob)
    parsed = extract_json(text, required_keys={"purpose", "executive_summary"})
    assert parsed["purpose"] == "Purpose text"


def test_cra_matrix_maps_complex_ownership_and_score():
    cra = build_cra_assessment(
        client_name="Acme Holdings FZ-LLC",
        client_country_raw="Dubai, United Arab Emirates",
        ubo_country_raw=None,
        industry="Corporate advisory / holding company",
        complex_ownership=True,
        adverse_confirmed=False,
        adverse_pending=False,
        net_wealth_raw="Less than AED 10 Million",
    )
    assert cra["client_name"] == "Acme Holdings FZ-LLC"
    assert cra["matrix_version"] == "2025-01-01"
    assert cra["elements"]["country_geography"]["client_country"]["selection"] == (
        "United Arab Emirates"
    )
    assert cra["elements"]["customer_risk"]["complex_ownership"]["risk_rating"] == "High"
    assert cra["elements"]["product_service"]["transaction_delivery_channel"][
        "selection"
    ] == "UAE bank account"
    assert cra["elements"]["product_service"]["estimated_net_wealth"]["selection"] == (
        "Less than AED 10 Million"
    )
    assert "weighted_score" in cra
    assert cra["risk_level"] in {"Low", "Medium", "High"}


def test_cra_uk_jurisdiction_does_not_fall_back_to_uae():
    cra = build_cra_assessment(
        client_name="Octopus Energy Limited",
        client_country_raw="England and Wales",
        ubo_country_raw=None,
        industry="Trade of electricity",
        complex_ownership=True,
        adverse_confirmed=False,
        adverse_pending=False,
    )
    assert cra["elements"]["country_geography"]["client_country"]["selection"] == (
        "United Kingdom"
    )
    assert cra["elements"]["product_service"]["estimated_net_wealth"]["selection"] == (
        "Unknown / Not verified"
    )
    assert cra["elements"]["product_service"]["transaction_delivery_channel"][
        "selection"
    ] == "Foreign bank account"
    assert cra["elements"]["customer_risk"]["type_of_client"]["selection"] == (
        "Legal entity - Regulated"
    )


def test_cra_adverse_screening_can_be_unacceptable():
    cra = build_cra_assessment(
        client_name="Acme",
        client_country_raw="UAE",
        ubo_country_raw="UAE",
        industry="office",
        complex_ownership=False,
        adverse_confirmed=True,
        adverse_pending=False,
    )
    screening = cra["elements"]["other_risks"]["screening_results"]
    assert screening["unacceptable"] is True
    assert screening["risk_rating"] == "Unacceptable"


def test_build_edd_report_requires_synthesis():
    with pytest.raises(RuntimeError, match="synthesized_edd is required"):
        build_edd_report({"entities": {"company": {"legal_name": "Acme"}}})


def test_build_edd_report_uses_synthesized_narrative():
    synthesized = SynthesizedEddReport(
        purpose="Synthesized purpose paragraph.",
        executive_summary="Concise executive summary for analyst review.",
        company_detail_rows=[],
        company_overview="Acme Holdings operates as a Dubai-based holding company.",
        ownership_structure="Public sources did not confirm ultimate beneficial ownership.",
        identified_ubo="Not identified",
        adverse_media="No relevant adverse media findings were identified in the sources searched.",
        litigation="No material litigation identified in the sources searched.",
        reputation_management="No material management concerns identified.",
        investigation_findings=[],
        overview_key_findings=[
            "Dubai holding company with incomplete documentary ownership.",
            "UBO not identified from documents or public sources.",
            "No relevant adverse media identified in sources searched.",
            "Further UBO documentation required.",
        ],
        source_coverage=EddSourceCoverage(
            entities_investigated=["Acme Holdings FZ-LLC"],
            research_categories_searched=["company", "ownership"],
            unresolved_areas=["UBO not verified"],
        ),
        overall_assessment="Further investigation required",
    )
    state = {
        "entities": {
            "company": {"legal_name": "Acme Holdings FZ-LLC", "jurisdiction": "UAE"},
            "persons": [],
        },
        "evidence": [
            {
                "entity": "Acme Holdings FZ-LLC",
                "category": "company",
                "claim": "| | | --- | | Formerly | Raw table junk that must not appear verbatim",
                "confidence": 0.7,
                "source": "Example",
                "published_date": "n/d",
                "url": "https://example.com/a",
            }
        ],
        "coverage": {"percent": 70, "items": [], "gaps": [], "suggested_actions": []},
        "ubo_status": "absent",
        "recommendation": "Review",
        "recommendation_rationale": "Ownership incomplete",
        "cra": {"risk_level": "Medium", "weighted_score": 6.5, "matrix_version": "2025-01-01"},
        "synthesized_edd": synthesized.model_dump(),
    }
    edd = build_edd_report(state)
    overview = edd["sections"]["4_client_overview"]
    assert "Acme Holdings operates" in overview
    assert "| | | ---" not in overview
    assert edd["sections"]["2_purpose"] == "Synthesized purpose paragraph."
    assert "Further investigation required" in edd["sections"]["3_executive_summary"]
    assert "Identified UBO" in edd["sections"]["5_ownership_ubo"]
    assert edd["sections"]["1_recommendation"]["recommendation"] == "Review"


def test_build_synthesis_inputs_redacts_person_id():
    state = {
        "entities": {
            "company": {"legal_name": "Acme"},
            "persons": [{"name": "Sara", "role": "signatory", "id_number": "SECRET123"}],
        },
        "evidence": [],
        "coverage": {},
        "cra": {},
    }
    payload = build_synthesis_inputs(state, [], [])
    person = payload["entity_resolution"]["persons"][0]
    assert "id_number" not in person


def test_edd_report_includes_appendix_citations():
    state = {
        "entities": {
            "company": {
                "legal_name": "Acme Holdings FZ-LLC",
                "jurisdiction": "Dubai, United Arab Emirates",
            },
            "persons": [],
            "corporate_shareholders": [],
            "ownership_gaps": [],
        },
        "evidence": [
            {
                "entity": "Acme Holdings FZ-LLC",
                "category": "company",
                "claim": "Public company profile note",
                "confidence": 0.7,
                "source": "Example News",
                "published_date": "2024-01-01",
                "url": "https://example.com/a",
                "provenance": "public_corroboration",
                "adverse": False,
            }
        ],
        "coverage": {"percent": 80, "items": [], "gaps": [], "suggested_actions": []},
        "ubo_status": "absent",
        "recommendation": "Review",
        "recommendation_rationale": "Ownership incomplete",
        "cra": {
            "risk_level": "Medium",
            "weighted_score": 6.5,
            "matrix_version": "2025-01-01",
            "elements": {},
            "overrides": {},
        },
        "missing_documents": ["UBO Declaration"],
        "document_requests": ["Request UBO declaration"],
        "synthesized_edd": minimal_synthesized_edd(),
    }
    edd = build_edd_report(state)
    assert "Enhanced Due Diligence" in edd["title"]
    assert edd["appendix_a_citations"]
    assert edd["appendix_a_citations"][0]["url"] == "https://example.com/a"
    assert edd["sections"]["1_recommendation"]["recommendation"] == "Review"
    assert edd["appendix_a_citations"][0]["url"] == "https://example.com/a"


def test_infer_doc_type_and_upload_materialize(tmp_path):
    assert infer_doc_type("trade_licence.md") == "trade_licence"
    assert infer_doc_type("my_kyc_form.txt") == "kyc_form"
    src = tmp_path / "kyc_form.md"
    src.write_text("# KYC\nClient: Test Co\n", encoding="utf-8")
    dest = materialize_upload_dir([src])
    docs = load_documents(dest)
    assert len(docs) == 1
    assert docs[0].doc_type == "kyc_form"


def test_load_pdf_document(tmp_path, monkeypatch):
    from pypdf import PdfWriter

    monkeypatch.setattr(
        "kyc_agent.intake.extract_pdf_via_vision",
        lambda path: f"[vision OCR stub for {path.name}]",
    )
    pdf_path = tmp_path / "trade_licence.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(pdf_path)
    docs = load_documents(tmp_path)
    assert len(docs) == 1
    assert docs[0].doc_type == "trade_licence"
    assert docs[0].content
    assert "PDF" in docs[0].content or docs[0].content.strip()


def test_search_response_to_evidence():
    from kyc_agent.tavily_client import TracedTavilyClient

    q = SearchQuery(
        objective_id="company_profile",
        entity="Acme Holdings FZ-LLC",
        category="company",
        query="Acme Holdings FZ-LLC company profile",
        rationale="Establish public company footprint",
    )
    client = object.__new__(TracedTavilyClient)
    evidence = client._search_to_evidence(
        q,
        {
            "answer": "Acme appears as a holding company in public records.",
            "results": [
                {
                    "title": "Example Source",
                    "url": "https://example.com/acme",
                    "content": "Acme Holdings FZ-LLC is a Dubai holding company.",
                    "score": 0.82,
                }
            ],
        },
    )
    assert evidence
    assert any("Acme appears" in e.claim for e in evidence)
    assert any(e.url == "https://example.com/acme" for e in evidence)


def test_delete_run_dir_only_removes_runs_under_artifacts(tmp_path, monkeypatch):
    from kyc_agent import app as app_module

    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    monkeypatch.setattr(app_module, "RUNS_DIR", runs_root)

    run_dir = runs_root / "20260101_acme"
    run_dir.mkdir()
    (run_dir / "run.json").write_text("{}", encoding="utf-8")

    assert app_module._delete_run_dir(run_dir)
    assert not run_dir.exists()
    assert not app_module._delete_run_dir(tmp_path / "outside")
    assert not app_module._delete_run_dir("/etc/passwd")


def test_emit_is_noop_outside_graph_context():
    # Outside a LangGraph run, emit should log but not raise.
    emit({"kind": "detail", "node": "test", "message": "hello"})


def test_investigation_ui_tracks_tasks_and_progress():
    from kyc_agent.app import _InvestigationUI

    ui = _InvestigationUI()
    ui.on_task_start("load_documents")
    ui.on_progress("load_documents", "Reading `kyc_form.md`…")
    ui.on_detail("load_documents", "**KYC Form** loaded")
    assert len(ui.steps) == 1
    assert ui.steps[0].live == ""
    assert "KYC Form" in ui.steps[0].details[0]
    assert ui.step_messages()[0].metadata["status"] == "pending"

    ui.on_task_complete("load_documents", {})
    assert ui.steps[0].done is True
    assert ui.step_messages()[0].metadata["status"] == "done"

    ui.on_task_start("run_searches")
    ui.on_task_start("run_searches")
    assert any("round 2" in step.label for step in ui.steps)


def test_planner_uses_inference_and_document_candidates():
    objectives, queries = build_objectives_and_queries(
        "Acme Holdings FZ-LLC",
        [
            {
                "name": "Sara Al Mansoori",
                "role": "beneficial_owner_candidate",
                "ownership_pct": 51.0,
                "ubo_basis": "shareholding",
            }
        ],
        corporate_shareholders=["Horizon Nest Investments Ltd"],
        context_summary="From documents: legal name `Acme Holdings FZ-LLC`",
    )
    assert any(q.objective_id == "ownership_inference" for q in queries)
    assert any("infer" in q.rationale.lower() for q in queries)
    assert any("document-extracted" in q.rationale.lower() for q in queries)
    assert any("Cross-verify context" in q.rationale for q in queries)
    assert any("Sara Al Mansoori" in q.query for q in queries)
    assert any("Horizon Nest" in q.query for q in queries)
    assert not any(q.objective_id.startswith("ubo_adverse") for q in queries)
    assert any(o.id == "company_adverse" for o in objectives)


def test_planner_targets_cross_verify_gaps():
    ctx = PlannerContext(
        company={"legal_name": "Acme Holdings FZ-LLC"},
        persons=[{"name": "Sara Al Mansoori", "role": "director"}],
        corporate_shareholders=["Horizon Nest Investments Ltd"],
        ownership_gaps=["Corporate shareholder ultimate owners not disclosed"],
        inconsistency_flags=["Company appears under multiple legal-name variants"],
        missing_documents=["UBO Declaration"],
        present_doc_types=["kyc_form", "register_of_shareholders"],
    )
    summary = summarize_cross_verify_context(ctx)
    assert "From documents" in summary
    assert "Not established" in summary

    gaps = identify_information_gaps(ctx)
    assert any(g.gap_id == "missing_registration_number" for g in gaps)
    assert any(g.gap_id == "missing_ubo_declaration" for g in gaps)
    assert any(g.gap_id.startswith("corp_ubo_") for g in gaps)

    gap_objectives, gap_queries = build_gap_fill_objectives_and_queries(
        gaps,
        ctx.company_name,
        context_summary=summary,
    )
    assert gap_queries
    assert all("Cross-verify gap" in q.rationale for q in gap_queries)
    assert any(q.objective_id.startswith("gap_missing_registration_number") for q in gap_queries)
    assert any("ultimate beneficial owner" in q.query.lower() for q in gap_queries)
    assert any(o.notes for o in gap_objectives)


def test_missing_docs_always_requests_ubo():
    missing, requests = evaluate_missing_documents(
        [
            "kyc_form",
            "trade_licence",
            "certificate_of_incorporation",
            "passport",
            "register_of_shareholders",
        ],
    )
    assert any("UBO" in m for m in missing)
    assert any("UBO" in r for r in requests)


def test_inferred_evidence_provenance_and_dedupe():
    items = [
        Evidence(
            entity="Acme",
            category="ownership",
            claim="Possible owner X",
            confidence=0.4,
            source="A",
            url="https://example.com/a",
            provenance="inferred",
        ),
        Evidence(
            entity="Acme",
            category="ownership",
            claim="Possible owner X",
            confidence=0.4,
            source="A",
            url="https://example.com/a",
            provenance="inferred",
        ),
    ]
    deduped = dedupe_evidence(items)
    assert len(deduped) == 1
    assert deduped[0].provenance == "inferred"
    assert claim_hash("https://example.com/a", "Possible owner X")


def test_coverage_marks_ownership_incomplete():
    objectives = [
        {
            "id": "ownership_inference",
            "label": "Ownership inference (unverified)",
            "category": "ownership",
            "entity": "Acme",
            "required": True,
        },
        {
            "id": "company_profile",
            "label": "Company profile",
            "category": "company",
            "entity": "Acme",
            "required": True,
        },
    ]
    evidence = [
        {
            "objective_id": "ownership_inference",
            "category": "ownership",
            "entity": "Acme",
            "claim": "inferred owner",
            "provenance": "inferred",
            "url": "https://example.com",
            "source": "x",
            "confidence": 0.4,
        },
        {
            "objective_id": "company_profile",
            "category": "company",
            "entity": "Acme",
            "claim": "profile",
            "url": "https://example.com/p",
            "source": "y",
            "confidence": 0.5,
        },
    ]
    report = evaluate_coverage(objectives, evidence)
    assert any("Ownership verification incomplete" in g for g in report.gaps)
    ownership_item = next(i for i in report.items if i["id"] == "ownership_inference")
    assert ownership_item["covered"] is False
    assert report.sufficient is True


def test_should_continue_to_assessment_when_public_domain_covered():
    from kyc_agent.graph import should_continue

    state = {
        "coverage": {"sufficient": True},
        "round": 1,
        "max_rounds": 2,
    }
    assert should_continue(state) == "assess_risk"


def test_model_profile_budget_defaults(monkeypatch):
    monkeypatch.delenv("NEBIUS_MODEL", raising=False)
    monkeypatch.delenv("NEBIUS_VISION_MODEL", raising=False)
    monkeypatch.setenv("NEBIUS_COST_PROFILE", "budget")
    from kyc_agent.llm import resolve_text_model, resolve_vision_model

    assert resolve_text_model() == "Qwen/Qwen3-30B-A3B-Instruct-2507"
    assert resolve_vision_model() == "openbmb/MiniCPM-V-4_5"


def test_model_profile_explicit_override(monkeypatch):
    monkeypatch.setenv("NEBIUS_COST_PROFILE", "budget")
    monkeypatch.setenv("NEBIUS_MODEL", "meta-llama/Meta-Llama-3.1-8B-Instruct")
    monkeypatch.setenv("NEBIUS_VISION_MODEL", "Qwen/Qwen2.5-VL-72B-Instruct")
    from kyc_agent.llm import resolve_text_model, resolve_vision_model

    assert resolve_text_model() == "meta-llama/Meta-Llama-3.1-8B-Instruct"
    assert resolve_vision_model() == "Qwen/Qwen2.5-VL-72B-Instruct"


def test_reflect_emits_followup_queries():
    state = {
        "coverage": {
            "gaps": [
                "Litigation",
                "Ownership verification incomplete (no formal UBO declaration)",
            ]
        },
        "entities": {"company": {"legal_name": "Acme Holdings FZ-LLC"}},
        "ubo_status": "absent",
        "tavily_traces": [],
    }
    queries = build_followup_queries(state)
    assert queries
    assert all(q.query for q in queries)
    assert any(q.category in {"litigation", "ownership"} for q in queries)


def test_recommendation_biases_review_for_incomplete_ownership():
    reco, rationale = assess_recommendation(
        adverse_count=0,
        missing_documents=["UBO Declaration"],
        inconsistency_flags=[],
        coverage_percent=80,
    )
    assert reco == "Review"
    assert "UBO" in rationale or "ownership" in rationale.lower()


def test_redact_secrets_in_traces():
    payload = {
        "api_key": "tvly-secret",
        "nested": {"authorization": "Bearer xyz"},
        "query": "safe",
        "note": "key tvly-ABCDEFG1234567890 hidden",
    }
    redacted = redact_secrets(payload)
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"]["authorization"] == "[REDACTED]"
    assert "tvly-" not in redacted["note"]
    assert redacted["query"] == "safe"


def test_normalize_collapses_ubo_role_to_candidate():
    raw = ExtractionResult(
        company=CompanyEntity(legal_name="Acme Holdings FZ-LLC"),
        persons=[
            PersonEntity(
                name="Sara Al Mansoori",
                role="ubo",
                ownership_pct=51.0,
                ubo_basis="explicit_declaration",
            )
        ],
    )
    normalized = _normalize_extraction(raw)
    assert normalized.persons[0].role == "beneficial_owner_candidate"
    assert normalized.persons[0].ubo_basis in {"shareholding", "control"}


@patch("kyc_agent.intake.structured_invoke")
def test_extract_entities_uses_llm(mock_invoke: MagicMock):
    doc_result = PerDocumentExtraction(
        doc_type="kyc_form",
        doc_label="KYC",
        persons=[
            PersonEntity(
                name="Sara Al Mansoori",
                role="beneficial_owner_candidate",
                ownership_pct=51.0,
                ubo_basis="shareholding",
            )
        ],
        corporate_shareholders=["Horizon Nest Investments Ltd"],
        ownership_gaps=["Corporate shareholder UBO undisclosed"],
    )
    cross_result = ExtractionResult(
        company=CompanyEntity(legal_name="Acme Holdings FZ-LLC"),
        persons=[
            PersonEntity(
                name="Sara Al Mansoori",
                role="beneficial_owner_candidate",
                ownership_pct=51.0,
                ubo_basis="shareholding",
            )
        ],
        corporate_shareholders=["Horizon Nest Investments Ltd"],
        ownership_gaps=["Corporate shareholder UBO undisclosed"],
    )

    def _side_effect(prompt, schema, **_kwargs):
        if schema is PerDocumentExtraction:
            return doc_result
        if schema is ExtractionResult:
            return cross_result
        raise AssertionError(f"Unexpected schema: {schema}")

    mock_invoke.side_effect = _side_effect
    result = extract_entities_from_documents(
        [{"label": "KYC", "doc_type": "kyc_form", "content": "sample text"}]
    )
    assert mock_invoke.call_count == 2
    assert result.persons[0].name == "Sara Al Mansoori"
    assert "Horizon Nest" in result.corporate_shareholders[0]


@patch("kyc_agent.intake.structured_invoke")
def test_parallel_document_extraction_runs_subagent_per_file(mock_invoke: MagicMock):
    mock_invoke.return_value = PerDocumentExtraction(
        doc_type="kyc_form",
        doc_label="KYC Form",
    )
    docs = [
        {"label": "KYC Form", "doc_type": "kyc_form", "content": "a"},
        {"label": "Trade Licence", "doc_type": "trade_licence", "content": "b"},
    ]
    slices = extract_documents_parallel(docs)
    assert len(slices) == 2
    assert mock_invoke.call_count == 2


def test_cross_verify_streams_progress_events():
    received: list[dict] = []

    def on_event(event: dict) -> None:
        received.append(event)

    slices = [
        PerDocumentExtraction(
            doc_type="kyc_form",
            doc_label="KYC Form",
            company=PerDocumentCompanyHints(legal_name="Acme Ltd"),
            persons=[PersonEntity(name="Jane Doe", role="director")],
            extraction_notes="Director listed on form.",
        )
    ]

    with patch("kyc_agent.intake.structured_invoke") as mock_invoke:
        mock_invoke.return_value = ExtractionResult(
            company=CompanyEntity(legal_name="Acme Ltd"),
            persons=[PersonEntity(name="Jane Doe", role="director")],
            extraction_notes="Merged view.",
        )
        with patch("kyc_agent.intake.emit", side_effect=on_event):
            cross_verify_document_extractions(slices)

    kinds = [event["kind"] for event in received]
    assert "progress" in kinds
    assert "detail" in kinds
    assert any("KYC Form" in str(event.get("message") or "") for event in received)


@patch("kyc_agent.intake.structured_invoke")
def test_cross_verify_reconciles_document_batch(mock_invoke: MagicMock):
    mock_invoke.return_value = ExtractionResult(
        company=CompanyEntity(legal_name="Acme Holdings FZ-LLC"),
        persons=[PersonEntity(name="Sara Al Mansoori", role="director")],
        inconsistency_flags=["Multiple legal-name variants"],
    )
    slices = [
        PerDocumentExtraction(
            doc_type="kyc_form",
            doc_label="KYC",
            company=PerDocumentCompanyHints(legal_name="Acme Holdings Limited"),
        ),
        PerDocumentExtraction(
            doc_type="trade_licence",
            doc_label="Trade Licence",
            company=PerDocumentCompanyHints(legal_name="Acme Holdings FZ-LLC"),
        ),
    ]
    result = cross_verify_document_extractions(slices)
    assert mock_invoke.called
    assert result.company.legal_name == "Acme Holdings FZ-LLC"


def _stub_search_many(queries, **_kwargs):
    evidence: list[Evidence] = []
    traces: list[TavilyTrace] = []
    for q in queries:
        provenance = (
            "inferred"
            if "infer" in q.rationale.lower() or q.category == "ownership"
            else "public_corroboration"
        )
        if provenance == "inferred" or "document-extracted" in q.rationale.lower():
            claim = (
                f"Possible ownership mention for {q.entity}. "
                "This is inferred from public web results and is NOT verified."
            )
            if "document-extracted" in q.rationale.lower():
                provenance = "document_extracted"
        else:
            claim = (
                f"Public profile information for {q.entity}. "
                "No definitive adverse finding confirmed in stub response."
            )
        evidence.append(
            Evidence(
                entity=q.entity,
                category=q.category,
                claim=claim,
                confidence=0.4,
                source="Stub source",
                published_date=None,
                url="https://example.com/stub",
                provenance=provenance,  # type: ignore[arg-type]
                objective_id=q.objective_id,
            )
        )
        traces.append(
            TavilyTrace(
                timestamp="00:00:00",
                objective_id=q.objective_id,
                query=q.query,
                params={
                    "api": "search",
                    "search_depth": "advanced",
                    "max_results": 5,
                    "topic": q.topic or "general",
                    "include_answer": True,
                },
                duration_ms=1.0,
                answer=claim,
                result_count=1,
                sources=[
                    {
                        "title": "Stub source",
                        "url": "https://example.com/stub",
                        "published_date": None,
                        "content": claim,
                        "score": 0.5,
                    }
                ],
            )
        )
    return evidence, traces


@patch("kyc_agent.reporting.synthesize_edd_report")
@patch("kyc_agent.investigate.TracedTavilyClient")
@patch("kyc_agent.intake.structured_invoke")
def test_full_graph_absent_ubo(
    mock_invoke: MagicMock,
    mock_client_cls: MagicMock,
    mock_synthesize: MagicMock,
):
    merged = ExtractionResult(
        company=CompanyEntity(
            legal_name="Acme Holdings FZ-LLC",
            aliases=["Acme Holdings"],
            registration_number="FZ-2021-88421",
        ),
        persons=[
            PersonEntity(
                name="Sara Al Mansoori",
                role="beneficial_owner_candidate",
                ownership_pct=51.0,
                ubo_basis="shareholding",
                id_number="XN7821345",
            )
        ],
        corporate_shareholders=["Horizon Nest Investments Ltd"],
        ownership_gaps=["Corporate shareholder ultimate owners not disclosed"],
        inconsistency_flags=["Multiple legal-name variants"],
    )
    doc_result = PerDocumentExtraction(
        doc_type="kyc_form",
        doc_label="KYC",
        persons=merged.persons,
    )

    def _side_effect(prompt, schema, **_kwargs):
        if schema is PerDocumentExtraction:
            return doc_result
        if schema is ExtractionResult:
            return merged
        raise AssertionError(f"Unexpected schema: {schema}")

    mock_invoke.side_effect = _side_effect
    mock_synthesize.return_value = SynthesizedEddReport.model_validate(minimal_synthesized_edd())
    mock_client_cls.return_value.search_many.side_effect = (
        lambda queries, **_kwargs: _stub_search_many(queries)
    )
    result = run_investigation(
        pack_id="acme_holdings",
        docs_dir=str(FIXTURES_DIR),
        max_rounds=2,
    )
    payload = serialize_result(result)
    assert payload["ubo_status"] in {"absent", "inferred"}
    assert payload["recommendation"] in {"Proceed", "Review", "Escalate"}
    assert payload["cra"]
    assert payload["cra"]["elements"]["country_geography"]
    assert payload["report"].get("appendix_a_citations") is not None
    assert "Enhanced Due Diligence" in (payload["report"].get("title") or "")
    assert payload["missing_documents"]
    assert any("UBO" in r for r in payload["document_requests"])
    assert payload["tavily_traces"]
    for trace in payload["tavily_traces"]:
        assert trace["params"].get("api") == "search"
    assert mock_client_cls.return_value.search_many.called
    assert mock_invoke.called
    assert mock_synthesize.called
    assert payload.get("extraction_markdown")
    assert "Extracted information" in payload["extraction_markdown"]
    assert payload.get("entities", {}).get("company", {}).get("legal_name")


def test_pdf_prose_split_handles_bullets_and_tables():
    from kyc_agent.reporting import split_prose_blocks

    intro, bullets = split_prose_blocks(
        "UBO has not been formally verified.\n\n"
        "- Clean company profile sentence. [1] (source: Example; n/d)\n"
        "- | | | --- | | Formerly | Octopus Energy Limited | Type | Subsidiary |"
    )
    assert intro and "UBO" in intro
    assert len(bullets) == 2
    assert "Clean company profile" in bullets[0]
    assert "Formerly" in bullets[1]
    assert "| ---" not in bullets[1]


def test_edd_pdf_is_written(tmp_path):
    from pathlib import Path

    from kyc_agent.reporting import edd_to_pdf, split_prose_blocks

    report = {
        "title": "Enhanced Due Diligence Report",
        "subtitle": "Test",
        "generated_at": "2026-01-01",
        "client": {"legal_name": "Acme Holdings FZ-LLC", "jurisdiction": "UAE"},
        "sections": {
            "1_recommendation": {"recommendation": "Review", "rationale": "Ownership gaps remain."},
            "2_purpose": "Purpose text",
            "3_executive_summary": "Summary with source [1].",
            "4_client_overview": "- Profile point one [1].\n- Profile point two [2].",
            "9_document_gaps": {
                "missing_documents": ["UBO Declaration"],
                "document_requests": ["Request ownership chart"],
                "next_manual_actions": ["Follow up with applicant"],
            },
        },
        "appendix_a_citations": [
            {"id": 1, "title": "Source One", "url": "https://example.com/one", "published_date": "n/d"},
            {"id": 2, "title": "Source Two", "url": "https://example.com/two", "published_date": "n/d"},
        ],
        "cra_summary": {
            "risk_level": "Medium",
            "weighted_score": 6.5,
            "matrix_version": "2025-01-01",
            "rows": [],
        },
    }
    out = tmp_path / "edd.pdf"
    path = edd_to_pdf(report, report["cra_summary"], output_path=out)
    assert Path(path).exists()
    assert Path(path).stat().st_size > 500


def test_search_api_maps_results_to_evidence():
    from kyc_agent.tavily_client import TracedTavilyClient

    q = SearchQuery(
        objective_id="company_profile",
        entity="Acme Holdings FZ-LLC",
        category="company",
        query="Acme Holdings FZ-LLC company profile",
        rationale="Establish public company footprint",
    )
    client = object.__new__(TracedTavilyClient)
    evidence = client._search_to_evidence(
        q,
        {
            "answer": "Acme is a Dubai holding company.",
            "results": [
                {
                    "title": "Company page",
                    "url": "https://example.com/acme",
                    "content": "Acme Holdings FZ-LLC provides corporate advisory services.",
                    "score": 0.82,
                    "published_date": "2024-01-01",
                }
            ],
        },
    )
    assert evidence
    assert evidence[0].claim.startswith("Acme is a Dubai")
    assert any(e.url == "https://example.com/acme" for e in evidence)
