"""LLM helpers (Nebius via LangChain) and model selection."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from typing import Any, Literal, TypeVar, cast

from langchain_nebius import ChatNebius
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)
CostProfile = Literal["budget", "balanced", "quality"]

PROFILE_MODELS: dict[CostProfile, dict[str, str]] = {
    "budget": {
        "text": "Qwen/Qwen3-30B-A3B-Instruct-2507",
        "vision": "openbmb/MiniCPM-V-4_5",
    },
    "balanced": {
        "text": "meta-llama/Llama-3.3-70B-Instruct",
        "vision": "openbmb/MiniCPM-V-4_5",
    },
    "quality": {
        "text": "moonshotai/Kimi-K2.6",
        "vision": "openbmb/MiniCPM-V-4_5",
    },
}

DEFAULT_PROFILE: CostProfile = "budget"
# Compact multimodal model — preferred for scanned PDF / image OCR latency.
DEFAULT_VISION_MODEL = "openbmb/MiniCPM-V-4_5"


def active_profile() -> CostProfile:
    raw = os.getenv("NEBIUS_COST_PROFILE", DEFAULT_PROFILE).strip().lower()
    if raw in PROFILE_MODELS:
        return cast(CostProfile, raw)
    return DEFAULT_PROFILE


def resolve_text_model() -> str:
    return os.getenv("NEBIUS_MODEL") or PROFILE_MODELS[active_profile()]["text"]


def resolve_vision_model() -> str:
    """Vision/OCR model — never inherits NEBIUS_MODEL (often text-only)."""
    if os.getenv("NEBIUS_VISION_MODEL"):
        return os.environ["NEBIUS_VISION_MODEL"]
    return PROFILE_MODELS[active_profile()].get("vision") or DEFAULT_VISION_MODEL


def get_chat_model(model: str | None = None, *, temperature: float = 0.1) -> ChatNebius:
    if not os.getenv("NEBIUS_API_KEY"):
        raise RuntimeError("Missing NEBIUS_API_KEY")
    return ChatNebius(model=model or resolve_text_model(), temperature=temperature)


def get_vision_model(model: str | None = None, *, temperature: float = 0.0) -> ChatNebius:
    if not os.getenv("NEBIUS_API_KEY"):
        raise RuntimeError("Missing NEBIUS_API_KEY")
    return ChatNebius(model=model or resolve_vision_model(), temperature=temperature)


def model_summary() -> str:
    return (
        f"profile={active_profile()} text={resolve_text_model()} vision={resolve_vision_model()}"
    )


def looks_like_json_schema(data: Any) -> bool:
    """Heuristic: distinguish JSON Schema metadata from instance payloads."""
    if not isinstance(data, dict):
        return False
    if "$defs" in data or "$schema" in data or "$ref" in data:
        return True
    if data.get("type") == "object" and "properties" in data:
        return True
    return False


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def iter_json_values(text: str) -> list[Any]:
    """Decode every JSON object/array found in text."""
    text = _strip_code_fence(text)
    values: list[Any] = []
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        if text[idx] not in "{[":
            idx += 1
            continue
        try:
            value, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            idx += 1
            continue
        values.append(value)
        idx = end
    return values


def extract_json(text: str, *, required_keys: set[str] | None = None) -> Any:
    """Parse the best JSON payload from model output, ignoring schema echoes."""
    text = _strip_code_fence(text)
    try:
        parsed = json.loads(text)
        if not looks_like_json_schema(parsed):
            return parsed
    except json.JSONDecodeError:
        pass

    candidates = iter_json_values(text)
    if not candidates:
        match = re.search(r"\{[\s\S]*\}|\[[\s\S]*\]", text)
        if not match:
            raise ValueError("No JSON object found in model response")
        parsed = json.loads(match.group(0))
        if looks_like_json_schema(parsed):
            raise ValueError("Model returned JSON Schema instead of instance data")
        return parsed

    if required_keys:
        for candidate in reversed(candidates):
            if (
                isinstance(candidate, dict)
                and not looks_like_json_schema(candidate)
                and required_keys.issubset(candidate.keys())
            ):
                return candidate

    for candidate in reversed(candidates):
        if not looks_like_json_schema(candidate):
            return candidate

    raise ValueError("Model returned JSON Schema instead of instance data")


def _example_value(prop: dict[str, Any]) -> Any:
    if "enum" in prop:
        return prop["enum"][0]
    if "anyOf" in prop:
        for option in prop["anyOf"]:
            if option.get("type") != "null":
                return _example_value(option)
    if "$ref" in prop:
        return {}
    prop_type = prop.get("type")
    if prop_type == "string":
        return "..."
    if prop_type == "array":
        items = prop.get("items") or {}
        if items.get("type") == "object":
            return []
        if "enum" in items:
            return []
        return []
    if prop_type == "object":
        return {}
    if prop_type in {"number", "integer"}:
        return 0
    if prop_type == "boolean":
        return False
    return None


def compact_schema_hint(schema: type[BaseModel]) -> str:
    """Compact instance-shaped example — avoids echoing full JSON Schema metadata."""
    properties = schema.model_json_schema().get("properties", {})
    example = {name: _example_value(spec) for name, spec in properties.items()}
    return json.dumps(example, indent=2)


def structured_invoke(
    prompt: str,
    schema: type[T],
    *,
    model: str | None = None,
    max_attempts: int = 2,
    normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> T:
    chat = get_chat_model(model)
    required_keys = set(schema.model_fields.keys())
    schema_hint = compact_schema_hint(schema)

    last_error: Exception | None = None
    for attempt in range(max_attempts):
        strict = attempt > 0
        correction = ""
        if strict:
            correction = (
                "\n\nYour previous response was invalid. Return a single JSON object "
                "with populated field values. Do NOT return JSON Schema, $defs, or "
                "properties metadata.\n"
            )
        message = (
            f"{prompt}{correction}\n\n"
            "Respond with ONLY one JSON object matching this shape "
            "(replace ... placeholders with real values; no markdown):\n"
            f"{schema_hint}"
        )
        result = chat.invoke(message)
        content = result.content if isinstance(result.content, str) else str(result.content)
        try:
            data = extract_json(content, required_keys=required_keys)
            if normalize is not None and isinstance(data, dict):
                data = normalize(data)
            return schema.model_validate(data)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            continue

    if last_error is not None:
        raise last_error
    raise RuntimeError("structured_invoke failed without a captured error")
