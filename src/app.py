"""Gradio case UI for KYC public-domain investigation."""

from __future__ import annotations

import base64
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

import gradio as gr
from dotenv import load_dotenv
from gradio import ChatMessage

from kyc_agent.graph import NODE_LABELS, should_continue, stream_investigation
from kyc_agent.intake import materialize_upload_dir
from kyc_agent.reporting import edd_to_pdf

load_dotenv()

DEFAULT_MAX_ROUNDS = 2
RUNS_DIR = Path(__file__).resolve().parent.parent / "artifacts" / "runs"

FILE_TYPES = [".md", ".txt", ".markdown", ".pdf", ".png", ".jpg", ".jpeg"]
WAITING_MD = "_Waiting for investigation…_"
EMPTY_CRA: dict[str, Any] = {}
TAB_EDD = "edd_report"
STEPS_CSS = """
#step-chatbot { min-height: 480px; }
#upload-row .file-preview { max-height: 4.5rem; overflow-y: auto; }
"""


def coverage_checklist(coverage: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for item in coverage.get("items") or []:
        mark = "✓" if item.get("covered") else "⚠"
        req = "required" if item.get("required") else "optional"
        lines.append(
            f"{mark} {item.get('label')} ({req}, {item.get('evidence_count', 0)} evidence)"
        )
    for gap in coverage.get("gaps") or []:
        if not any(gap in line for line in lines):
            lines.append(f"⚠ {gap}")
    return lines


def format_extraction_markdown(
    *,
    entities: dict[str, Any] | None = None,
    documents: list[dict[str, Any]] | None = None,
    inconsistency_flags: list[str] | None = None,
    missing_documents: list[str] | None = None,
    ubo_status: str | None = None,
) -> str:
    """Human-readable extracted intake for the side file viewer."""
    entities = entities or {}
    company = entities.get("company") or {}
    persons = entities.get("persons") or []
    corporates = entities.get("corporate_shareholders") or []
    gaps = entities.get("ownership_gaps") or []
    lines: list[str] = [
        "# Extracted information",
        "",
        "## Company",
        "",
        f"- **Legal name:** {company.get('legal_name') or 'n/a'}",
        f"- **Aliases:** {', '.join(company.get('aliases') or []) or 'n/a'}",
        f"- **Registration:** {company.get('registration_number') or 'n/a'}",
        f"- **Trade licence:** {company.get('trade_licence_number') or 'n/a'}",
        f"- **Jurisdiction:** {company.get('jurisdiction') or 'n/a'}",
        f"- **Address:** {company.get('address') or 'n/a'}",
        f"- **Industry:** {company.get('industry') or 'n/a'}",
        f"- **UBO status:** {ubo_status or entities.get('ubo_status') or 'n/a'}",
        "",
        "## Persons",
        "",
    ]
    if not persons:
        lines.append("_No persons extracted._")
        lines.append("")
    else:
        for person in persons:
            pct = person.get("ownership_pct")
            pct_s = f", {pct}%" if pct is not None else ""
            bits = [
                f"- **{person.get('name') or 'Unknown'}** — "
                f"role `{person.get('role') or 'n/a'}`{pct_s}"
            ]
            if person.get("ubo_basis"):
                bits[0] += f", basis `{person.get('ubo_basis')}`"
            if person.get("id_number"):
                bits[0] += f", ID `{person.get('id_number')}`"
            lines.append(bits[0])
        lines.append("")

    lines.extend(["## Corporate shareholders", ""])
    if not corporates:
        lines.append("_None extracted._")
        lines.append("")
    else:
        for corp in corporates:
            lines.append(f"- {corp}")
        lines.append("")

    lines.extend(["## Ownership gaps", ""])
    if not gaps:
        lines.append("_None flagged._")
        lines.append("")
    else:
        for gap in gaps:
            lines.append(f"- {gap}")
        lines.append("")

    flags = inconsistency_flags or entities.get("inconsistency_flags") or []
    lines.extend(["## Inconsistency flags", ""])
    if not flags:
        lines.append("_None._")
        lines.append("")
    else:
        for flag in flags:
            lines.append(f"- {flag}")
        lines.append("")

    missing = missing_documents or []
    lines.extend(["## Missing documents", ""])
    if not missing:
        lines.append("_None flagged yet._")
        lines.append("")
    else:
        for item in missing:
            lines.append(f"- {item}")
        lines.append("")

    docs = documents or []
    if docs:
        lines.extend(["## Source documents (intake text)", ""])
        for doc in docs:
            label = doc.get("label") or doc.get("doc_type") or "Document"
            content = (doc.get("content") or "").strip()
            preview = content[:1200] + ("…" if len(content) > 1200 else "")
            lines.append(f"### {label}")
            lines.append("")
            lines.append(preview or "_Empty_")
            lines.append("")

    return "\n".join(lines)


def serialize_result(state: dict[str, Any]) -> dict[str, Any]:
    report = state.get("report") or {}
    coverage = state.get("coverage") or report.get("coverage") or {}
    return {
        "pack_id": state.get("pack_id"),
        "ubo_status": state.get("ubo_status"),
        "entities": state.get("entities"),
        "documents": state.get("documents") or [],
        "inconsistency_flags": state.get("inconsistency_flags") or [],
        "missing_documents": state.get("missing_documents") or [],
        "document_requests": state.get("document_requests") or [],
        "recommendation": state.get("recommendation") or report.get("recommendation"),
        "recommendation_rationale": state.get("recommendation_rationale")
        or report.get("recommendation_rationale"),
        "coverage": coverage,
        "coverage_checklist": coverage_checklist(coverage),
        "tavily_traces": state.get("tavily_traces") or [],
        "evidence": state.get("evidence") or [],
        "cra": state.get("cra") or {},
        "report": report,
        "extraction_markdown": format_extraction_markdown(
            entities=state.get("entities") or {},
            documents=state.get("documents") or [],
            inconsistency_flags=state.get("inconsistency_flags") or [],
            missing_documents=state.get("missing_documents") or [],
            ubo_status=state.get("ubo_status"),
        ),
    }


@dataclass
class RunView:
    step_chat: list[Any] = field(default_factory=list)
    summary: str = ""
    extraction_md: str = WAITING_MD
    evidence_md: str = WAITING_MD
    cra: dict[str, Any] = field(default_factory=dict)
    recommendation_md: str = WAITING_MD
    report_html: str = ""
    select_edd_tab: bool = False

    def as_tuple(self) -> tuple[Any, ...]:
        tabs = gr.Tabs(selected=TAB_EDD) if self.select_edd_tab else gr.Tabs()
        return (
            self.step_chat,
            self.summary,
            self.extraction_md,
            self.evidence_md,
            self.cra,
            self.recommendation_md,
            self.report_html,
            tabs,
        )

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "step_chat": _serialize_chat_messages(self.step_chat),
            "summary": self.summary,
            "extraction_md": self.extraction_md,
            "evidence_md": self.evidence_md,
            "cra": self.cra,
            "recommendation_md": self.recommendation_md,
            # PDF is reloaded from run.json / disk — do not embed base64 here.
        }

    @classmethod
    def from_snapshot(cls, data: dict[str, Any]) -> RunView:
        raw_chat = data.get("step_chat")
        if raw_chat is None and data.get("steps_md"):
            raw_chat = [
                {
                    "role": "assistant",
                    "content": data.get("steps_md"),
                    "metadata": {"title": "Investigation steps", "id": "snapshot"},
                }
            ]
        return cls(
            step_chat=_deserialize_chat_messages(raw_chat),
            summary=str(data.get("summary") or ""),
            extraction_md=str(data.get("extraction_md") or WAITING_MD),
            evidence_md=str(data.get("evidence_md") or WAITING_MD),
            cra=dict(data.get("cra") or {}),
            recommendation_md=str(data.get("recommendation_md") or WAITING_MD),
            report_html="",
            select_edd_tab=False,
        )


