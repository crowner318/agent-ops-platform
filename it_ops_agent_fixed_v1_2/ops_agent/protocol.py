"""Parse inert provider JSON with narrowly defined, recorded normalizations.

No eval, execution, tool dispatch or search through multiple object candidates
is performed. Prose recovery accepts exactly one top-level object; it never
extracts an object from a list, code wrapper, or ambiguous sequence of values.
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

MAX_DOCUMENT_CHARS = 48_000


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _constant(_):
    raise ValueError("nonfinite_json")


_DECODER = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_constant)


def _source(raw: str) -> tuple[str, list[dict]]:
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_DOCUMENT_CHARS:
        raise ValueError("invalid_json")
    source, normalizations = raw.strip(), []
    if source.startswith("```"):
        fenced = re.fullmatch(r"```json[ \t]*\r?\n([\s\S]*)\r?\n```", source, re.I)
        if not fenced or "```" in fenced.group(1):
            raise ValueError("invalid_json_fence")
        source = fenced.group(1).strip()
        normalizations.append({"code": "json_fence_removed", "path": "final"})
    elif "```" in source:
        raise ValueError("invalid_json_fence")
    return source, normalizations


def _ordinary_prose(text: str) -> bool:
    """A small affix allowance, not an interpreter for arbitrary wrappers."""
    if not text.strip():
        return True
    if (len(text) > 2000 or any(ch in text for ch in '{}[]()`=\\<>|&$')
            or not any(unicodedata.category(ch).startswith("L") for ch in text)):
        return False
    if any(unicodedata.category(ch).startswith("C") and not ch.isspace() for ch in text):
        return False
    if re.search(r"\b(?:import|exec|eval|__import__|lambda|subprocess|return|function|require)\b|"
                 r"\b(?:print|system|json\.loads)\s*\(|\bos\.", text, re.I):
        return False
    # Standalone JSON values in either affix are additional documents, not
    # explanatory prose. Check complete lines, without searching for objects.
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        try:
            _, end = _DECODER.raw_decode(candidate)
        except (ValueError, TypeError, RecursionError):
            # NaN/Infinity are invalid JSON, rather than acceptable prose.
            if re.fullmatch(r"(?:NaN|-?Infinity)", candidate):
                return False
            continue
        # A line beginning with another JSON value is ambiguous even when
        # more prose follows it. Do not reinterpret it as an explanation.
        return False
    return True


def strict_json_document(raw: str) -> tuple[Any, list[dict]]:
    """Decode one JSON value; only an object may have plain prose affixes.

    ValueError rejects malformed/ambiguous documents. Safe envelope removal is
    returned as metadata and does not make an otherwise valid report fail.
    """
    source, normalizations = _source(raw)
    try:
        parsed, end = _DECODER.raw_decode(source)
    except json.JSONDecodeError as exc:
        if source[:1] in ('{', '[', '"'):
            raise ValueError("invalid_json") from None
        # Consider only the first structural opener. A preceding '[' forbids
        # falling through to a dictionary nested inside an array.
        starts = [pos for pos in (source.find("{"), source.find("[")) if pos >= 0]
        if not starts:
            raise ValueError("invalid_json") from None
        start = min(starts)
        if source[start] != "{" or not _ordinary_prose(source[:start]):
            raise ValueError("ambiguous_json_document") from None
        try:
            parsed, end = _DECODER.raw_decode(source, start)
        except (ValueError, RecursionError):
            raise ValueError("invalid_json") from None
        if not isinstance(parsed, dict) or not _ordinary_prose(source[end:]):
            raise ValueError("ambiguous_json_document") from None
        normalizations.append({"code": "plain_prose_removed", "path": "final",
                               "prefix_chars": start, "suffix_chars": len(source) - end})
        return parsed, normalizations
    except (ValueError, RecursionError):
        # Duplicate keys and non-finite constants must not be recovered by
        # looking for a different object later in the same response.
        raise ValueError("invalid_json") from None
    if source[end:].strip():
        if not isinstance(parsed, dict) or not _ordinary_prose(source[end:]):
            raise ValueError("ambiguous_json_document")
        normalizations.append({"code": "plain_prose_removed", "path": "final",
                               "prefix_chars": 0, "suffix_chars": len(source) - end})
    return parsed, normalizations


def parse_tool_plan(raw: str, max_calls: int = 64) -> list[dict] | None:
    """Recognize an entire JSON tool-plan array, without executing its tools.

    Ordinary final objects and text return None. An array-shaped but malformed
    or unauthorized-by-shape plan raises ValueError; tool names themselves are
    left to the runtime Harness allowlist.
    """
    if type(max_calls) is not int or not 0 <= max_calls <= 64:
        raise ValueError("invalid_tool_plan_budget")
    if not isinstance(raw, str) or not raw.strip():
        return None
    trimmed = raw.strip()
    if trimmed.startswith("```"):
        source, _ = _source(raw)
    else:
        source = trimmed
    if not source.startswith("["):
        return None
    if len(raw) > MAX_DOCUMENT_CHARS:
        raise ValueError("invalid_tool_plan")
    try:
        parsed, end = _DECODER.raw_decode(source)
    except (ValueError, RecursionError):
        raise ValueError("invalid_tool_plan") from None
    if source[end:].strip() or not isinstance(parsed, list) or not parsed or len(parsed) > max_calls:
        raise ValueError("invalid_tool_plan")
    seen, result = set(), []
    for item in parsed:
        if not isinstance(item, dict) or set(item) != {"id", "name", "args"}:
            raise ValueError("invalid_tool_plan")
        if (any(not isinstance(item[key], str) or not item[key].strip() or len(item[key]) > 128
                for key in ("id", "name")) or not isinstance(item["args"], dict) or item["id"] in seen):
            raise ValueError("invalid_tool_plan")
        seen.add(item["id"])
        result.append({"id": item["id"], "name": item["name"], "arguments": item["args"]})
    return result
