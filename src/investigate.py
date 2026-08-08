"""Public-domain investigation: plan, search, resolve hits, coverage, reflect."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from kyc_agent.llm import structured_invoke
from kyc_agent.runtime import emit
from kyc_agent.state import (
    CoverageReport,
    Evidence,
    EvidenceCategory,
    InvestigationObjective,
    InvestigationState,
    SearchQuery,
    TavilyTrace,
)
from kyc_agent.tavily_client import TracedTavilyClient, dedupe_evidence

COMPANY_FIELD_GAPS: tuple[tuple[str, str], ...] = (
    ("registration_number", "company registration number"),
    ("jurisdiction", "jurisdiction / country of incorporation"),
    ("address", "registered address"),
    ("industry", "business activity / industry"),
    ("trade_licence_number", "trade licence number"),
)


@dataclass
class PlannerContext:
    company: dict
    persons: list[dict]
    corporate_shareholders: list[str]
    ownership_gaps: list[str]
    inconsistency_flags: list[str]
    missing_documents: list[str]
    present_doc_types: list[str]
    document_extractions: list[dict] = field(default_factory=list)

    @classmethod
    def from_state(cls, state: InvestigationState) -> PlannerContext:
        entities = state.get("entities") or {}
        return cls(
            company=dict(entities.get("company") or {}),
            persons=list(entities.get("persons") or []),
            corporate_shareholders=list(entities.get("corporate_shareholders") or []),
            ownership_gaps=list(entities.get("ownership_gaps") or []),
            inconsistency_flags=list(state.get("inconsistency_flags") or []),
            missing_documents=list(state.get("missing_documents") or []),
            present_doc_types=list(state.get("present_doc_types") or []),
            document_extractions=list(state.get("document_extractions") or []),
        )

    @property
    def company_name(self) -> str:
        return str(self.company.get("legal_name") or "Unknown Company")


@dataclass(frozen=True)
class InformationGap:
    gap_id: str
    category: EvidenceCategory
    description: str
    search_entity: str
    fill_target: str


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def summarize_cross_verify_context(ctx: PlannerContext) -> str:
    """Short narrative of what documents established vs what remains unknown."""
    known: list[str] = []
    company = ctx.company
    if company.get("legal_name"):
        known.append(f"legal name `{company['legal_name']}`")
    if company.get("registration_number"):
        known.append(f"registration `{company['registration_number']}`")
    if company.get("jurisdiction"):
        known.append(f"jurisdiction `{company['jurisdiction']}`")
    if company.get("trade_licence_number"):
        known.append(f"trade licence `{company['trade_licence_number']}`")
    if ctx.persons:
        names = ", ".join(
            p.get("name") or "unnamed"
            for p in ctx.persons[:4]
            if p.get("name")
        )
        if names:
            known.append(f"persons: {names}")
    if ctx.corporate_shareholders:
        known.append(
            "corporate shareholders: " + ", ".join(ctx.corporate_shareholders[:3])
        )

    unknown: list[str] = []
    for field_name, label in COMPANY_FIELD_GAPS:
        if _is_missing(company.get(field_name)):
            unknown.append(label)
    unknown.extend(ctx.ownership_gaps)
    if any("UBO" in item for item in ctx.missing_documents):
        unknown.append("formal UBO declaration / verified ownership chain")
    if ctx.inconsistency_flags:
        unknown.extend(ctx.inconsistency_flags[:3])

    parts: list[str] = []
    if known:
        parts.append("From documents: " + "; ".join(known))
    if unknown:
        parts.append("Not established from documents: " + "; ".join(dict.fromkeys(unknown)))
    return " | ".join(parts) if parts else "Limited structured data from document extraction"


def identify_information_gaps(ctx: PlannerContext) -> list[InformationGap]:
    """Map cross-verify output to fields that public search should try to fill."""
    gaps: list[InformationGap] = []
    company_name = ctx.company_name
    seen_ids: set[str] = set()

    def push(
        gap_id: str,
        category: EvidenceCategory,
        description: str,
        search_entity: str,
        fill_target: str,
    ) -> None:
        if gap_id in seen_ids:
            return
        seen_ids.add(gap_id)
        gaps.append(
            InformationGap(
                gap_id=gap_id,
                category=category,
                description=description,
                search_entity=search_entity,
                fill_target=fill_target,
            )
        )

    for field_name, label in COMPANY_FIELD_GAPS:
        if _is_missing(ctx.company.get(field_name)):
            push(
                f"missing_{field_name}",
                "company",
                f"Documents did not yield {label}",
                company_name,
                label,
            )

    for idx, gap_text in enumerate(ctx.ownership_gaps):
        push(
            f"ownership_gap_{idx}",
            "ownership",
            gap_text,
            company_name,
            "ultimate beneficial ownership",
        )

    for idx, flag in enumerate(ctx.inconsistency_flags):
        flag_lower = flag.lower()
        if any(
            token in flag_lower
            for token in ("variant", "mismatch", "conflict", "inconsistent", "discrep")
        ):
            push(
                f"inconsistency_{idx}",
                "company",
                flag,
                company_name,
                "legal name / registry verification",
            )

    if any("UBO" in item for item in ctx.missing_documents):
        push(
            "missing_ubo_declaration",
            "ownership",
            "UBO declaration not provided — ownership chain unverified from documents",
            company_name,
            "ultimate beneficial ownership",
        )

    if not ctx.persons:
        push(
            "missing_persons",
            "company",
            "No directors or signatories extracted from onboarding documents",
            company_name,
            "directors and officers",
        )

    for idx, person in enumerate(ctx.persons[:4]):
        name = person.get("name") or ""
        if not name:
            continue
        role = (person.get("role") or "").lower()
        if _is_missing(person.get("nationality")):
            push(
                f"person_{idx}_nationality",
                "company",
                f"Nationality not extracted for {name} ({role or 'role unknown'})",
                name,
                f"nationality and background for {name}",
            )
        if _is_missing(person.get("id_number")) and "passport" not in ctx.present_doc_types:
            push(
                f"person_{idx}_identity",
                "company",
                f"Identity document number not extracted for {name}",
                name,
                f"public identity references for {name}",
            )

    for idx, corp in enumerate(ctx.corporate_shareholders[:3]):
        push(
            f"corp_ubo_{idx}",
            "ownership",
            f"Ultimate owners of corporate shareholder `{corp}` not verified from documents",
            corp,
            f"beneficial owners of {corp}",
        )

    return gaps


def _gap_to_search(gap: InformationGap, company_name: str) -> tuple[str, str]:
    """Return (query text, topic)."""
    entity = gap.search_entity
    target = gap.fill_target.lower()

    if "registration number" in target:
        return (
            f'"{company_name}" company registration number OR incorporation number',
            "general",
        )
    if "jurisdiction" in target:
        return (f'"{company_name}" jurisdiction incorporation country registered', "general")
    if "registered address" in target:
        return (f'"{company_name}" registered office address headquarters', "general")
    if "industry" in target or "business activity" in target:
        return (f'"{company_name}" business activity industry sector', "general")
    if "trade licence" in target:
        return (f'"{company_name}" trade licence number commercial license UAE', "general")
    if "directors" in target or "officers" in target:
        return (f'"{company_name}" directors OR officers OR management team', "general")
    if "legal name" in target or "registry verification" in target:
        return (
            f'"{company_name}" official legal name OR registry OR "{company_name}" company',
            "general",
        )
    if "nationality" in target or "background" in target:
        return (f'"{entity}" nationality biography professional background', "general")
    if "identity" in target:
        return (
            f'"{entity}" director OR signatory "{company_name}" public profile',
            "general",
        )
    if "beneficial owner" in target or "ultimate beneficial" in target:
        if entity != company_name:
            return (
                f'"{entity}" ultimate beneficial owner OR shareholders OR parent company',
                "general",
            )
        return (
            f'"{company_name}" ultimate beneficial owner OR UBO OR shareholders register',
            "general",
        )
    return (f'"{entity}" {gap.fill_target} public records', "general")


def build_gap_fill_objectives_and_queries(
    gaps: list[InformationGap],
    company_name: str,
    *,
    context_summary: str,
) -> tuple[list[InvestigationObjective], list[SearchQuery]]:
    objectives: list[InvestigationObjective] = []
    queries: list[SearchQuery] = []

    for gap in gaps:
        oid = f"gap_{gap.gap_id}"[:64]
        query_text, topic = _gap_to_search(gap, company_name)
        objectives.append(
            InvestigationObjective(
                id=oid,
                label=f"Fill document gap — {gap.fill_target}",
                category=gap.category,
                entity=gap.search_entity,
                required=True,
                notes=gap.description,
            )
        )
        queries.append(
            SearchQuery(
                objective_id=oid,
                entity=gap.search_entity,
                category=gap.category,
                query=query_text,
                topic=topic,  # type: ignore[arg-type]
                rationale=(
                    f"Cross-verify gap: {gap.description}. "
                    f"Attempting to fill `{gap.fill_target}` via public web search (unverified). "
                    f"Context: {context_summary}"
                ),
            )
        )
    return objectives, queries


def _append_context(rationale: str, context_summary: str) -> str:
    if not context_summary:
        return rationale
    return f"{rationale} | Cross-verify context: {context_summary}"


def build_objectives_and_queries(
    company_name: str,
    persons: list[dict],
    *,
    corporate_shareholders: list[str] | None = None,
    context_summary: str = "",
) -> tuple[list[InvestigationObjective], list[SearchQuery]]:
    objectives: list[InvestigationObjective] = []
    queries: list[SearchQuery] = []
    corporates = corporate_shareholders or []

    def add(
        oid: str,
        label: str,
        category: str,
        entity: str,
        query: str,
        *,
        topic: str = "general",
        rationale: str = "",
        required: bool = True,
        notes: str = "",
    ) -> None:
        objectives.append(
            InvestigationObjective(
                id=oid,
                label=label,
                category=category,  # type: ignore[arg-type]
                entity=entity,
                required=required,
                notes=notes or context_summary,
            )
        )
        queries.append(
            SearchQuery(
                objective_id=oid,
                entity=entity,
                category=category,  # type: ignore[arg-type]
                query=query,
                topic=topic,  # type: ignore[arg-type]
                rationale=_append_context(rationale, context_summary),
            )
        )

    add(
        "company_profile",
        "Company profile",
        "company",
        company_name,
        f"{company_name} company profile Dubai",
        rationale="Establish public company footprint",
    )
    add(
        "company_litigation",
        "Litigation",
        "litigation",
        company_name,
        f"{company_name} litigation OR lawsuit OR court",
        rationale="Litigation screen",
    )
    add(
        "company_regulatory",
        "Regulatory / sanctions mentions",
        "adverse_media",
        company_name,
        f"{company_name} sanctions OR regulatory action OR fine",
        topic="news",
        rationale="Regulatory and sanctions media",
    )
    add(
        "company_adverse",
        "Adverse media",
        "adverse_media",
        company_name,
        f"{company_name} fraud OR money laundering OR bribery OR corruption",
        topic="news",
        rationale="Adverse media screen",
    )
    add(
        "company_reputation",
        "Reputation",
        "reputation",
        company_name,
        f"{company_name} executive interview OR reputation OR review",
        rationale="Reputation signals",
        required=False,
    )

    ownership_candidates = [
        p
        for p in persons
        if (p.get("role") or "").lower() in {"beneficial_owner_candidate", "shareholder"}
        or (p.get("ubo_basis") or "").lower() in {"shareholding", "control"}
    ]
    directors = [
        p
        for p in persons
        if (p.get("role") or "").lower()
        in {"director", "signatory", "authorised_signatory", "authorized_signatory"}
    ]
    screened_names: set[str] = set()

    add(
        "ownership_inference",
        "Ownership inference (unverified)",
        "ownership",
        company_name,
        f"{company_name} beneficial owners OR shareholders OR ultimate owner",
        rationale="Infer ownership from public sources — unverified",
    )

    for idx, candidate in enumerate(ownership_candidates[:3]):
        name = candidate.get("name") or "Ownership candidate"
        if name.lower() in screened_names:
            continue
        screened_names.add(name.lower())
        pct = candidate.get("ownership_pct")
        pct_note = f" ({pct}%)" if pct is not None else ""
        add(
            f"doc_owner_adverse_{idx}",
            f"Document ownership candidate — {name}{pct_note}",
            "ownership",
            name,
            f"{name} fraud OR sanctions OR beneficial owner",
            topic="news",
            rationale="Document-extracted ownership candidate (not a formal UBO declaration)",
        )
        add(
            f"doc_owner_background_{idx}",
            f"Ownership candidate background — {name}",
            "ownership",
            name,
            f"{name} {company_name} shareholder OR owner OR director",
            rationale="Investigate document-extracted ownership candidate — unverified UBO",
        )

    for idx, corp in enumerate(corporates[:2]):
        add(
            f"corporate_shareholder_{idx}",
            f"Corporate shareholder research — {corp}",
            "ownership",
            corp,
            f"{corp} ownership OR beneficial owners OR ultimate owner",
            rationale="Infer ownership via corporate shareholder — unverified",
            required=False,
        )

    for idx, person in enumerate(directors[:2]):
        name = person.get("name") or "Director"
        if name.lower() in screened_names:
            continue
        screened_names.add(name.lower())
        add(
            f"director_adverse_{idx}",
            f"Director adverse media — {name}",
            "adverse_media",
            name,
            f"{name} fraud OR sanctions OR litigation",
            topic="news",
            rationale="Director adverse screen",
        )
        add(
            f"director_background_{idx}",
            f"Director background — {name}",
            "reputation",
            name,
            f"{name} previous companies OR director OR executive",
            rationale="Director background",
            required=False,
        )

    return objectives, queries


def format_investigation_plan_summary(
    *,
    context_summary: str,
    information_gaps: list[InformationGap],
    objectives: list[InvestigationObjective],
    queries: list[SearchQuery],
) -> str:
    lines = [
        "**Investigation plan** (deterministic planner — no LLM)",
        "",
        f"**Cross-verify context:** {context_summary}",
        "",
    ]
    if information_gaps:
        lines.append(f"**Document / entity gaps ({len(information_gaps)}):**")
        for gap in information_gaps[:12]:
            lines.append(f"- `{gap.gap_id}` — {gap.description}")
        if len(information_gaps) > 12:
            lines.append(f"- … and {len(information_gaps) - 12} more")
        lines.append("")
    else:
        lines.append("**Document / entity gaps:** none flagged")
        lines.append("")

    lines.append(f"**Objectives queued ({len(objectives)}):**")
    for objective in objectives[:14]:
        req = "required" if objective.required else "optional"
        lines.append(
            f"- `{objective.id}` — {objective.label} ({req}, {objective.category})"
        )
    if len(objectives) > 14:
        lines.append(f"- … and {len(objectives) - 14} more")
    lines.append("")

    lines.append(f"**Tavily search queries ({len(queries)}):**")
    for query in queries[:14]:
        lines.append(f"- `{query.objective_id}` → `{query.query}`")
        if query.rationale:
            preview = query.rationale[:220] + ("…" if len(query.rationale) > 220 else "")
            lines.append(f"  - _{preview}_")
    if len(queries) > 14:
        lines.append(f"- … and {len(queries) - 14} more")
    return "\n".join(lines)


def _merge_plans(
    gap_objectives: list[InvestigationObjective],
    gap_queries: list[SearchQuery],
    standard_objectives: list[InvestigationObjective],
    standard_queries: list[SearchQuery],
) -> tuple[list[InvestigationObjective], list[SearchQuery]]:
    """Place gap-fill tasks first; skip standard objectives that duplicate gap-fill ids."""
    gap_ids = {o.id for o in gap_objectives}
    objectives = list(gap_objectives)
    queries = list(gap_queries)
    for objective, query in zip(standard_objectives, standard_queries, strict=True):
        if objective.id in gap_ids:
            continue
        objectives.append(objective)
        queries.append(query)
    return objectives, queries


def plan_investigation_node(state: InvestigationState) -> dict:
    emit(
        {
            "kind": "progress",
            "node": "plan_investigation",
            "message": "Building public-domain objectives from document gaps…",
        }
    )
    ctx = PlannerContext.from_state(state)
    company_name = ctx.company_name
    context_summary = summarize_cross_verify_context(ctx)
    information_gaps = identify_information_gaps(ctx)

    emit(
        {
            "kind": "detail",
            "node": "plan_investigation",
            "message": f"**Cross-verify context**\n\n{context_summary}",
        }
    )
    if information_gaps:
        gap_lines = "\n".join(
            f"- {gap.description} → search `{gap.search_entity}` for {gap.fill_target}"
            for gap in information_gaps[:10]
        )
        emit(
            {
                "kind": "detail",
                "node": "plan_investigation",
                "message": f"**Gaps to fill via search**\n\n{gap_lines}",
            }
        )

    gap_objectives, gap_queries = build_gap_fill_objectives_and_queries(
        information_gaps,
        company_name,
        context_summary=context_summary,
    )
    standard_objectives, standard_queries = build_objectives_and_queries(
        company_name,
        ctx.persons,
        corporate_shareholders=ctx.corporate_shareholders,
        context_summary=context_summary,
    )
    objectives, queries = _merge_plans(
        gap_objectives,
        gap_queries,
        standard_objectives,
        standard_queries,
    )

    emit(
        {
            "kind": "detail",
            "node": "plan_investigation",
            "message": format_investigation_plan_summary(
                context_summary=context_summary,
                information_gaps=information_gaps,
                objectives=objectives,
                queries=queries,
            ),
        }
    )

    return {
        "objectives": [o.model_dump() for o in objectives],
        "pending_queries": [q.model_dump() for q in queries],
        "investigation_plan_context": context_summary,
    }


def run_searches_node(state: InvestigationState) -> dict:
    raw_queries = state.get("pending_queries") or []
    queries = [SearchQuery.model_validate(q) for q in raw_queries]

    client = TracedTavilyClient()

    def on_trace(trace: TavilyTrace) -> None:
        dump = trace.model_dump()
        emit(
            {
                "kind": "detail",
                "node": "run_searches",
                "message": f"Tavily: {trace.query}",
                "trace": dump,
            }
        )

    def on_progress(message: str) -> None:
        emit(
            {
                "kind": "progress",
                "node": "run_searches",
                "message": message,
            }
        )

    new_evidence, trace_models = client.search_many(
        queries,
        on_trace=on_trace,
        on_progress=on_progress,
    )
    traces = [t.model_dump() for t in trace_models]

    rationale_by_oid = {q.objective_id: q.rationale.lower() for q in queries}
    normalized: list[Evidence] = []
    for ev in new_evidence:
        rationale = rationale_by_oid.get(ev.objective_id or "", "")
        if "document-extracted" in rationale:
            ev.provenance = "document_extracted"
        elif ev.category == "ownership":
            ev.provenance = "inferred"
        normalized.append(ev)

    prior = [Evidence.model_validate(e) for e in (state.get("evidence") or [])]
    deduped = dedupe_evidence(prior + normalized)
    round_num = int(state.get("round") or 0) + 1
    return {
        "evidence": [e.model_dump() for e in deduped],
        "tavily_traces": traces,
        "pending_queries": [],
        "round": round_num,
    }


ADVERSE_PATTERNS = (
    r"\bfraud(?:ulent)?\b",
    r"\bsanction(?:s|ed)?\b",
    r"\bmoney[\s-]?laundering\b",
    r"\bbriber(?:y|ed)\b",
    r"\bcorruption\b",
    r"\bindict(?:ed|ment)\b",
    r"\bconvicted\b",
    r"\balle(?:ges|ged|gation)s?\b",
    r"\benforcement action\b",
    r"\bpenalt(?:y|ies)\b",
    r"\bfined\b",
    r"\barrest(?:ed)?\b",
    r"\blawsuit\b",
    r"\bsued\b",
)

NEGATION_CUES = (
    "no evidence",
    "no definitive",
    "not found",
    "no record",
    "unrelated",
    "no adverse",
    "does not appear",
    "no indication",
    "not associated",
    "no known",
    "cleared",
    "not verified",
    "no material",
)


class HitResolution(BaseModel):
    adverse_indices: list[int] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _looks_negated(text: str) -> bool:
    lower = text.lower()
    return any(cue in lower for cue in NEGATION_CUES)


def _keyword_hit(text: str) -> bool:
    if _looks_negated(text):
        return False
    return any(re.search(pat, text, flags=re.IGNORECASE) for pat in ADVERSE_PATTERNS)


def _heuristic_resolve(evidence: list[dict]) -> list[dict]:
    updated: list[dict] = []
    for item in evidence:
        text = f"{item.get('claim', '')}"
        category = item.get("category")
        item = dict(item)
        # Preserve structured adverse flags when already set.
        if item.get("adverse") is True:
            updated.append(item)
            continue
        if category in {"adverse_media", "litigation", "ubo"}:
            item["adverse"] = _keyword_hit(text)
        elif category in {"company", "reputation", "ownership"}:
            item["adverse"] = _keyword_hit(text) and not _looks_negated(text)
        else:
            item["adverse"] = False if item.get("adverse") is None else item.get("adverse")
        updated.append(item)
    return updated


def resolve_hits_node(state: InvestigationState) -> dict:
    evidence = [dict(e) for e in (state.get("evidence") or [])]
    if not evidence:
        return {}

    updated = _heuristic_resolve(evidence)
    candidates = [
        (i, e)
        for i, e in enumerate(updated)
        if e.get("category") in {"adverse_media", "litigation", "ubo"} and e.get("adverse")
    ]

    # Ask LLM only to confirm/deny heuristic positives (reduces false escalations)
    if candidates:
        try:
            payload = [
                {
                    "index": i,
                    "entity": e.get("entity"),
                    "claim": (e.get("claim") or "")[:500],
                    "url": e.get("url"),
                }
                for i, e in candidates[:10]
            ]
            resolution = structured_invoke(
                (
                    "You are a KYC analyst. For each public-domain hit, decide whether it is a "
                    "GENUINE adverse finding about the named entity (fraud, sanctions, crime, "
                    "regulatory penalty, serious litigation against them).\n"
                    "Reject hits that merely repeat search terms, discuss generic risk, or say "
                    "no issues were found.\n"
                    "Return JSON with adverse_indices = indices that are truly adverse.\n\n"
                    f"HITS:\n{payload}"
                ),
                HitResolution,
            )
            confirmed = set(resolution.adverse_indices)
            for i, _e in candidates:
                updated[i]["adverse"] = i in confirmed
        except Exception:  # noqa: BLE001
            # Keep conservative heuristic if LLM unavailable
            pass

    return {
        "evidence": updated,
    }


def evaluate_coverage(
    objectives: list[dict],
    evidence: list[dict],
    *,
    missing_documents: list[str] | None = None,
) -> CoverageReport:
    by_objective: dict[str, list[dict]] = {}
    for item in evidence:
        oid = item.get("objective_id") or ""
        by_objective.setdefault(oid, []).append(item)

    items: list[dict] = []
    gaps: list[str] = []
    covered_required = 0
    required_total = 0
    public_required = 0
    public_covered = 0

    for obj in objectives:
        oid = obj.get("id", "")
        required = bool(obj.get("required", True))
        related = by_objective.get(oid, [])
        if not related:
            related = [
                e
                for e in evidence
                if e.get("category") == obj.get("category")
                and e.get("entity") == obj.get("entity")
            ]
        # Ownership is never fully verified without a formal UBO pack
        category = obj.get("category")
        if category == "ownership":
            is_covered = False
            gaps.append("Ownership verification incomplete (no formal UBO declaration)")
        else:
            is_covered = len(related) >= 1
        if required:
            required_total += 1
            if is_covered:
                covered_required += 1
            elif category != "ownership":
                gaps.append(obj.get("label") or oid)
            if category != "ownership":
                public_required += 1
                if is_covered:
                    public_covered += 1
        items.append(
            {
                "id": oid,
                "label": obj.get("label"),
                "covered": is_covered,
                "required": required,
                "evidence_count": len(related),
            }
        )

    uniq_gaps = list(dict.fromkeys(gaps))
    percent = (covered_required / required_total * 100.0) if required_total else 100.0
    suggested: list[str] = [
        "Request UBO declaration and full shareholder register from applicant"
    ]
    if missing_documents:
        suggested.extend(f"Obtain missing document: {m}" for m in missing_documents[:5])
    for g in uniq_gaps:
        if "Ownership" not in g:
            suggested.append(f"Follow up investigation for: {g}")

    public_domain_sufficient = public_required == 0 or public_covered == public_required
    sufficient = percent >= 75.0 or public_domain_sufficient

    return CoverageReport(
        percent=round(percent, 1),
        items=items,
        gaps=uniq_gaps,
        suggested_actions=list(dict.fromkeys(suggested))[:8],
        sufficient=sufficient,
    )


def evaluate_coverage_node(state: InvestigationState) -> dict:
    coverage = evaluate_coverage(
        state.get("objectives") or [],
        state.get("evidence") or [],
        missing_documents=state.get("missing_documents") or [],
    )
    covered_ids = {i["id"] for i in coverage.items if i.get("covered")}
    objectives = []
    for obj in state.get("objectives") or []:
        o = dict(obj)
        o["covered"] = o.get("id") in covered_ids
        objectives.append(o)

    return {
        "coverage": coverage.model_dump(),
        "objectives": objectives,
    }


def build_followup_queries(state: InvestigationState) -> list[SearchQuery]:
    coverage = state.get("coverage") or {}
    gaps = coverage.get("gaps") or []
    entities = state.get("entities") or {}
    company = (entities.get("company") or {}).get("legal_name") or "Unknown Company"
    existing = {
        (t.get("query") or "").lower() for t in (state.get("tavily_traces") or [])
    }
    queries: list[SearchQuery] = []

    for gap in gaps:
        gap_l = gap.lower()
        if "ownership" in gap_l:
            q = SearchQuery(
                objective_id="ownership_followup",
                entity=company,
                category="ownership",
                query=f"{company} ultimate beneficial owner OR UBO OR shareholders register",
                topic="general",
                rationale="Infer ownership follow-up — unverified",
            )
            if q.query.lower() not in existing:
                queries.append(q)
        elif "litigation" in gap_l:
            q = SearchQuery(
                objective_id="litigation_followup",
                entity=company,
                category="litigation",
                query=f"{company} court case OR dispute OR sued",
                topic="news",
                rationale="Litigation coverage follow-up",
            )
            if q.query.lower() not in existing:
                queries.append(q)
        elif "adverse" in gap_l or "regulatory" in gap_l:
            q = SearchQuery(
                objective_id="adverse_followup",
                entity=company,
                category="adverse_media",
                query=f'"{company}" scandal OR allegation OR enforcement',
                topic="news",
                rationale="Adverse media follow-up",
            )
            if q.query.lower() not in existing:
                queries.append(q)
        elif "company profile" in gap_l:
            q = SearchQuery(
                objective_id="company_followup",
                entity=company,
                category="company",
                query=f"{company} official website OR about",
                rationale="Company profile follow-up",
            )
            if q.query.lower() not in existing:
                queries.append(q)

    if not any(q.objective_id == "ownership_followup" for q in queries):
        q = SearchQuery(
            objective_id="ownership_followup",
            entity=company,
            category="ownership",
            query=f"{company} owned by OR founder OR beneficial ownership UAE",
            rationale="Infer ownership follow-up — unverified",
        )
        if q.query.lower() not in existing:
            queries.append(q)

    return queries[:4]


def reflect_node(state: InvestigationState) -> dict:
    queries = build_followup_queries(state)
    return {
        "pending_queries": [q.model_dump() for q in queries],
    }
