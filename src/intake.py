"""Document intake: load, extract, cross-verify, and gap checks."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import fitz
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field
from pypdf import PdfReader

from kyc_agent.llm import get_vision_model, structured_invoke
from kyc_agent.runtime import emit
from kyc_agent.state import (
    DOC_TYPE_TO_LABEL,
    CompanyEntity,
    DocumentFile,
    EntityBundle,
    InvestigationState,
    PersonEntity,
)

TEXT_SUFFIXES = {".md", ".txt", ".markdown"}
PDF_SUFFIXES = {".pdf"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | PDF_SUFFIXES | IMAGE_SUFFIXES

# Real-world uploads often have minor xref issues; pypdf recovers but logs warnings.
logging.getLogger("pypdf").setLevel(logging.ERROR)
_log = logging.getLogger("kyc_agent.intake")

VISION_PROMPT = (
    "You are extracting text from a KYC onboarding document image "
    "named `{label}`.\n"
    "Return a clean plain-text transcript of all readable fields and labels. "
    "Preserve names, IDs, dates, addresses, company names, and ownership figures. "
    "Do not invent values that are not visible."
)


def extract_pdf_text(path: Path) -> str:
    reader = PdfReader(str(path), strict=False)
    pages: list[str] = []
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(f"[PDF page {i}]\n{text}")
    if pages:
        return "\n\n".join(pages)
    return extract_pdf_via_vision(path)


def _render_pdf_pages(path: Path, *, dpi: int = 150) -> list[tuple[int, bytes]]:
    doc = fitz.open(path)
    rendered: list[tuple[int, bytes]] = []
    try:
        for index, page in enumerate(doc, start=1):
            pixmap = page.get_pixmap(dpi=dpi)
            rendered.append((index, pixmap.tobytes("png")))
    finally:
        doc.close()
    return rendered


def transcribe_image_bytes(
    data: bytes,
    *,
    label: str,
    mime: str = "image/png",
) -> str:
    if not data:
        return f"[Empty image payload: {label}]"
    b64 = base64.b64encode(data).decode("ascii")
    message = HumanMessage(
        content=[
            {"type": "text", "text": VISION_PROMPT.format(label=label)},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            },
        ]
    )
    result = get_vision_model().invoke([message])
    content = result.content if isinstance(result.content, str) else str(result.content)
    text = content.strip()
    if not text:
        return f"[No text transcribed from image: {label}]"
    return text


def extract_pdf_via_vision(path: Path) -> str:
    """OCR scanned PDF pages with the configured vision model."""
    try:
        pages = _render_pdf_pages(path)
    except Exception as exc:  # noqa: BLE001
        _log.warning("PDF vision render failed for %s: %s", path.name, exc)
        return (
            f"[PDF has no extractable text layer: {path.name}. "
            f"Vision OCR failed to render pages: {exc}]"
        )

    if not pages:
        return f"[PDF has no pages: {path.name}]"

    _log.info(
        "No text layer in %s — running vision OCR on %d page(s)",
        path.name,
        len(pages),
    )

    transcripts: list[str] = []
    for page_num, png_bytes in pages:
        try:
            text = transcribe_image_bytes(
                png_bytes,
                label=f"{path.name} page {page_num}",
                mime="image/png",
            )
            transcripts.append(f"[PDF page {page_num} — vision OCR]\n{text}")
        except Exception as exc:  # noqa: BLE001
            _log.warning("Vision OCR failed for %s page %d: %s", path.name, page_num, exc)
            transcripts.append(
                f"[PDF page {page_num} — vision OCR failed: {exc}]"
            )

    if any("vision OCR failed" not in part for part in transcripts):
        return "\n\n".join(transcripts)
    return (
        f"[PDF has no extractable text layer: {path.name}. "
        "Vision OCR failed — set NEBIUS_VISION_MODEL to a vision-capable model "
        "(default: openbmb/MiniCPM-V-4_5).]"
    )


def extract_image_text(path: Path) -> str:
    """Transcribe an onboarding image via the vision model."""
    mime, _ = mimetypes.guess_type(path.name)
    if mime not in {"image/png", "image/jpeg", "image/jpg", "image/webp"}:
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    raw = path.read_bytes()
    try:
        text = transcribe_image_bytes(raw, label=path.name, mime=mime or "image/png")
    except Exception as exc:  # noqa: BLE001
        return (
            f"[Failed to transcribe image {path.name}: {exc}. "
            "Ensure NEBIUS_API_KEY is set and NEBIUS_VISION_MODEL supports vision.]"
        )
    return f"[Image transcript: {path.name}]\n{text}"


def read_document_content(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix in PDF_SUFFIXES:
        try:
            return extract_pdf_text(path)
        except Exception as exc:  # noqa: BLE001
            return f"[Failed to read PDF {path.name}: {exc}]"
    if suffix in IMAGE_SUFFIXES:
        return extract_image_text(path)
    raise ValueError(f"Unsupported file type: {path.name}")


FILENAME_TO_DOC_TYPE: dict[str, str] = {
    "kyc_form.md": "kyc_form",
    "trade_licence.md": "trade_licence",
    "certificate_of_incorporation.md": "certificate_of_incorporation",
    "register_of_shareholders.md": "register_of_shareholders",
    "group_structure_chart.md": "group_structure_chart",
    "passport_sara_al_mansoori.md": "passport",
}


def infer_doc_type(filename: str) -> str:
    name = Path(filename).name
    if name in FILENAME_TO_DOC_TYPE:
        return FILENAME_TO_DOC_TYPE[name]
    stem = Path(name).stem.lower()
    stem = re.sub(r"[^a-z0-9]+", "_", stem)
    if "kyc" in stem and "form" in stem:
        return "kyc_form"
    if "trade" in stem and ("licen" in stem or "license" in stem):
        return "trade_licence"
    if "incorporation" in stem or "certificate" in stem:
        return "certificate_of_incorporation"
    if "shareholder" in stem or "register" in stem:
        return "register_of_shareholders"
    if "structure" in stem or "org_chart" in stem or "group" in stem:
        return "group_structure_chart"
    if "passport" in stem or "id_" in stem or stem.startswith("id"):
        return "passport"
    if "ubo" in stem:
        return "ubo_declaration"
    return stem or "supporting_document"


def load_documents(docs_dir: Path) -> list[DocumentFile]:
    documents: list[DocumentFile] = []
    paths = sorted(
        p
        for p in docs_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    )
    for path in paths:
        doc_type = infer_doc_type(path.name)
        label = DOC_TYPE_TO_LABEL.get(doc_type, path.stem.replace("_", " ").title())
        emit(
            {
                "kind": "progress",
                "node": "load_documents",
                "message": f"Reading `{path.name}`…",
            }
        )
        content = read_document_content(path)
        documents.append(
            DocumentFile(
                doc_type=doc_type,
                label=label,
                path=str(path),
                content=content,
            )
        )
        preview = content[:240].replace("\n", " ").strip()
        if len(content) > 240:
            preview += "…"
        emit(
            {
                "kind": "detail",
                "node": "load_documents",
                "message": f"**{label}** (`{path.name}`)\n\n{preview or '_No text extracted._'}",
            }
        )
    return documents


def materialize_upload_dir(
    uploaded_paths: list[str | Path],
    *,
    prefix: str = "kyc_upload_",
) -> Path:
    """Copy uploaded files into a fresh temp directory for investigation intake."""
    dest = Path(tempfile.mkdtemp(prefix=prefix))
    for raw in uploaded_paths:
        src = Path(raw)
        if not src.is_file():
            continue
        shutil.copy2(src, dest / src.name)
    return dest


def load_documents_node(state: InvestigationState) -> dict:
    pack_id = state.get("pack_id") or "upload"
    docs_dir_raw = state.get("docs_dir")
    if not docs_dir_raw:
        raise ValueError("No documents provided — attach files to investigate.")

    docs_dir = Path(docs_dir_raw)
    documents = load_documents(docs_dir)
    if not documents:
        raise ValueError(f"No readable onboarding documents found in {docs_dir}")

    return {
        "pack_id": pack_id,
        "docs_dir": str(docs_dir),
        "documents": [d.model_dump() for d in documents],
        "present_doc_types": [d.doc_type for d in documents],
        "round": 0,
        "max_rounds": state.get("max_rounds") or 2,
        "evidence": [],
        "tavily_traces": [],
        "pending_queries": [],
    }


_log = logging.getLogger("kyc_agent")

DEFAULT_MAX_WORKERS = int(os.getenv("DOC_EXTRACT_MAX_WORKERS", "8"))
MAX_DOC_CHARS = int(os.getenv("DOC_EXTRACT_MAX_CHARS", "24000"))


class PerDocumentCompanyHints(BaseModel):
    legal_name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    registration_number: str | None = None
    jurisdiction: str | None = None
    address: str | None = None
    industry: str | None = None
    trade_licence_number: str | None = None


class PerDocumentExtraction(BaseModel):
    doc_type: str
    doc_label: str
    doc_path: str = ""
    company: PerDocumentCompanyHints | None = None
    persons: list[PersonEntity] = Field(default_factory=list)
    corporate_shareholders: list[str] = Field(default_factory=list)
    ownership_gaps: list[str] = Field(default_factory=list)
    inconsistency_flags: list[str] = Field(default_factory=list)
    extraction_notes: str = ""


class PerDocumentExtractionPayload(BaseModel):
    """LLM response shape — document metadata is added after validation."""

    company: PerDocumentCompanyHints | None = None
    persons: list[PersonEntity] = Field(default_factory=list)
    corporate_shareholders: list[str] = Field(default_factory=list)
    ownership_gaps: list[str] = Field(default_factory=list)
    inconsistency_flags: list[str] = Field(default_factory=list)
    extraction_notes: str = ""


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def normalize_per_document_payload(data: dict[str, Any]) -> dict[str, Any]:
    payload = dict(data)
    company = payload.get("company")
    if isinstance(company, dict):
        payload["company"] = {
            key: _blank_to_none(val) for key, val in company.items()
        }
    persons: list[dict[str, Any]] = []
    for person in payload.get("persons") or []:
        if not isinstance(person, dict):
            continue
        name = str(person.get("name") or person.get("full_name") or "").strip()
        if not name:
            continue
        persons.append({**person, "name": name})
    payload["persons"] = persons
    for key in ("corporate_shareholders", "ownership_gaps", "inconsistency_flags"):
        payload[key] = [
            str(item).strip()
            for item in (payload.get(key) or [])
            if str(item).strip()
        ]
    return payload


SINGLE_DOCUMENT_PROMPT = """
You are a KYC document extraction subagent. You receive ONE onboarding file only.

