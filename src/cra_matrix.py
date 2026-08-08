"""Deterministic CRA scoring matrix lookups (subset of enterprise CRA workbook)."""

from __future__ import annotations

from typing import Any, Literal

RiskRating = Literal["Low", "Medium", "High", "Unacceptable"]

MATRIX_VERSION = "2025-01-01"

# Leaf factor: selection -> (risk_rating, score) # LLM generated but we would create a real one with an enterprise customer.
COUNTRY_LOOKUP: dict[str, tuple[RiskRating, float]] = {
    "United Arab Emirates": ("Low", 2.5),
    "United Kingdom": ("Low", 2.5),
    "Japan": ("Low", 2.5),
    "United States": ("Low", 2.5),
    "Russian Federation": ("Medium", 5.0),
    "Unknown / Not verified": ("High", 10.0),
    "Other": ("Medium", 5.0),
}

BUSINESS_ACTIVITY_LOOKUP: dict[str, tuple[RiskRating, float]] = {
    "Combined office administrative service activities": ("Low", 2.5),
    "Holding company / corporate advisory": ("Medium", 5.0),
    "Others": ("Medium", 5.0),
}

COMPLEX_OWNERSHIP_LOOKUP: dict[str, tuple[RiskRating, float]] = {
    "Complex Ownership Structure - No": ("Low", 2.5),
    "Complex Ownership Structure - Yes": ("High", 10.0),
}

CLIENT_TYPE_LOOKUP: dict[str, tuple[RiskRating, float]] = {
    "Legal entity - Regulated": ("Low", 2.5),
    "Legal entity - Unregulated": ("Medium", 7.0),
    "Natural person": ("Low", 2.5),
}

ONBOARDING_CHANNEL_LOOKUP: dict[str, tuple[RiskRating, float]] = {
    "Face-to-face": ("Low", 2.5),
    "Non face-to-face using electronic means": ("Medium", 5.0),
}

PEP_LOOKUP: dict[str, tuple[RiskRating, float]] = {
    "Non-PEP": ("Low", 2.5),
    "PEP - Domestic": ("High", 10.0),
    "PEP - Foreign": ("High", 10.0),
}

SCREENING_LOOKUP: dict[str, tuple[RiskRating, float, bool]] = {
    "No match / clear": ("Low", 2.5, False),
    "False positive / resolved": ("Low", 2.5, False),
    "Confirmed match with adverse media coverage (whereby media coverage is stating facts correctly)": (
        "Unacceptable",
        10.0,
        True,
    ),
    "Potential match pending resolution": ("High", 10.0, False),
}

NET_WEALTH_LOOKUP: dict[str, tuple[RiskRating, float]] = {
    "Less than AED 10 Million": ("Low", 2.5),
    "AED 10 Million to AED 50 Million": ("Medium", 5.0),
    "More than AED 50 Million": ("High", 10.0),
    "Unknown / Not verified": ("High", 10.0),
}

PRODUCTS_LOOKUP: dict[str, tuple[RiskRating, float]] = {
    "Combined office administrative service activities": ("Low", 2.5),
    "Others": ("Medium", 5.0),
}

SOURCE_OF_FUNDS_LOOKUP: dict[str, tuple[RiskRating, float]] = {
    "Business income (refer business activity risk rating)": ("Medium", 5.0),
    "Salary / employment": ("Low", 2.5),
    "Investment proceeds": ("Medium", 5.0),
    "Unknown / incomplete": ("High", 10.0),
}

TX_CHANNEL_LOOKUP: dict[str, tuple[RiskRating, float]] = {
    "UAE bank account": ("Low", 2.5),
    "Foreign bank account": ("Medium", 5.0),
}


def _factor(
    selection: str,
    rating: RiskRating,
    score: float,
    weight: float,
    *,
    unacceptable: bool = False,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "risk_rating": rating,
        "score": score,
        "selection": selection,
        "weight": weight,
    }
    if unacceptable:
        out["unacceptable"] = True
    return out