def _serialize_chat_messages(messages: list[Any]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, ChatMessage):
            serialized.append(gr.utils.shallow_asdict(msg))
        elif isinstance(msg, dict):
            serialized.append(msg)
    return serialized


def _deserialize_chat_messages(messages: Any) -> list[Any]:
    if not messages:
        return []
    restored: list[Any] = []
    for msg in messages:
        if isinstance(msg, dict):
            restored.append(
                ChatMessage(
                    role=msg.get("role") or "assistant",
                    content=msg.get("content") or "",
                    metadata=msg.get("metadata") or {},
                )
            )
    return restored


def _new_run_dir(stem: str) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_dir = RUNS_DIR / f"{ts}_{stem}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _write_json(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return str(path)


def _persist_run(
    *,
    stem: str,
    serialized: dict[str, Any],
    report: dict[str, Any],
    cra: dict[str, Any],
    snapshot: dict[str, Any] | None = None,
) -> tuple[str, str, str, str]:
    run_dir = _new_run_dir(stem)
    pdf_path = edd_to_pdf(report, cra, output_path=run_dir / f"{stem}_edd_report.pdf")
    extraction_json = _write_json(
        run_dir / f"{stem}_extraction.json",
        {
            "entities": serialized.get("entities"),
            "documents": [
                {
                    "doc_type": d.get("doc_type"),
                    "label": d.get("label"),
                    "path": d.get("path"),
                    "content": d.get("content"),
                }
                for d in (serialized.get("documents") or [])
            ],
            "missing_documents": serialized.get("missing_documents"),
            "inconsistency_flags": serialized.get("inconsistency_flags"),
            "ubo_status": serialized.get("ubo_status"),
        },
    )
    cra_json = _write_json(run_dir / f"{stem}_cra.json", cra)
    _write_json(
        run_dir / "run.json",
        {
            "pack_id": serialized.get("pack_id"),
            "recommendation": serialized.get("recommendation"),
            "recommendation_rationale": serialized.get("recommendation_rationale"),
            "ubo_status": serialized.get("ubo_status"),
            "artifact_dir": str(run_dir),
            "pdf": pdf_path,
            "extraction_json": extraction_json,
            "cra_json": cra_json,
        },
    )
    if snapshot is not None:
        _write_json(run_dir / "run_snapshot.json", snapshot)
    return pdf_path, extraction_json, cra_json, str(run_dir)