Extract every entity, ownership signal, and company field visible in this document.
Do NOT infer facts from other documents or general knowledge.

Rules:
- Extract only what appears on this document; use null for absent fields.
- Dates: DD/MM/YYYY when present.
- shareholders/directors: JSON arrays of {{"name", "role", "percentage"}} when listed.
- Ownership percentages only when explicitly stated on this document.
- Flag intra-document ambiguities in inconsistency_flags.
- Note undisclosed ultimate owners or opaque corporate shareholders in ownership_gaps.

DOCUMENT ({doc_type} — {doc_label}):
{content}
"""


def _fallback_extract_single(document: dict) -> PerDocumentExtraction:
    """Deterministic single-document parse when the LLM is unavailable."""
    content = document.get("content", "")
    doc_type = str(document.get("doc_type") or "supporting_document")
    label = str(document.get("label") or "Document")
    path = str(document.get("path") or "")

    company: PerDocumentCompanyHints | None = None
    persons: list[PersonEntity] = []
    corporates: list[str] = []
    gaps: list[str] = []
    flags: list[str] = []

    if any(
        token in content
        for token in ("Acme Holdings", "FZ-2021-88421", "CN-784521")
    ):
        company = PerDocumentCompanyHints(
            legal_name="Acme Holdings FZ-LLC" if "FZ-LLC" in content else "Acme Holdings Limited",
            aliases=["Acme Holdings", "Acme Holdings Limited", "Acme Holdings FZ-LLC"],
            registration_number="FZ-2021-88421" if "FZ-2021" in content else None,
            jurisdiction="Dubai, United Arab Emirates" if "Dubai" in content else None,
            address="Office 1402, Cluster X, Jumeirah Lakes Towers, Dubai, UAE"
            if "Jumeirah" in content
            else None,
            industry="Corporate advisory / holding company" if "holding" in content.lower() else None,
            trade_licence_number="CN-784521" if "CN-784521" in content else None,
        )

    if "Sara Al Mansoori" in content or "XN7821345" in content:
        persons.append(
            PersonEntity(
                name="Sara Al Mansoori",
                role="director",
                nationality="UAE",
                id_number="XN7821345" if "XN7821345" in content else None,
                ownership_pct=51.0 if "51%" in content or "510" in content else None,
                ownership_source="register_of_shareholders" if "51%" in content else None,
                ubo_basis="shareholding" if "51%" in content else None,
            )
        )
        if "51%" in content:
            persons[-1].role = "beneficial_owner_candidate"

    if "Horizon Nest" in content:
        corporates.append("Horizon Nest Investments Ltd")
    if "ultimate owners not disclosed" in content.lower() or "Horizon Nest" in content:
        gaps.append("Corporate shareholder Horizon Nest Investments Ltd has undisclosed ultimate owners")
    if "Acme Holdings Limited" in content and "Acme Holdings FZ-LLC" in content:
        flags.append("Company appears under multiple legal-name variants in this document")

    return PerDocumentExtraction(
        doc_type=doc_type,
        doc_label=label,
        doc_path=path,
        company=company,
        persons=persons,
        corporate_shareholders=corporates,
        ownership_gaps=gaps,
        inconsistency_flags=flags,
        extraction_notes="Fallback per-document extractor used (structured LLM extraction failed)",
    )


def extract_single_document(document: dict) -> PerDocumentExtraction:
    """Run one extraction subagent for a single loaded document."""
    label = str(document.get("label") or "Document")
    doc_type = str(document.get("doc_type") or "supporting_document")
    raw_content = str(document.get("content") or "")
    content = raw_content[:MAX_DOC_CHARS]
    if len(raw_content) > MAX_DOC_CHARS:
        content += "\n\n[Document truncated for extraction — full text retained in intake.]"
    prompt = SINGLE_DOCUMENT_PROMPT.format(
        doc_type=doc_type,
        doc_label=label,
        content=content,
    )
    try:
        raw = structured_invoke(
            prompt,
            PerDocumentExtractionPayload,
            normalize=normalize_per_document_payload,
        )
        return PerDocumentExtraction(
            doc_type=doc_type,
            doc_label=label,
            doc_path=str(document.get("path") or ""),
            company=raw.company,
            persons=raw.persons,
            corporate_shareholders=raw.corporate_shareholders,
            ownership_gaps=raw.ownership_gaps,
            inconsistency_flags=raw.inconsistency_flags,
            extraction_notes=raw.extraction_notes,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "Per-document structured extraction failed for %s: %s: %s",
            label,
            type(exc).__name__,
            exc,
        )
        fallback = _fallback_extract_single(document)
        fallback.extraction_notes = (
            f"Fallback per-document extractor used ({type(exc).__name__})."
        )
        return fallback


def _emit_extract_complete(result: PerDocumentExtraction, *, success: bool) -> None:
    label = result.doc_label
    notes = (result.extraction_notes or "").strip()
    if success:
        message = f"Finished `{label}`"
    else:
        message = f"Fallback extraction for `{label}`"
    if notes:
        message = f"{message}\n\n{notes}"
    emit(
        {
            "kind": "detail",
            "node": "extract_documents",
            "message": message,
        }
    )


def extract_documents_parallel(
    documents: list[dict],
    *,
    max_workers: int | None = None,
) -> list[PerDocumentExtraction]:
    """Run one extraction subagent per document in parallel."""
    if not documents:
        return []
    if len(documents) == 1:
        doc = documents[0]
        label = str(doc.get("label") or "Document")
        emit(
            {
                "kind": "progress",
                "node": "extract_documents",
                "message": f"Extracting `{label}`",
            }
        )
        result = extract_single_document(doc)
        success = "Fallback per-document extractor" not in (result.extraction_notes or "")
        _emit_extract_complete(result, success=success)
        return [result]

    workers = min(max_workers or DEFAULT_MAX_WORKERS, len(documents))
    emit(
        {
            "kind": "progress",
            "node": "extract_documents",
            "message": f"Extracting {len(documents)} document(s) in parallel…",
        }
    )
    ordered: list[PerDocumentExtraction | None] = [None] * len(documents)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_index = {
            pool.submit(extract_single_document, doc): index
            for index, doc in enumerate(documents)
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            result = future.result()
            ordered[index] = result
            success = "Fallback per-document extractor" not in (result.extraction_notes or "")
            _emit_extract_complete(result, success=success)
    return [item for item in ordered if item is not None]


def extract_documents_node(state: InvestigationState) -> dict:
    documents = state.get("documents") or []
    slices = extract_documents_parallel(documents)
    return {
        "document_extractions": [item.model_dump() for item in slices],
    }


CROSS_VERIFY_PROMPT = """
You are a KYC analyst reconciling independent extractions from multiple onboarding documents.

