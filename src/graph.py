"""LangGraph wiring for the KYC public-domain investigation."""

from __future__ import annotations

from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from kyc_agent.intake import (
    cross_verify_node,
    extract_documents_node,
    load_documents_node,
    missing_docs_node,
    resolve_entities_node,
)
from kyc_agent.investigate import (
    evaluate_coverage_node,
    plan_investigation_node,
    reflect_node,
    resolve_hits_node,
    run_searches_node,
)
from kyc_agent.reporting import render_report_node, synthesize_report_node
from kyc_agent.risk import assess_risk_node, cra_assessment_node
from kyc_agent.state import InvestigationState

NODE_LABELS: dict[str, str] = {
    "load_documents": "Loading onboarding documents",
    "extract_documents": "Running parallel document extraction subagents",
    "cross_verify": "Cross-verifying extracted entities",
    "missing_docs": "Checking document completeness",
    "resolve_entities": "Normalizing entity aliases",
    "plan_investigation": "Planning public-domain objectives",
    "run_searches": "Running Tavily search",
    "resolve_hits": "Resolving adverse media hits",
    "evaluate_coverage": "Evaluating investigation coverage",
    "reflect": "Reflecting on coverage gaps",
    "assess_risk": "Assessing public-domain risk",
    "cra_assessment": "Scoring client risk assessment",
    "synthesize_report": "Synthesizing due diligence narrative",
    "render_report": "Rendering due diligence report",
}


def should_continue(state: InvestigationState) -> Literal["reflect", "assess_risk"]:
    coverage = state.get("coverage") or {}
    round_num = int(state.get("round") or 0)
    max_rounds = int(state.get("max_rounds") or 2)
    if coverage.get("sufficient"):
        return "assess_risk"
    if round_num >= max_rounds:
        return "assess_risk"
    return "reflect"


def build_graph():
    graph = StateGraph(InvestigationState)
    graph.add_node("load_documents", load_documents_node)
    graph.add_node("extract_documents", extract_documents_node)
    graph.add_node("cross_verify", cross_verify_node)
    graph.add_node("missing_docs", missing_docs_node)
    graph.add_node("resolve_entities", resolve_entities_node)
    graph.add_node("plan_investigation", plan_investigation_node)
    graph.add_node("run_searches", run_searches_node)
    graph.add_node("resolve_hits", resolve_hits_node)
    graph.add_node("evaluate_coverage", evaluate_coverage_node)
    graph.add_node("reflect", reflect_node)
    graph.add_node("assess_risk", assess_risk_node)
    graph.add_node("cra_assessment", cra_assessment_node)
    graph.add_node("synthesize_report", synthesize_report_node)
    graph.add_node("render_report", render_report_node)

    graph.add_edge(START, "load_documents")
    graph.add_edge("load_documents", "extract_documents")
    graph.add_edge("extract_documents", "cross_verify")
    graph.add_edge("cross_verify", "missing_docs")
    graph.add_edge("missing_docs", "resolve_entities")
    graph.add_edge("resolve_entities", "plan_investigation")
    graph.add_edge("plan_investigation", "run_searches")
    graph.add_edge("run_searches", "resolve_hits")
    graph.add_edge("resolve_hits", "evaluate_coverage")
    graph.add_conditional_edges(
        "evaluate_coverage",
        should_continue,
        {
            "reflect": "reflect",
            "assess_risk": "assess_risk",
        },
    )
    graph.add_edge("reflect", "run_searches")
    graph.add_edge("assess_risk", "cra_assessment")
    graph.add_edge("cra_assessment", "synthesize_report")
    graph.add_edge("synthesize_report", "render_report")
    graph.add_edge("render_report", END)
    return graph.compile()


def run_investigation(
    pack_id: str = "upload",
    *,
    docs_dir: str | None = None,
    max_rounds: int = 2,
) -> dict[str, Any]:
    """Run the full investigation graph and return the final state."""
    app = build_graph()
    initial: InvestigationState = {
        "pack_id": pack_id,
        "max_rounds": max_rounds,
        "round": 0,
        "evidence": [],
        "tavily_traces": [],
        "pending_queries": [],
    }
    if docs_dir:
        initial["docs_dir"] = docs_dir
    return app.invoke(initial)


def stream_investigation(
    pack_id: str = "upload",
    *,
    docs_dir: str | None = None,
    max_rounds: int = 2,
):
    """Stream tasks + custom progress + values for the Gradio UI."""
    app = build_graph()
    initial: InvestigationState = {
        "pack_id": pack_id,
        "max_rounds": max_rounds,
        "round": 0,
        "evidence": [],
        "tavily_traces": [],
        "pending_queries": [],
    }
    if docs_dir:
        initial["docs_dir"] = docs_dir
    return app.stream(
        initial,
        stream_mode=["tasks", "custom", "values"],
    )