def _run_choices() -> list[tuple[str, str]]:
    if not RUNS_DIR.exists():
        return []
    choices: list[tuple[str, str]] = []
    for run_dir in sorted(RUNS_DIR.iterdir(), key=lambda path: path.name, reverse=True):
        if not run_dir.is_dir():
            continue
        label = run_dir.name
        manifest_path = run_dir / "run.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                recommendation = manifest.get("recommendation") or ""
                if recommendation:
                    label = f"{run_dir.name} · {recommendation}"
            except (OSError, json.JSONDecodeError):
                pass
        choices.append((label, str(run_dir)))
    return choices


def _run_dataset_data() -> tuple[list[list[str]], list[str]]:
    choices = _run_choices()
    return [[path] for _label, path in choices], [label for label, _path in choices]


def _delete_run_dir(run_dir: str | Path) -> bool:
    path = Path(run_dir).resolve()
    runs_root = RUNS_DIR.resolve()
    try:
        path.relative_to(runs_root)
    except ValueError:
        return False
    if not path.is_dir():
        return False
    shutil.rmtree(path)
    return True


def _run_dir_for_index(index: int | None) -> str | None:
    if index is None:
        return None
    choices = _run_choices()
    if index < 0 or index >= len(choices):
        return None
    return choices[index][1]