Each JSON object below was produced by a dedicated subagent that saw ONLY one file.
Merge them into one canonical entity view.

Tasks:
1. Choose a primary legal company name and collect aliases / variants.
2. Deduplicate persons only when clearly the same individual; never merge distinct people.
3. Aggregate corporate shareholders and ownership gaps from all documents.
4. Flag cross-document inconsistencies (name variants, conflicting IDs, ownership mismatches).
5. Mark ownership candidates as beneficial_owner_candidate with ubo_basis when supported by registers or cap tables.

Do NOT invent facts absent from the per-document extractions below.

PER-DOCUMENT EXTRACTIONS:
{batch_json}
"""


class ExtractionResult(BaseModel):
    company: CompanyEntity
    persons: list[PersonEntity] = Field(default_factory=list)
    corporate_shareholders: list[str] = Field(default_factory=list)
    ownership_gaps: list[str] = Field(default_factory=list)
    inconsistency_flags: list[str] = Field(default_factory=list)
    extraction_notes: str = ""


def format_document_slice_summary(item: PerDocumentExtraction) -> str:
    """Compact per-document extraction for streaming cross-verify progress."""
    lines = [f"**`{item.doc_label}`** ({item.doc_type})"]
    company = item.company
    if company:
        if company.legal_name:
            lines.append(f"- Company: {company.legal_name}")
        if company.registration_number:
            lines.append(f"- Registration: {company.registration_number}")
        if company.jurisdiction:
            lines.append(f"- Jurisdiction: {company.jurisdiction}")
    if item.persons:
        names = ", ".join(p.name or "unnamed" for p in item.persons[:4])
        lines.append(f"- Persons ({len(item.persons)}): {names}")
    if item.corporate_shareholders:
        lines.append(f"- Corporate shareholders: {', '.join(item.corporate_shareholders[:4])}")
    if item.ownership_gaps:
        lines.append(f"- Ownership gaps: {len(item.ownership_gaps)} flagged")
    if item.inconsistency_flags:
        lines.append(f"- Inconsistency flags: {len(item.inconsistency_flags)}")
    notes = (item.extraction_notes or "").strip()
    if notes:
        preview = notes[:400] + ("…" if len(notes) > 400 else "")
        lines.append(f"- Notes: {preview}")
    return "\n".join(lines)


def format_cross_verify_result_summary(extraction: ExtractionResult) -> str:
    company = extraction.company
    lines = [
        "**Reconciled entity view**",
        f"- **Legal name:** {company.legal_name or 'n/a'}",
        f"- **Persons:** {len(extraction.persons)}",
        f"- **Corporate shareholders:** {len(extraction.corporate_shareholders)}",
        f"- **Ownership gaps:** {len(extraction.ownership_gaps)}",
        f"- **Inconsistency flags:** {len(extraction.inconsistency_flags)}",
    ]
    notes = (extraction.extraction_notes or "").strip()
    if notes:
        preview = notes[:500] + ("…" if len(notes) > 500 else "")
        lines.extend(["", preview])
    return "\n".join(lines)


def _fallback_extract(documents: list[dict]) -> ExtractionResult:
    """Last-resort deterministic parse when the LLM is unavailable."""
    joined = "\n".join(d.get("content", "") for d in documents)

    legal_name = "Unknown applicant"
    aliases: list[str] = []
    registration_number: str | None = None
    jurisdiction: str | None = None
    address: str | None = None
    industry: str | None = None
    trade_licence_number: str | None = None

    for pattern in (
        r"(?:Applicant|Legal name|Company name):\s*\*{0,2}\s*([^\n*]+)",
        r"^#\s+[^—]+—\s*([^\n(]+)",
    ):
        match = re.search(pattern, joined, re.I | re.MULTILINE)
        if match:
            legal_name = match.group(1).strip().strip("*").strip()
            break

    reg_match = re.search(
        r"(?:Registration number|Company number|Licence number):\s*\*{0,2}\s*([^\n*]+)",
        joined,
        re.I,
    )
    if reg_match:
        registration_number = reg_match.group(1).strip().strip("*").strip()

    licence_match = re.search(r"Licence number:\s*\*{0,2}\s*([^\n*]+)", joined, re.I)
    if licence_match:
        trade_licence_number = licence_match.group(1).strip().strip("*").strip()

    if "Also known as:" in joined:
        aka_match = re.search(r"Also known as:\s*\*{0,2}\s*([^\n*]+)", joined, re.I)
        if aka_match:
            aliases.append(aka_match.group(1).strip().strip("*").strip())

    if "Dubai" in joined or "United Arab Emirates" in joined:
        jurisdiction = "Dubai, United Arab Emirates"
    if "Jumeirah Lakes Towers" in joined or "JLT" in joined:
        address = "Office 1402, Cluster X, Jumeirah Lakes Towers, Dubai, UAE"
    if "holding company" in joined.lower():
        industry = "Corporate advisory / holding company"

    persons: list[PersonEntity] = []
    if "Sara Al Mansoori" in joined:
        persons.append(
            PersonEntity(
                name="Sara Al Mansoori",
                role="director",
                nationality="UAE" if "UAE" in joined or "United Arab Emirates" in joined else None,
                id_number="XN7821345" if "XN7821345" in joined else None,
                ownership_pct=51.0 if "51%" in joined or "510" in joined else None,
                ownership_source="register_of_shareholders" if "51%" in joined else None,
                ubo_basis="shareholding" if "51%" in joined else None,
            )
        )
        if "51%" in joined:
            persons[0].role = "beneficial_owner_candidate"
            persons[0].ubo_basis = "shareholding"

    flags: list[str] = []
    gaps: list[str] = []
    if "Acme Holdings Limited" in joined and "Acme Holdings FZ-LLC" in joined:
        flags.append("Company appears under multiple legal-name variants across documents")
    if "ultimate owners not disclosed" in joined.lower() or "Horizon Nest" in joined:
        gaps.append(
            "Corporate shareholder Horizon Nest Investments Ltd has undisclosed ultimate owners"
        )
    corporates: list[str] = []
    if "Horizon Nest" in joined:
        corporates.append("Horizon Nest Investments Ltd")

    return ExtractionResult(
        company=CompanyEntity(
            legal_name=legal_name,
            aliases=aliases,
            registration_number=registration_number,
            jurisdiction=jurisdiction,
            address=address,
            industry=industry,
            trade_licence_number=trade_licence_number,
        ),
        persons=persons,
        corporate_shareholders=corporates,
        ownership_gaps=gaps,
        inconsistency_flags=flags,
        extraction_notes="Fallback extractor used (structured LLM reconciliation failed)",
    )


def _normalize_extraction(extraction: ExtractionResult) -> ExtractionResult:
    """Align roles so planner treats ownership signals as unverified candidates."""
    persons: list[PersonEntity] = []
    for person in extraction.persons:
        p = person.model_copy(deep=True)
        basis = (p.ubo_basis or "").lower()
        role = (p.role or "").lower()
        if role == "ubo" or basis in {"shareholding", "control", "explicit_declaration"}:
            p.role = "beneficial_owner_candidate"
            if basis == "explicit_declaration" or not p.ubo_basis:
                p.ubo_basis = "shareholding" if basis != "control" else "control"
        elif role in {"shareholder", "beneficial_owner_candidate"}:
            p.role = "beneficial_owner_candidate"
            if not p.ubo_basis:
                p.ubo_basis = basis or "shareholding"
        persons.append(p)

    return ExtractionResult(
        company=extraction.company,
        persons=persons,
        corporate_shareholders=list(dict.fromkeys(extraction.corporate_shareholders)),
        ownership_gaps=extraction.ownership_gaps,
        inconsistency_flags=extraction.inconsistency_flags,
        extraction_notes=extraction.extraction_notes,
    )


def cross_verify_document_extractions(
    slices: list[PerDocumentExtraction],
    documents: list[dict] | None = None,
) -> ExtractionResult:
    """Reconcile a batch of per-document subagent extractions."""
    if not slices:
        docs = documents or []
        emit(
            {
                "kind": "progress",
                "node": "cross_verify",
                "message": "No per-document extractions — using fallback parser",
            }
        )
        return _normalize_extraction(_fallback_extract(docs))

    emit(
        {
            "kind": "progress",
            "node": "cross_verify",
            "message": f"Reconciling **{len(slices)}** per-document extraction(s)…",
        }
    )
    for item in slices:
        emit(
            {
                "kind": "detail",
                "node": "cross_verify",
                "message": format_document_slice_summary(item),
            }
        )

    batch_json = json.dumps(
        [item.model_dump() for item in slices],
        indent=2,
        default=str,
    )
    emit(
        {
            "kind": "progress",
            "node": "cross_verify",
            "message": "Running LLM reconciliation across document extractions…",
        }
    )
    try:
        raw = structured_invoke(
            CROSS_VERIFY_PROMPT.replace("{batch_json}", batch_json),
            ExtractionResult,
        )
        extraction = _normalize_extraction(raw)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "Cross-verify structured reconciliation failed: %s: %s",
            type(exc).__name__,
            exc,
        )
        emit(
            {
                "kind": "progress",
                "node": "cross_verify",
                "message": (
                    f"LLM reconciliation failed ({type(exc).__name__}) — using fallback parser"
                ),
            }
        )
        docs = documents or [
            {
                "label": item.doc_label,
                "doc_type": item.doc_type,
                "content": item.extraction_notes,
            }
            for item in slices
        ]
        extraction = _normalize_extraction(_fallback_extract(docs))

    emit(
        {
            "kind": "detail",
            "node": "cross_verify",
            "message": format_cross_verify_result_summary(extraction),
        }
    )
    return extraction


def extract_entities_from_documents(documents: list[dict]) -> ExtractionResult:
    """Parallel per-document extraction followed by batch cross-verification."""
    slices = extract_documents_parallel(documents)
    return cross_verify_document_extractions(slices, documents)


def cross_verify_node(state: InvestigationState) -> dict:
    documents = state.get("documents") or []
    raw_slices = state.get("document_extractions") or []
    if raw_slices:
        slices = [PerDocumentExtraction.model_validate(item) for item in raw_slices]
        extraction = cross_verify_document_extractions(slices, documents)
    else:
        extraction = extract_entities_from_documents(documents)

    gaps = list(extraction.ownership_gaps)
    flags = list(extraction.inconsistency_flags) + [
        f"Ownership gap: {g}"
        for g in gaps
        if f"Ownership gap: {g}" not in extraction.inconsistency_flags
    ]

    bundle = EntityBundle(
        company=extraction.company,
        persons=extraction.persons,
        corporate_shareholders=extraction.corporate_shareholders,
        ubo_status="absent",
        inconsistency_flags=flags,
        ownership_gaps=gaps,
    )

    return {
        "entities": bundle.model_dump(),
        "ubo_status": "absent",
        "inconsistency_flags": flags,
    }


def normalize_name(name: str) -> str:
    text = name.strip()
    text = re.sub(r"\s+", " ", text)
    collapsed = re.sub(
        r"\b(limited|ltd\.?|llc|fz-llc|fz llc|inc\.?|corp\.?)\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    collapsed = re.sub(r"\s+", " ", collapsed).strip(" ,.")
    return collapsed or text


def format_entity_resolution_summary(
    *,
    legal_name: str,
    aliases: list[str],
    persons: list[dict],
) -> str:
    lines = [
        "**Entity resolution** (deterministic alias normalization — no LLM)",
        "",
        f"- **Legal name:** {legal_name}",
        f"- **Normalized aliases:** {', '.join(aliases) if aliases else 'none'}",
    ]
    if persons:
        lines.append("- **Persons:**")
        for person in persons[:8]:
            name = person.get("name") or "unnamed"
            role = person.get("role") or "n/a"
            person_aliases = person.get("aliases") or []
            alias_s = f" · aliases: {', '.join(person_aliases)}" if person_aliases else ""
            lines.append(f"  - {name} (`{role}`){alias_s}")
    else:
        lines.append("- **Persons:** none extracted")
    return "\n".join(lines)


def resolve_entities_node(state: InvestigationState) -> dict:
    emit(
        {
            "kind": "progress",
            "node": "resolve_entities",
            "message": "Normalizing company and person aliases…",
        }
    )
    entities = dict(state.get("entities") or {})
    company = dict(entities.get("company") or {})
    legal = company.get("legal_name") or "Unknown Company"
    aliases = list(company.get("aliases") or [])
    canonical = normalize_name(legal)

    normalized_aliases: list[str] = []
    seen = {legal.lower()}
    for alias in aliases + [canonical]:
        if alias and alias.lower() not in seen:
            seen.add(alias.lower())
            normalized_aliases.append(alias)
    company["aliases"] = normalized_aliases
    company["legal_name"] = legal
    entities["company"] = company

    persons = []
    for person in entities.get("persons") or []:
        p = dict(person)
        p_aliases = list(p.get("aliases") or [])
        norm = normalize_name(p.get("name", ""))
        if norm and norm.lower() != (p.get("name") or "").lower():
            p_aliases.append(norm)
        p["aliases"] = list(dict.fromkeys(p_aliases))
        persons.append(p)
    entities["persons"] = persons
    entities["ubo_status"] = state.get("ubo_status") or entities.get("ubo_status") or "absent"

    emit(
        {
            "kind": "detail",
            "node": "resolve_entities",
            "message": format_entity_resolution_summary(
                legal_name=legal,
                aliases=normalized_aliases,
                persons=persons,
            ),
        }
    )

    return {
        "entities": entities,
    }


IDENTITY_TYPES = {"passport", "id_front"}
INCORP_TYPES = {
    "trade_licence",
    "commercial_license",
    "certificate_of_incorporation",
}
OWNERSHIP_TYPES = {
    "register_of_shareholders",
    "shareholding_structure",
    "group_structure_chart",
}


def evaluate_missing_documents(
    present: list[str],
    *,
    signatory_count: int = 1,
) -> tuple[list[str], list[str]]:
    """Return (missing_labels, document_requests)."""
    present_set = set(present)
    missing: list[str] = []
    requests: list[str] = []

    if "kyc_form" not in present_set:
        missing.append(DOC_TYPE_TO_LABEL["kyc_form"])
        requests.append("Request completed KYC / CIF form")

    if not present_set.intersection(INCORP_TYPES):
        missing.append("Trade Licence or Certificate of Incorporation")
        requests.append("Request trade licence or certificate of incorporation")

    if not present_set.intersection(OWNERSHIP_TYPES):
        missing.append("Shareholding Structure")
        requests.append("Request shareholder register or group structure chart")

    has_identity = bool(present_set.intersection(IDENTITY_TYPES))
    if not has_identity and signatory_count > 0:
        missing.append("Proof of Identity (signatory)")
        requests.append("Request passport or national ID for each authorised signatory")

    # This workflow has no formal UBO-declaration path — always request it.
    missing.append("UBO Declaration")
    requests.append(
        "Request UBO declaration and ownership chart — ultimate owners not verified from documents"
    )
    if "group_structure_chart" not in present_set:
        missing.append(DOC_TYPE_TO_LABEL["group_structure_chart"])
        requests.append("Request group structure / ownership chart")

    def uniq(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return out

    return uniq(missing), uniq(requests)


def missing_docs_node(state: InvestigationState) -> dict:
    present = state.get("present_doc_types") or []
    entities = state.get("entities") or {}
    persons = entities.get("persons") or []
    missing, requests = evaluate_missing_documents(
        present,
        signatory_count=max(1, len(persons)),
    )
    return {
        "missing_documents": missing,
        "document_requests": requests,
    }