def _lookup(
    table: dict[str, tuple],
    selection: str,
    weight: float,
    *,
    default: str | None = None,
) -> dict[str, Any]:
    if selection not in table:
        if default is not None and default in table:
            selection = default
        elif "Unknown / Not verified" in table:
            selection = "Unknown / Not verified"
        elif "Other" in table:
            selection = "Other"
        else:
            selection = next(iter(table))
    values = table[selection]
    if len(values) == 3:
        rating, score, unacceptable = values
        return _factor(selection, rating, score, weight, unacceptable=unacceptable)
    rating, score = values
    return _factor(selection, rating, score, weight)


def cra_table_rows(cra: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    elements = cra.get("elements") or {}
    for group_name, group in elements.items():
        if not isinstance(group, dict):
            continue
        for key, value in group.items():
            if key == "weight" or not isinstance(value, dict) or "score" not in value:
                continue
            rows.append(
                {
                    "group": group_name.replace("_", " ").title(),
                    "factor": key.replace("_", " ").title(),
                    "selection": value.get("selection"),
                    "risk_rating": value.get("risk_rating"),
                    "score": value.get("score"),
                    "weight": value.get("weight"),
                    "unacceptable": bool(value.get("unacceptable")),
                }
            )
    return rows


def normalize_country(raw: str | None) -> str:
    if not raw:
        return "Unknown / Not verified"
    text = raw.strip()
    lower = text.lower()
    if (
        "emirates" in lower
        or lower in {"uae", "dubai"}
        or "dubai" in lower
        or "abu dhabi" in lower
    ):
        return "United Arab Emirates"
    if "russia" in lower:
        return "Russian Federation"
    if (
        "united kingdom" in lower
        or lower in {"uk", "gb", "great britain"}
        or "england" in lower
        or "scotland" in lower
        or "wales" in lower
    ):
        return "United Kingdom"
    if "japan" in lower:
        return "Japan"
    if (
        "united states" in lower
        or lower in {"usa", "us", "u.s.", "u.s.a."}
        or "america" in lower
    ):
        return "United States"
    if text in COUNTRY_LOOKUP:
        return text
    return "Other"


def resolve_net_wealth(raw: str | None) -> str:
    """Map declared net-wealth band; never invent a Low band when unknown."""
    if not raw:
        return "Unknown / Not verified"
    text = raw.strip()
    if text in NET_WEALTH_LOOKUP:
        return text
    lower = text.lower()
    if "unknown" in lower or "not verified" in lower or "incomplete" in lower:
        return "Unknown / Not verified"
    if "more than" in lower or "over" in lower or ">" in lower:
        return "More than AED 50 Million"
    if "less than" in lower or "<" in lower:
        return "Less than AED 10 Million"
    if "10" in lower and "50" in lower:
        return "AED 10 Million to AED 50 Million"
    return "Unknown / Not verified"


def resolve_client_type(industry: str | None) -> str:
    industry_l = (industry or "").lower()
    regulated_markers = (
        "bank",
        "banking",
        "credit institution",
        "payment",
        "emi",
        "insurance",
        "broker",
        "regulated",
        "electricity",
        "gas supply",
        "energy supply",
    )
    if any(marker in industry_l for marker in regulated_markers):
        return "Legal entity - Regulated"
    return "Legal entity - Unregulated"


def resolve_tx_channel(client_country: str) -> str:
    if client_country == "United Arab Emirates":
        return "UAE bank account"
    return "Foreign bank account"


def build_cra_assessment(
    *,
    client_name: str,
    client_country_raw: str | None,
    ubo_country_raw: str | None,
    industry: str | None,
    complex_ownership: bool,
    adverse_confirmed: bool,
    adverse_pending: bool,
    sanctions_hit: bool = False,
    net_wealth_raw: str | None = None,
) -> dict[str, Any]:
    client_country = normalize_country(client_country_raw)
    ubo_country = normalize_country(ubo_country_raw)
    net_wealth_sel = resolve_net_wealth(net_wealth_raw)
    client_type_sel = resolve_client_type(industry)
    tx_channel_sel = resolve_tx_channel(client_country)

    industry_l = (industry or "").lower()
    if "office" in industry_l or "administr" in industry_l:
        business_sel = "Combined office administrative service activities"
        product_sel = "Combined office administrative service activities"
    elif "hold" in industry_l or "advisor" in industry_l or "consult" in industry_l:
        business_sel = "Holding company / corporate advisory"
        product_sel = "Others"
    else:
        business_sel = "Others"
        product_sel = "Others"

    if adverse_confirmed:
        screening_sel = (
            "Confirmed match with adverse media coverage "
            "(whereby media coverage is stating facts correctly)"
        )
    elif adverse_pending:
        screening_sel = "Potential match pending resolution"
    else:
        screening_sel = "No match / clear"

    complex_sel = (
        "Complex Ownership Structure - Yes"
        if complex_ownership
        else "Complex Ownership Structure - No"
    )

    elements = {
        "country_geography": {
            "client_country": _lookup(
                COUNTRY_LOOKUP,
                client_country,
                0.1,
                default="Unknown / Not verified",
            ),
            "ubo_country": _lookup(
                COUNTRY_LOOKUP,
                ubo_country,
                0.1,
                default="Unknown / Not verified",
            ),
            "weight": 0.2,
        },
        "customer_risk": {
            "business_activity": _lookup(
                BUSINESS_ACTIVITY_LOOKUP, business_sel, 0.1, default="Others"
            ),
            "complex_ownership": _lookup(
                COMPLEX_OWNERSHIP_LOOKUP, complex_sel, 0.05
            ),
            "type_of_client": _lookup(CLIENT_TYPE_LOOKUP, client_type_sel, 0.05),
            "weight": 0.2,
        },
        "delivery_channel": {
            "onboarding_channel": _lookup(
                ONBOARDING_CHANNEL_LOOKUP,
                "Non face-to-face using electronic means",
                0.05,
            ),
            "weight": 0.05,
        },
        "other_risks": {
            "pep_status": _lookup(PEP_LOOKUP, "Non-PEP", 0.05),
            "screening_results": _lookup(SCREENING_LOOKUP, screening_sel, 0.2),
            "weight": 0.25,
        },
        "product_service": {
            "estimated_net_wealth": _lookup(
                NET_WEALTH_LOOKUP,
                net_wealth_sel,
                0.1,
                default="Unknown / Not verified",
            ),
            "products_services": _lookup(
                PRODUCTS_LOOKUP, product_sel, 0.05, default="Others"
            ),
            "source_of_funds": _lookup(
                SOURCE_OF_FUNDS_LOOKUP,
                "Business income (refer business activity risk rating)",
                0.1,
            ),
            "transaction_delivery_channel": _lookup(
                TX_CHANNEL_LOOKUP, tx_channel_sel, 0.05
            ),
            "weight": 0.3,
        },
    }

    weighted_score, risk_level, unacceptable = score_cra(elements)
    overrides = {
        "notes": (
            "Sanctions override — UNSC/UAE/OFAC match from screening matrix lookup"
            if sanctions_hit
            else "No sanctions override applied (WorldCheck / sanctions provider out of scope for this demo)."
        ),
        "sanctions_other_lists": False,
        "sanctions_unsc_uae_ofac": sanctions_hit,
    }
    if sanctions_hit:
        risk_level = "High"
        unacceptable = True

    return {
        "client_name": client_name,
        "elements": elements,
        "matrix_version": MATRIX_VERSION,
        "overrides": overrides,
        "weighted_score": round(weighted_score, 2),
        "risk_level": risk_level,
        "unacceptable": unacceptable,
        "rationale": (
            "Deterministic CRA mapping from Cross Verification, public-domain Research "
            "resolution, and file extractions using CRA Scoring Matrix lookup tables."
        ),
    }


def score_cra(elements: dict[str, Any]) -> tuple[float, str, bool]:
    """Compute weighted composite score and risk band.

    Thresholds (aligned with enterprise CRA framing):
    0–6 Low, 6.1–8 Medium, 8.1–10 High. Unacceptable leaf factors force High.
    """
    total = 0.0
    unacceptable = False
    for group in elements.values():
        if not isinstance(group, dict):
            continue
        for key, value in group.items():
            if key == "weight" or not isinstance(value, dict):
                continue
            if "score" not in value or "weight" not in value:
                continue
            total += float(value["score"]) * float(value["weight"])
            if value.get("unacceptable") or value.get("risk_rating") == "Unacceptable":
                unacceptable = True

    # Scale: leaf weights already sum to ~1.0, scores are 0–10 → composite ~0–10
    if unacceptable or total >= 8.1:
        level = "High"
    elif total >= 6.1:
        level = "Medium"
    else:
        level = "Low"
    return total, level, unacceptable
