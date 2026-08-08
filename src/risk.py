"""Advisory risk recommendation and CRA assessment nodes."""

from __future__ import annotations

from kyc_agent.cra_matrix import build_cra_assessment
from kyc_agent.state import InvestigationState, Recommendation


def assess_recommendation(
    *,
    adverse_count: int,
    missing_documents: list[str],
    inconsistency_flags: list[str],
    coverage_percent: float,
) -> tuple[Recommendation, str]:
    reasons: list[str] = []

    if adverse_count >= 3:
        reasons.append(
            f"{adverse_count} confirmed adverse public-domain hits require enhanced review"
        )
        return "Escalate", "; ".join(reasons)

    if adverse_count >= 1:
        reasons.append(f"{adverse_count} adverse public-domain hit(s) need analyst review")

    reasons.append("UBO not verified — ownership incomplete")

    if missing_documents:
        reasons.append(f"{len(missing_documents)} mandatory document gap(s)")

    if inconsistency_flags:
        reasons.append(f"{len(inconsistency_flags)} cross-document inconsistency flag(s)")

    if coverage_percent < 60:
        reasons.append("Investigation coverage below acceptable threshold")

    return "Review", "; ".join(reasons)


def assess_risk_node(state: InvestigationState) -> dict:
    evidence = state.get("evidence") or []
    adverse_count = sum(1 for e in evidence if e.get("adverse"))
    coverage = state.get("coverage") or {}
    recommendation, rationale = assess_recommendation(
        adverse_count=adverse_count,
        missing_documents=state.get("missing_documents") or [],
        inconsistency_flags=state.get("inconsistency_flags") or [],
        coverage_percent=float(coverage.get("percent") or 0),
    )

    ubo_status = "inferred" if any(
        e.get("category") == "ownership" and e.get("provenance") == "inferred"
        for e in evidence
    ) else "absent"

    return {
        "recommendation": recommendation,
        "recommendation_rationale": rationale,
        "ubo_status": ubo_status,
    }


def _recommendation_from_cra(cra: dict) -> tuple[Recommendation, str]:
    if cra.get("unacceptable") or cra.get("overrides", {}).get("sanctions_unsc_uae_ofac"):
        return (
            "Escalate",
            "CRA produced Unacceptable / sanctions override — Enhanced Due Diligence required.",
        )
    level = cra.get("risk_level") or "Medium"
    score = cra.get("weighted_score")
    if level == "High":
        return (
            "Escalate",
            f"CRA risk level High (weighted score {score}). Enhanced Due Diligence required.",
        )
    if level == "Medium":
        return (
            "Review",
            f"CRA risk level Medium (weighted score {score}). Analyst review before clearance.",
        )
    return (
        "Proceed",
        f"CRA risk level Low (weighted score {score}). Advisory only — not an automatic approval.",
    )


def cra_assessment_node(state: InvestigationState) -> dict:
    entities = state.get("entities") or {}
    company = entities.get("company") or {}
    persons = entities.get("persons") or []
    evidence = state.get("evidence") or []
    corporates = entities.get("corporate_shareholders") or []
    ownership_gaps = entities.get("ownership_gaps") or []

    adverse_confirmed = any(e.get("adverse") for e in evidence)

    ubo_countries = [
        p.get("nationality")
        for p in persons
        if (p.get("role") or "").lower() in {"beneficial_owner_candidate", "shareholder"}
        and p.get("nationality")
    ]
    ubo_country = ubo_countries[0] if ubo_countries else None

    complex_ownership = (
        bool(corporates)
        or bool(ownership_gaps)
        or state.get("ubo_status") in {"absent", "inferred"}
    )

    cra = build_cra_assessment(
        client_name=company.get("legal_name") or "Unknown Client",
        client_country_raw=company.get("jurisdiction"),
        ubo_country_raw=ubo_country,
        industry=company.get("industry"),
        complex_ownership=complex_ownership,
        adverse_confirmed=adverse_confirmed,
        adverse_pending=False,
        sanctions_hit=False,
    )

    recommendation, rationale = _recommendation_from_cra(cra)
    return {
        "cra": cra,
        "recommendation": recommendation,
        "recommendation_rationale": rationale,
    }