def _refresh_runs_dataset() -> gr.Dataset:
    samples, labels = _run_dataset_data()
    return gr.Dataset(samples=samples, sample_labels=labels)


def _pdf_html(pdf_path: str | None) -> str:
    if not pdf_path or not Path(pdf_path).exists():
        return "<p><em>No EDD report PDF available.</em></p>"
    pdf_b64 = base64.b64encode(Path(pdf_path).read_bytes()).decode("ascii")
    return (
        "<iframe "
        'title="EDD report" '
        f'src="data:application/pdf;base64,{pdf_b64}" '
        'width="100%" height="640" style="border:none;"></iframe>'
    )


def _manifest_pdf_path(run_path: Path) -> str | None:
    manifest_path = run_path / "run.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    pdf_path = str(manifest.get("pdf") or "").strip()
    if pdf_path and Path(pdf_path).exists():
        return pdf_path
    # Fallback: any *_edd_report.pdf in the run folder
    matches = sorted(run_path.glob("*_edd_report.pdf"))
    return str(matches[0]) if matches else None


def _outputs_from_run_dir(run_dir: str) -> RunView:
    run_path = Path(run_dir)
    pdf_path = _manifest_pdf_path(run_path)
    report_html = _pdf_html(pdf_path)
    has_pdf = bool(pdf_path)

    snapshot_path = run_path / "run_snapshot.json"
    if snapshot_path.exists():
        try:
            data = json.loads(snapshot_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                view = RunView.from_snapshot(data)
                # Snapshots often omit the PDF iframe (saved before render); always reload from disk.
                view.report_html = report_html
                view.select_edd_tab = has_pdf
                return view
        except (OSError, json.JSONDecodeError):
            pass

    manifest_path = run_path / "run.json"
    if not manifest_path.exists():
        return RunView(summary=f"Run folder not found: `{run_dir}`")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    recommendation = manifest.get("recommendation") or "Review"
    rationale = manifest.get("recommendation_rationale") or ""

    extraction_md = WAITING_MD
    extraction_json = manifest.get("extraction_json")
    if extraction_json and Path(extraction_json).exists():
        try:
            payload = json.loads(Path(extraction_json).read_text(encoding="utf-8"))
            extraction_md = format_extraction_markdown(
                entities=payload.get("entities") or {},
                documents=payload.get("documents") or [],
                inconsistency_flags=payload.get("inconsistency_flags") or [],
                missing_documents=payload.get("missing_documents") or [],
                ubo_status=payload.get("ubo_status"),
            )
        except (OSError, json.JSONDecodeError):
            pass

    cra: dict[str, Any] = {}
    cra_json = manifest.get("cra_json")
    if cra_json and Path(cra_json).exists():
        try:
            cra = json.loads(Path(cra_json).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cra = {}

    summary_parts = [f"**{recommendation}**"]
    if rationale:
        summary_parts.extend(["", rationale])

    return RunView(
        summary="\n".join(summary_parts),
        extraction_md=extraction_md,
        evidence_md="_Evidence not stored for this run — re-run to refresh citations._",
        cra=cra,
        recommendation_md=_recommendation_markdown(recommendation, rationale),
        report_html=report_html,
        select_edd_tab=has_pdf,
    )


def _empty_run_view() -> RunView:
    return RunView(cra=dict(EMPTY_CRA))


def _load_run_by_index(index: int | None) -> tuple[Any, ...]:
    if index is None:
        return (*_empty_run_view().as_tuple(), None)
    run_dir = _run_dir_for_index(index)
    if not run_dir:
        return (*_empty_run_view().as_tuple(), None)
    return (*_outputs_from_run_dir(run_dir).as_tuple(), index)


def _delete_run_by_index(index: int | None) -> tuple[Any, ...]:
    run_dir = _run_dir_for_index(index)
    if run_dir:
        _delete_run_dir(run_dir)
    return (_refresh_runs_dataset(), *_empty_run_view().as_tuple(), None)


def _clear_run_view() -> tuple[Any, ...]:
    return (*_empty_run_view().as_tuple(), None)


def _normalize_upload_paths(files: list[str] | None) -> list[str]:
    paths: list[str] = []
    for item in files or []:
        if isinstance(item, str) and item:
            paths.append(item)
        elif isinstance(item, dict):
            path = item.get("path") or item.get("name")
            if path:
                paths.append(str(path))
    return paths


def _infer_pack(files: list[str]) -> tuple[str, str]:
    if not files:
        raise ValueError(
            "Attach at least one onboarding file "
            "(`.md`, `.txt`, `.pdf`, `.png`, `.jpg`) before running."
        )
    return "upload", str(materialize_upload_dir(files))


def _format_trace_block(trace: dict[str, Any]) -> str:
    query = trace.get("query") or "search task"
    lines = [f"**Query:** `{query}`"]
    meta: list[str] = []
    n = trace.get("result_count")
    ms = trace.get("duration_ms")
    if n is not None:
        meta.append(f"{n} sources")
    if ms is not None:
        meta.append(f"{ms} ms")
    if meta:
        lines.append(f"_{', '.join(meta)}_")

    err = trace.get("error")
    if err:
        lines.append(f"**Error:** {err}")
        return "\n".join(lines)

    for src in trace.get("sources") or []:
        url = src.get("url") or ""
        title = src.get("title") or url
        if url:
            lines.append(f"- [{title}]({url})")

    answer = trace.get("answer")
    if answer:
        preview = answer[:800] + ("…" if len(answer) > 800 else "")
        lines.extend(["", preview])
    return "\n".join(lines)


def _format_citations(evidence: list[dict[str, Any]]) -> str:
    if not evidence:
        return "_No public sources collected._"

    lines: list[str] = []
    for item in evidence:
        url = item.get("url") or ""
        claim = (item.get("claim") or "").strip()
        if not url and not claim:
            continue
        source = item.get("source") or item.get("category") or "source"
        category = item.get("category") or ""
        adverse = item.get("adverse")
        flags = []
        if category:
            flags.append(category)
        if adverse:
            flags.append("adverse")
        flag_s = f" ({', '.join(flags)})" if flags else ""
        if url:
            lines.append(f"- [{source}]({url}){flag_s}: {claim}")
        else:
            lines.append(f"- **{source}**{flag_s}: {claim}")
    return "\n".join(lines) if lines else "_No cited URLs in evidence._"


def _recommendation_markdown(recommendation: str, rationale: str) -> str:
    parts = [f"## {recommendation}"]
    if rationale:
        parts.extend(["", rationale])
    return "\n".join(parts)


def _cra_markdown_block(cra: dict[str, Any]) -> str:
    if not cra:
        return ""
    level = cra.get("risk_level") or "n/a"
    score = cra.get("weighted_score")
    score_s = f" · score {score}" if score is not None else ""
    return "\n".join(
        [
            f"**{level}**{score_s} · matrix `{cra.get('matrix_version') or 'n/a'}`",
            "",
            "```json",
            json.dumps(cra, indent=2, default=str),
            "```",
        ]
    )


def _safe_stem(client_name: str | None) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (client_name or "edd"))
    return safe.strip("_")[:48] or "edd"


@dataclass
class _Step:
    node: str
    label: str
    index: int
    details: list[str] = field(default_factory=list)
    live: str = ""
    done: bool = False

    @property
    def step_id(self) -> str:
        return f"step-{self.index}-{self.node}"

    def content(self) -> str:
        parts = list(self.details)
        if self.live:
            parts.append(self.live)
        if parts:
            return "\n\n".join(parts)
        return "_Completed._" if self.done else "_In progress…_"

    def to_message(self) -> ChatMessage:
        icon = "✅" if self.done else "🔄"
        return ChatMessage(
            role="assistant",
            content=self.content(),
            metadata={
                "title": f"{self.index + 1}. {icon} {self.label}",
                "id": self.step_id,
                "status": "done" if self.done else "pending",
            },
        )


class _InvestigationUI:
    def __init__(self) -> None:
        self.steps: list[_Step] = []
        self.extraction_md = WAITING_MD
        self.evidence_md = WAITING_MD
        self.cra: dict[str, Any] = dict(EMPTY_CRA)
        self.recommendation = ""
        self.rationale = ""
        self.done = False
        self.error: str | None = None
        self.report_html = ""
        self.state: dict[str, Any] = {}

    def step_messages(self, *, starting: bool = False) -> list[ChatMessage]:
        if not self.steps:
            if starting:
                return [
                    ChatMessage(
                        role="assistant",
                        content="_Starting investigation…_",
                        metadata={
                            "title": "Investigation",
                            "id": "start",
                            "status": "pending",
                        },
                    )
                ]
            return []
        return [step.to_message() for step in self.steps]

    def summary_markdown(self) -> str:
        if self.error:
            return f"**Error:** {self.error}"
        if self.done and self.recommendation:
            parts = [f"**{self.recommendation}**"]
            if self.rationale:
                parts.extend(["", self.rationale])
            return "\n".join(parts)
        if self.steps:
            return "Investigation in progress…"
        return ""

    def recommendation_markdown(self) -> str:
        if not self.recommendation:
            return WAITING_MD
        return _recommendation_markdown(self.recommendation, self.rationale)

    def to_run_view(self, *, pdf_path: str | None = None, select_edd_tab: bool = False) -> RunView:
        if pdf_path:
            self.report_html = _pdf_html(pdf_path)
            select_edd_tab = True
        return RunView(
            step_chat=self.step_messages(starting=not self.steps),
            summary=self.summary_markdown(),
            extraction_md=self.extraction_md,
            evidence_md=self.evidence_md,
            cra=self.cra,
            recommendation_md=self.recommendation_markdown(),
            report_html=self.report_html,
            select_edd_tab=select_edd_tab,
        )

    def on_task_start(self, node: str) -> None:
        label = NODE_LABELS.get(node, node.replace("_", " "))
        repeat = sum(1 for step in self.steps if step.node == node)
        if repeat:
            label = f"{label} (round {repeat + 1})"
        self.steps.append(_Step(node=node, label=label, index=len(self.steps)))

    def _latest(self, node: str) -> _Step | None:
        for step in reversed(self.steps):
            if step.node == node:
                return step
        return None

    def on_progress(self, node: str, message: str) -> None:
        step = self._latest(node)
        if step is None or step.done or not message.strip():
            return
        step.live = message.strip()

    def on_detail(self, node: str, message: str) -> None:
        text = message.strip()
        if not text:
            return
        step = self._latest(node)
        if step is None:
            return
        if step.details and step.details[-1] == text:
            return
        step.details.append(text)
        step.live = ""

    def on_task_complete(self, node: str, result: dict[str, Any]) -> None:
        step = self._latest(node)
        if step is not None and not step.done:
            step.done = True
            step.live = ""

        if node == "cross_verify" and result.get("entities"):
            self.extraction_md = format_extraction_markdown(
                entities=result.get("entities") or {},
                documents=self.state.get("documents") or [],
                inconsistency_flags=result.get("inconsistency_flags")
                or self.state.get("inconsistency_flags")
                or [],
                missing_documents=self.state.get("missing_documents") or [],
                ubo_status=result.get("ubo_status") or self.state.get("ubo_status"),
            )
            self.on_detail(node, self.extraction_md)

        if node == "missing_docs":
            missing = result.get("missing_documents")
            if missing is not None:
                self.extraction_md = format_extraction_markdown(
                    entities=self.state.get("entities") or {},
                    documents=self.state.get("documents") or [],
                    inconsistency_flags=self.state.get("inconsistency_flags") or [],
                    missing_documents=missing,
                    ubo_status=self.state.get("ubo_status"),
                )
                if missing:
                    missing_lines = "\n".join(f"- {item}" for item in missing)
                    self.on_detail(node, f"**Missing documents**\n{missing_lines}")

        if node == "evaluate_coverage":
            merged = {**self.state, **result}
            route = should_continue(merged)
            coverage = merged.get("coverage") or {}
            if route == "assess_risk":
                msg = f"Public-domain coverage complete — routing to {route}"
            else:
                msg = (
                    f"Coverage gaps remain — routing to {route} "
                    f"(round {merged.get('round')}/{merged.get('max_rounds')})"
                )
            self.on_detail(node, msg)
            if coverage.get("percent") is not None:
                self.on_detail(
                    node,
                    f"Coverage {coverage.get('percent')}% — "
                    f"{'sufficient' if coverage.get('sufficient') else 'gaps remain'}",
                )

        if node == "cra_assessment" and result.get("cra"):
            self.cra = dict(result["cra"])
            self.on_detail(node, _cra_markdown_block(self.cra))

        if result.get("recommendation"):
            self.recommendation = str(result.get("recommendation") or self.recommendation)
            self.rationale = str(
                result.get("recommendation_rationale") or self.rationale or ""
            )
            self.on_detail(
                node,
                f"**{self.recommendation}** — {self.rationale or 'No rationale provided.'}",
            )


def run_case(
    files: list[str] | None,
) -> Generator[tuple[Any, ...], None, None]:
    file_paths = _normalize_upload_paths(files)
    if not file_paths:
        return

    try:
        pack_id, docs_dir = _infer_pack(file_paths)
    except ValueError as exc:
        view = RunView(summary=str(exc))
        yield view.as_tuple()
        return

    ui = _InvestigationUI()
    yield ui.to_run_view().as_tuple()

    try:
        for mode, chunk in stream_investigation(
            pack_id=pack_id,
            docs_dir=docs_dir,
            max_rounds=DEFAULT_MAX_ROUNDS,
        ):
            if mode == "values" and isinstance(chunk, dict):
                ui.state = chunk
                continue

            if mode == "tasks" and isinstance(chunk, dict):
                node = str(chunk.get("name") or "")
                if not node:
                    continue
                if "result" not in chunk:
                    ui.on_task_start(node)
                else:
                    error = chunk.get("error")
                    if error is not None:
                        ui.error = str(error)
                        yield ui.to_run_view().as_tuple()
                        return
                    result = chunk.get("result") or {}
                    if isinstance(result, dict):
                        ui.on_task_complete(node, result)
                yield ui.to_run_view().as_tuple()
                continue

            if mode == "custom" and isinstance(chunk, dict):
                node = str(chunk.get("node") or "")
                message = str(chunk.get("message") or "")
                kind = str(chunk.get("kind") or "detail")
                if kind == "progress":
                    ui.on_progress(node, message)
                else:
                    trace = chunk.get("trace")
                    if isinstance(trace, dict):
                        ui.on_detail(node, _format_trace_block(trace))
                    else:
                        ui.on_detail(node, message)
                yield ui.to_run_view().as_tuple()
    except Exception as exc:  # noqa: BLE001
        ui.error = str(exc)
        yield ui.to_run_view().as_tuple()
        return

    serialized = serialize_result(ui.state)
    report = serialized.get("report") or {}
    cra = serialized.get("cra") or {}
    ui.extraction_md = serialized.get("extraction_markdown") or format_extraction_markdown(
        entities=serialized.get("entities") or {},
        documents=serialized.get("documents") or [],
        inconsistency_flags=serialized.get("inconsistency_flags") or [],
        missing_documents=serialized.get("missing_documents") or [],
        ubo_status=serialized.get("ubo_status"),
    )
    ui.evidence_md = _format_citations(serialized.get("evidence") or [])
    ui.cra = dict(cra)
    ui.recommendation = serialized.get("recommendation") or "Review"
    ui.rationale = serialized.get("recommendation_rationale") or ""
    ui.done = True

    client_name = (report.get("client") or {}).get("legal_name")
    stem = _safe_stem(client_name)
    final_view = ui.to_run_view()
    pdf_path, _extraction_json, _cra_json, _artifact_dir = _persist_run(
        stem=stem,
        serialized=serialized,
        report=report,
        cra=cra,
        snapshot=final_view.to_snapshot(),
    )
    yield ui.to_run_view(pdf_path=pdf_path).as_tuple()


def build_ui() -> gr.Blocks:
    with gr.Blocks(fill_width=True, title="Tenant Onboarding KYC Agent") as demo:
        gr.Markdown(
            "# Tenant Onboarding KYC Agent\n\n"
            "Upload onboarding documents to start a public-domain investigation. "
            "Progress streams below; review extraction, evidence, CRA, and the EDD report in the tabs."
        )

        with gr.Sidebar(label="Past runs", open=True):
            new_run_btn = gr.Button("New run", variant="primary")
            refresh_runs_btn = gr.Button("Refresh list")
            delete_run_btn = gr.Button("Delete selected run", variant="stop")
            selected_run_index = gr.State(value=None)
            samples, labels = _run_dataset_data()
            runs_dataset = gr.Dataset(
                components=[gr.Textbox(visible=False)],
                samples=samples,
                sample_labels=labels,
                show_label=False,
                layout="table",
                type="index",
            )

        with gr.Row(elem_id="upload-row"):
            upload_files = gr.File(
                label="Onboarding documents",
                file_count="multiple",
                file_types=FILE_TYPES,
                type="filepath",
                height=120,
            )

        summary_md = gr.Markdown("")

        step_chatbot = gr.Chatbot(
            label="Investigation steps",
            render_markdown=True,
            height=480,
            elem_id="step-chatbot",
        )

        with gr.Tabs() as results_tabs:
            with gr.Tab("Extraction", id="extraction"):
                extraction_md = gr.Markdown(WAITING_MD)
            with gr.Tab("Evidence", id="evidence"):
                evidence_md = gr.Markdown(WAITING_MD)
            with gr.Tab("CRA", id="cra"):
                cra_json = gr.JSON(label="CRA assessment", value=EMPTY_CRA)
            with gr.Tab("Recommendation", id="recommendation"):
                recommendation_md = gr.Markdown(WAITING_MD)
            with gr.Tab("EDD report", id=TAB_EDD):
                report_html = gr.HTML(
                    "<p><em>EDD PDF will appear here when the run completes.</em></p>"
                )

        run_outputs = [
            step_chatbot,
            summary_md,
            extraction_md,
            evidence_md,
            cra_json,
            recommendation_md,
            report_html,
            results_tabs,
        ]

        upload_files.upload(
            run_case,
            inputs=[upload_files],
            outputs=run_outputs,
        )

        new_run_btn.click(
            _clear_run_view,
            None,
            [*run_outputs, selected_run_index],
            queue=False,
        )
        refresh_runs_btn.click(_refresh_runs_dataset, None, runs_dataset, queue=False)
        runs_dataset.click(
            _load_run_by_index,
            runs_dataset,
            [*run_outputs, selected_run_index],
            queue=False,
        )
        delete_run_btn.click(
            _delete_run_by_index,
            selected_run_index,
            [runs_dataset, *run_outputs, selected_run_index],
            queue=False,
        )
        demo.load(_refresh_runs_dataset, None, runs_dataset, queue=False)

    return demo


def launch_app(*, host: str = "127.0.0.1", port: int = 7860, share: bool = False) -> None:
    demo = build_ui()
    demo.queue(default_concurrency_limit=3)
    demo.launch(server_name=host, server_port=port, share=share, css=STEPS_CSS)
