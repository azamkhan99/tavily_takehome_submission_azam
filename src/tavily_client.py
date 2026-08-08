"""Tavily Search API wrapper with tracing, parallel runs, and evidence helpers."""

from __future__ import annotations

import hashlib
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable

from tavily import TavilyClient

from kyc_agent.state import Evidence, Provenance, SearchQuery, TavilyTrace


CLIENT_NAME = "kyc-investigation-agent"
SECRET_KEYS = ("api_key", "authorization", "cookie", "token", "tavily_api_key")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def redact_secrets(payload: Any) -> Any:
    if isinstance(payload, dict):
        redacted: dict[str, Any] = {}
        for key, value in payload.items():
            if any(s in key.lower() for s in SECRET_KEYS):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_secrets(value)
        return redacted
    if isinstance(payload, list):
        return [redact_secrets(item) for item in payload]
    if isinstance(payload, str):
        return re.sub(r"(tvly-[A-Za-z0-9_-]+)", "[REDACTED]", payload)
    return payload


def claim_hash(url: str, claim: str) -> str:
    return hashlib.sha256(f"{url}|{claim.strip().lower()}".encode()).hexdigest()[:16]


def dedupe_evidence(items: list[Evidence]) -> list[Evidence]:
    seen: set[str] = set()
    unique: list[Evidence] = []
    for item in items:
        key = claim_hash(item.url, item.claim)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


class TracedTavilyClient:
    """Thin wrapper around Tavily Search that always records safe traces."""

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or os.getenv("TAVILY_API_KEY")
        if not key:
            raise RuntimeError("Missing TAVILY_API_KEY")
        self._client = TavilyClient(api_key=key, client_name=CLIENT_NAME)

    def search(
        self,
        query: SearchQuery,
    ) -> tuple[list[Evidence], TavilyTrace]:
        topic = query.topic if query.topic in {"general", "news", "finance"} else "general"
        params: dict[str, Any] = {
            "api": "search",
            "search_depth": "advanced",
            "max_results": 5,
            "topic": topic,
            "include_answer": True,
        }
        started = time.perf_counter()
        timestamp = utc_now()
        try:
            response = self._client.search(
                query=query.query,
                search_depth="advanced",
                max_results=5,
                topic=topic,
                include_answer=True,
            )
            duration_ms = (time.perf_counter() - started) * 1000
            evidence = self._search_to_evidence(query, response)
            results = response.get("results") or []
            sources = [
                {
                    "title": r.get("title") or "Untitled",
                    "url": r.get("url") or "",
                    "published_date": r.get("published_date"),
                    "content": (r.get("content") or "")[:280],
                    "score": r.get("score"),
                }
                for r in results
                if isinstance(r, dict)
            ]
            answer = response.get("answer")
            if isinstance(answer, str):
                answer = answer[:800]
            else:
                answer = None
            trace = TavilyTrace(
                timestamp=timestamp,
                objective_id=query.objective_id,
                query=query.query,
                params=redact_secrets(params),
                duration_ms=round(duration_ms, 1),
                answer=answer,
                result_count=len(sources),
                sources=sources,
            )
            return evidence, trace
        except Exception as exc:  # noqa: BLE001 - surface in trace for analyst UI
            duration_ms = (time.perf_counter() - started) * 1000
            trace = TavilyTrace(
                timestamp=timestamp,
                objective_id=query.objective_id,
                query=query.query,
                params=redact_secrets(params),
                duration_ms=round(duration_ms, 1),
                error=str(exc),
            )
            return [], trace

    def search_many(
        self,
        queries: list[SearchQuery],
        *,
        max_workers: int = 4,
        on_trace: Callable[[TavilyTrace], None] | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> tuple[list[Evidence], list[TavilyTrace]]:
        if not queries:
            return [], []

        evidence: list[Evidence] = []
        traces: list[TavilyTrace] = []

        with ThreadPoolExecutor(max_workers=min(max_workers, len(queries))) as pool:
            futures = {}
            for q in queries:
                if on_progress:
                    on_progress(f"Searching: `{q.query}`")
                futures[pool.submit(self.search, q)] = q
            for future in as_completed(futures):
                items, trace = future.result()
                evidence.extend(items)
                traces.append(trace)
                if on_trace:
                    on_trace(trace)

        return dedupe_evidence(evidence), traces

    def _search_to_evidence(
        self, query: SearchQuery, response: dict[str, Any]
    ) -> list[Evidence]:
        provenance: Provenance = (
            "inferred"
            if query.category == "ownership" and "infer" in (query.rationale or "").lower()
            else "public_corroboration"
        )
        items: list[Evidence] = []
        answer = response.get("answer")
        results = [r for r in (response.get("results") or []) if isinstance(r, dict)]
        primary = results[0] if results else {}
        if isinstance(answer, str) and answer.strip():
            items.append(
                Evidence(
                    entity=query.entity,
                    category=query.category,
                    claim=answer.strip()[:800],
                    confidence=0.7,
                    source=primary.get("title") or "Tavily search answer",
                    published_date=primary.get("published_date"),
                    url=primary.get("url") or "https://tavily.com",
                    provenance=provenance,
                    objective_id=query.objective_id,
                )
            )
        for result in results[:5]:
            snippet = " ".join(str(result.get("content") or "").split())
            url = result.get("url") or ""
            if not snippet or not url:
                continue
            score = result.get("score")
            try:
                confidence = float(score) if score is not None else 0.55
            except (TypeError, ValueError):
                confidence = 0.55
            items.append(
                Evidence(
                    entity=query.entity,
                    category=query.category,
                    claim=snippet[:500],
                    confidence=max(0.0, min(confidence, 1.0)),
                    source=result.get("title") or "Untitled",
                    published_date=result.get("published_date"),
                    url=url,
                    provenance=provenance,
                    objective_id=query.objective_id,
                )
            )
        return dedupe_evidence(items)
