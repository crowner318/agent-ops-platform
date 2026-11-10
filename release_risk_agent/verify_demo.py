"""Reproducible, no-key CLI verification in a new directory; no human approval."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parent


def main():
    target = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "runtime" / ("verification-" + uuid4().hex[:8])
    target.mkdir(parents=True, exist_ok=False)
    data = target / "data"
    data.mkdir()
    shutil.copyfile(ROOT / "data" / "fixtures.json", data / "fixtures.json")
    results = []

    def call(*args, expected=0):
        command = [sys.executable, "run.py", *args, "--runtime-dir", str(target / "runtime"), "--data-dir", str(data)]
        started = time.monotonic()
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120)
        if result.returncode != expected:
            raise RuntimeError(f"CLI {args[0]} returned {result.returncode}; expected {expected}: " + result.stderr[-1500:])
        return round(time.monotonic() - started, 3)

    for change, outcome, code in (("risk-pool", "blocked", 2), ("safe", "review_acknowledged", 0),
                                  ("missing", "incomplete", 2)):
        review = "verify-" + change
        elapsed = call("start", "--demo", "--change-id", change, "--review-id", review)
        report = target / "runtime" / "reports" / (review + ".json")
        paused = json.loads(report.read_text(encoding="utf-8"))
        assert paused["status"] == "waiting_human"
        elapsed += call("resume", "--review-id", review, "--decision", "acknowledge",
                        "--reviewer", "scripted-test-reviewer", "--comment", "自动化演示意见，非真实人工审核", expected=code)
        final = json.loads(report.read_text(encoding="utf-8"))
        assert final["status"] == outcome and final["deployment_approved"] is False
        assert all(r["mcp_transport"] == "inprocess" for r in final["tool_records"])
        results.append({"change_id": change, "status_before": paused["status"], "status_after": final["status"],
            "tool_calls": len(final["tool_records"]), "evidence_count": len(final["evidence"]),
            "model_source": final["analysis_source"], "cli_wall_seconds": round(elapsed, 3),
            "quality_issues": final["quality_issues"], "api_requests": 0, "redis_service_used": False})

    old = target / "synthetic_old_report.json"
    old.write_text(json.dumps({"session_id": "synthetic-legacy-demo", "complete": True,
        "scope": {"service": "order-service", "environment": "demo"},
        "answer": "已审核模拟案例：连接池修改需要核对负载、容量与实际生效配置。",
        "evidence": [{"id": "synthetic-old-1", "synthetic": True}]}, ensure_ascii=False), encoding="utf-8")
    call("import-legacy", "--source", str(old))
    assert json.loads((data / "legacy_cases.json").read_text(encoding="utf-8"))[0]["verified"] is False
    call("import-legacy", "--source", str(old), "--confirmed")
    assert json.loads((data / "legacy_cases.json").read_text(encoding="utf-8"))[0]["verified"] is True
    call("start", "--demo", "--change-id", "legacy-demo", "--review-id", "verify-legacy")
    legacy = json.loads((target / "runtime" / "reports" / "verify-legacy.json").read_text(encoding="utf-8"))
    assert any(row["kind"] == "incident_case" and row.get("content", {}).get("source_type") == "legacy_report_json"
               for row in legacy["evidence"])
    results.append({"change_id": "legacy-demo", "status_after": legacy["status"], "legacy_import_verified": True,
                    "note": "confirmation is a synthetic test fixture, not real human approval"})
    (target / "verification.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"verification_directory": str(target), "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
