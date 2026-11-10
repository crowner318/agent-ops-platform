"""Runtime policy checks independent of model prompts and skill implementations."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from yaml.tokens import AliasToken, AnchorToken


ROLE_TOOL_CAPS: dict[str, tuple[str, ...]] = {
    "triage": (
        "query_metrics", "search_logs", "collect_evidence",
    ),
    "diagnosis": (
        "query_metrics", "search_logs", "analyze_traces", "get_changes",
        "search_runbooks", "collect_evidence", "assess_remediation", "build_postmortem",
    ),
    "remediation": (
        "assess_remediation", "build_postmortem",
    ),
}
ALL_TOOLS = frozenset(tool for tools in ROLE_TOOL_CAPS.values() for tool in tools)
LIMIT_BOUNDS = {
    "max_steps": 32,
    "max_tool_calls": 64,
    "max_seconds": 900,
    "max_output_chars": 200_000,
}
REDACTED = "[REDACTED]"


class _UniqueKeyLoader(yaml.SafeLoader):
    """Reject ambiguous policy documents instead of silently taking the last key."""


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: Any, deep: bool = False) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ValueError("Harness YAML mapping keys must be strings")
        if key in result:
            raise ValueError(f"Duplicate harness YAML field: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _sensitive_key(key: str) -> bool:
    """Match credential fields while preserving counters such as max_tokens."""
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return (
        normalized in {"apikey", "password", "passwd", "secret", "authorization", "token"}
        or normalized.endswith(("apikey", "password", "passwd", "secret", "authorization"))
        or normalized.endswith("token")
    )


_BEARER = re.compile(r"(?i)\bBearer\s+[a-z0-9._~+/=-]+")
_SK_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|password|passwd|secret|access[_-]?token|refresh[_-]?token|token|authorization)"
    r"\b[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&}\]]+)"
)


def _redact_text(value: str) -> str:
    # Tool arguments and model-produced reports may be JSON encoded in a string.
    # Redact their parsed structure so quote handling remains valid JSON.
    if value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
            return json.dumps(redact(parsed), ensure_ascii=False, allow_nan=False)
        except (json.JSONDecodeError, ValueError, RecursionError):
            # Non-JSON log lines still receive best-effort text filtering.
            pass
    value = _BEARER.sub("Bearer " + REDACTED, value)
    value = _SK_KEY.sub(REDACTED, value)
    return _ASSIGNMENT.sub(lambda match: match.group(1) + REDACTED, value)


def redact(value: Any) -> Any:
    """Return a redacted copy suitable for traces, reports and persisted memory.

    This is best-effort secret filtering, not a substitute for source-side
    sanitization. Do not persist raw model reasoning or raw credential stores.
    """
    if isinstance(value, dict):
        return {
            str(key): REDACTED if _sensitive_key(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _mapping(value: Any, name: str, expected: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{name} must be an object with string keys")
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown or missing:
        raise ValueError(f"{name}: unknown fields={sorted(unknown)}, missing fields={sorted(missing)}")
    return value


def _string_list(value: Any, name: str, maximum: int = 100) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise ValueError(f"{name} must be a nonempty list of at most {maximum} strings")
    if any(not isinstance(item, str) or not item.strip() or len(item) > 128 for item in value):
        raise ValueError(f"{name} contains an invalid string")
    if len(value) != len(set(value)):
        raise ValueError(f"{name} must not contain duplicates")
    return list(value)


class Harness:
    """Load a restrictive policy; YAML can only reduce hardcoded capabilities."""

    def __init__(self, path: Path):
        raw = Path(path).read_text(encoding="utf-8")
        if len(raw) > 65_536:
            raise ValueError("Harness config is too large")
        try:
            # Policy documents do not need aliases, merge keys or recursive nodes.
            if any(isinstance(token, (AliasToken, AnchorToken)) for token in yaml.scan(raw)):
                raise ValueError("YAML anchors and aliases are not permitted")
            config = yaml.load(raw, Loader=_UniqueKeyLoader)
        except (yaml.YAMLError, RecursionError) as exc:
            raise ValueError(f"Invalid harness YAML: {exc}") from exc
        config = _mapping(config, "config", {"schema_version", "limits", "scope", "roles", "l0"})
        if type(config["schema_version"]) is not int or config["schema_version"] != 1:
            raise ValueError("schema_version must be 1")
        limits = _mapping(config["limits"], "limits", set(LIMIT_BOUNDS))
        for key, upper_bound in LIMIT_BOUNDS.items():
            value = limits[key]
            if type(value) is not int or not 1 <= value <= upper_bound:
                raise ValueError(f"{key} must be an integer between 1 and {upper_bound}")
            setattr(self, key, value)
        scope = _mapping(config["scope"], "scope", {"allowed_environments", "allowed_services"})
        self.allowed_environments = _string_list(scope["allowed_environments"], "allowed_environments")
        self.allowed_services = _string_list(scope["allowed_services"], "allowed_services")
        roles = _mapping(config["roles"], "roles", set(ROLE_TOOL_CAPS))
        self._roles: dict[str, list[str]] = {}
        for role, hard_cap in ROLE_TOOL_CAPS.items():
            names = _string_list(roles[role], f"roles.{role}", len(ALL_TOOLS))
            unauthorized = set(names) - set(hard_cap)
            if unauthorized:
                raise ValueError(f"roles.{role} exceeds hardcoded capabilities: {sorted(unauthorized)}")
            self._roles[role] = names
        self.l0 = _mapping(config["l0"], "l0", {"identity", "constraints", "execution_risk"})
        if not isinstance(self.l0["identity"], str) or not 1 <= len(self.l0["identity"]) <= 2000:
            raise ValueError("l0.identity must be a nonempty string of at most 2000 characters")
        for key in ("constraints", "execution_risk"):
            _string_list(self.l0[key], f"l0.{key}")

    def allowed_tools(self, role: str, workflow: str = "single") -> list[str]:
        if workflow not in ("single", "swarm"):
            raise ValueError("workflow must be single or swarm")
        if not isinstance(role, str) or role not in self._roles:
            raise ValueError(f"Unknown role: {role}")
        names = list(self._roles[role])
        if workflow == "swarm" and role == "diagnosis":
            # Workflow capabilities are a hard runtime boundary. A permissive
            # YAML role list cannot give the specialist the final planner's job.
            names = [name for name in names if name not in {"assess_remediation", "build_postmortem"}]
        return names

    def validate_scope(self, service: str, environment: str) -> None:
        if not isinstance(service, str) or service not in self.allowed_services:
            raise ValueError("Service is outside the configured scope")
        if not isinstance(environment, str) or environment not in self.allowed_environments:
            raise ValueError("Environment is outside the configured scope")

    def validate_tool(self, role: str, name: str, arguments: dict[str, Any],
                      workflow: str = "single") -> None:
        allowed = self.allowed_tools(role, workflow)
        if not isinstance(name, str) or name not in ALL_TOOLS or name not in allowed:
            raise ValueError(f"Tool is not allowed for {role}: {name}")
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a JSON object")
        self._validate_payload(arguments)
        if name == "collect_evidence":
            kinds = arguments.get("kinds")
            if (not isinstance(kinds, list) or not 1 <= len(kinds) <= 5
                    or any(not isinstance(kind, str) or kind not in {"metrics", "logs", "traces", "changes", "runbooks"} for kind in kinds)
                    or len(set(kinds)) != len(kinds)):
                raise ValueError("collect_evidence requires 1-5 unique allowed kinds")
            if role == "triage" and not set(kinds) <= {"metrics", "logs"}:
                raise ValueError("triage collect_evidence is limited to metrics/logs")
        # A model cannot redirect a permitted query to another service/environment.
        # Most tools receive scope directly; this walk also checks nested selectors.
        self._validate_payload_scope(arguments)

    @staticmethod
    def _validate_payload(arguments: dict[str, Any]) -> None:
        count = 0

        def walk(value: Any, depth: int) -> None:
            nonlocal count
            count += 1
            if depth > 12 or count > 2000:
                raise ValueError("Tool payload nesting/item limit exceeded")
            if isinstance(value, dict):
                for key, item in value.items():
                    if not isinstance(key, str) or len(key) > 128:
                        raise ValueError("Tool payload has an invalid object key")
                    walk(item, depth + 1)
            elif isinstance(value, list):
                for item in value:
                    walk(item, depth + 1)
            elif isinstance(value, str):
                if len(value) > 16_000:
                    raise ValueError("Tool payload string limit exceeded")
            elif value is not None and type(value) not in (bool, int, float):
                raise ValueError("Tool payload must contain only JSON values")

        walk(arguments, 0)
        try:
            encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, OverflowError) as exc:
            raise ValueError("Tool payload must contain finite JSON values") from exc
        if len(encoded) > 32_768:
            raise ValueError("Tool payload total size limit exceeded")

    def _validate_payload_scope(self, value: Any) -> None:
        if isinstance(value, dict):
            if "service" in value:
                if value["service"] not in self.allowed_services:
                    raise ValueError("Tool service is outside the configured scope")
            if "environment" in value:
                if value["environment"] not in self.allowed_environments:
                    raise ValueError("Tool environment is outside the configured scope")
            for item in value.values():
                self._validate_payload_scope(item)
        elif isinstance(value, list):
            for item in value:
                self._validate_payload_scope(item)
