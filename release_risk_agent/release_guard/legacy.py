"""Read-only legacy JSON import. Confirmation is an explicit human CLI option."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import threading
from uuid import uuid4

from .storage import sanitize


MAX_REPORT_BYTES = 2 * 1024 * 1024
_IMPORT_LOCK = threading.RLock()


def _scope(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError(f"旧报告{field}范围无效，不能导入通配或缺失范围。")
    return value


def import_legacy_report(source: Path, data_dir: Path, confirmed: bool = False) -> dict:
    if type(confirmed) is not bool:
        raise ValueError("confirmed必须是人工CLI显式布尔选项。")
    source, data_dir = Path(source), Path(data_dir)
    if source.suffix.casefold() != ".json" or not source.is_file():
        raise ValueError("只能导入已有的JSON报告文件。")
    if source.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("旧报告超过2 MiB限制。")
    try:
        with source.open("rb") as stream:
            raw = stream.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES:
            raise ValueError
        payload = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("旧报告不是有效的UTF-8 JSON对象。") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("scope"), dict):
        raise ValueError("旧报告必须包含明确的scope对象。")
    service = _scope(payload["scope"].get("service"), "服务")
    environment = _scope(payload["scope"].get("environment"), "环境")
    summary = payload.get("answer", payload.get("summary"))
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 120000:
        raise ValueError("旧报告需要非空且不超过120000字符的answer/summary。")
    session_id = payload.get("session_id", "unknown")
    if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 200:
        raise ValueError("旧报告会话标识必须是非空且不超过200字符的字符串。")
    evidence = payload.get("evidence", [])
    if not isinstance(evidence, list) or any(not isinstance(row, dict) for row in evidence):
        raise ValueError("旧报告evidence必须是对象数组。")
    for row in evidence:
        if row.get("service", service) != service or row.get("environment", environment) not in {environment, "*"}:
            raise ValueError("旧报告证据与报告服务或环境不一致。")
    synthetic = payload.get("demo") is True or payload.get("synthetic") is True or any(
        row.get("synthetic") is True for row in evidence)
    # Canonical source JSON hash is stable under formatting/key-order changes.
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    source_digest = hashlib.sha256(canonical).hexdigest()
    case = {"id": "legacy-" + source_digest[:24], "service": service, "environment": environment,
            "summary": sanitize(summary), "verified": confirmed,
            "original_session_id": sanitize(session_id),
            "synthetic": synthetic, "synthetic_notice": "Imported report includes synthetic/demo observations." if synthetic else
                "Data origin not independently verified by this import.",
            "report_complete": payload.get("complete") is True,
            "source_type": "legacy_report_json", "source_digest": source_digest,
            "imported_at": datetime.now(timezone.utc).isoformat()}
    target = data_dir / "legacy_cases.json"
    if source.resolve() == target.resolve():
        raise ValueError("导入源不能是目标案例库，原报告将保持只读。")
    data_dir.mkdir(parents=True, exist_ok=True)
    with _IMPORT_LOCK:
        try:
            existing = json.loads(target.read_text(encoding="utf-8")) if target.exists() else []
        except (ValueError, UnicodeError):
            raise ValueError("本地旧案例库格式无效，拒绝覆盖。") from None
        if not isinstance(existing, list) or any(not isinstance(row, dict) for row in existing):
            raise ValueError("本地旧案例库必须是数组，拒绝覆盖。")
        for row in existing:
            if row.get("source_digest") == source_digest:
                # Only an explicit reimport --confirmed can promote a draft;
                # old complete=true and model-written flags never promote it.
                if confirmed and not row.get("verified"):
                    row["verified"] = True
                    row["confirmed_at"] = datetime.now(timezone.utc).isoformat()
                    _write_cases(target, existing)
                return dict(row)
        if confirmed:
            case["confirmed_at"] = datetime.now(timezone.utc).isoformat()
        existing.append(case)
        _write_cases(target, existing)
    return case


def _write_cases(target, cases):
    temporary = target.with_name(target.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(cases, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()
