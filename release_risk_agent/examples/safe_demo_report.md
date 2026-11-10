# 发布风险评审报告

- 评审编号：verify-safe
- 变更编号：safe
- 调查范围：order-service / staging
- 状态：review_acknowledged
- 评审材料指纹：3f9c9c2228cec2f5a423ae9e404a3567aaf77a2d3dc9e31810e519caa8cd9dde
- 模型模式：demo
- 协调后端：local

本报告是发布风险评审材料；acknowledge只确认已阅读，不批准发布。没有执行发布、回滚或其他生产变更。
模拟材料标记synthetic=true；本次模型为固定脚本，不代表Qwen效果。

## 规则检查结果

| 级别 | 规则 | 发现与说明 | 证据 |
|---|---|---|---|
| — | — | 未生成规则发现；不能据此认为发布安全 | — |

## 模型辅助分析（需复核）

脚本演示：请结合规则检查和证据完成人工评审；本程序不批准发布。
- 配置、测试与依赖容量需要联合复核：这是脚本演示产生的风险复核方向；它不构成发布批准，不能替代程序规则检查。；引用=safe-config
  - 待验证：对照本次变更的实际配置、测试结果和依赖容量，补充独立验证记录。
- 流程待核查：核查是否具备容量评估、回退方案和测试记录。

## 审核与质量状态

质量问题：[]
人工记录：{"reviewer": "scripted-test-reviewer", "decision": "acknowledge", "comment": "自动化演示意见，非真实人工审核", "proposed_digest": "3f9c9c2228cec2f5a423ae9e404a3567aaf77a2d3dc9e31810e519caa8cd9dde", "reviewed_at": "2026-10-30T07:47:50.482517+00:00", "scope": {"change_id": "safe", "service": "order-service", "environment": "staging", "snapshot_id": "release-demo-2026-10-v1"}, "outcome": "review_acknowledged", "production_approval": false, "record_type": "release_risk_report_review"}

## 工具与证据

| ID | 类型 | 来源服务 | 模拟数据 |
|---|---|---|---|
| safe-release | release_snapshot | order-service | True |
| safe-config | config_diff | order-service | True |
| safe-code | code_diff | order-service | True |
| safe-ci | ci_checks | order-service | True |
| safe-capacity | dependency_budget | order-service | True |
| safe-runbook | runbook | order-service | True |
| safe-case | incident_case | order-service | True |

## 执行观测

```json
{
  "tool_records": [
    {
      "name": "get_release_snapshot",
      "ok": true,
      "duration_ms": 60.471,
      "evidence_count": 1,
      "mcp_transport": "inprocess",
      "cache_hit": false
    },
    {
      "name": "get_config_diff",
      "ok": true,
      "duration_ms": 60.337,
      "evidence_count": 1,
      "mcp_transport": "inprocess",
      "cache_hit": false
    },
    {
      "name": "get_code_diff",
      "ok": true,
      "duration_ms": 59.671,
      "evidence_count": 1,
      "mcp_transport": "inprocess",
      "cache_hit": false
    },
    {
      "name": "get_ci_checks",
      "ok": true,
      "duration_ms": 59.558,
      "evidence_count": 1,
      "mcp_transport": "inprocess",
      "cache_hit": false
    },
    {
      "name": "get_dependency_budget",
      "ok": true,
      "duration_ms": 60.823,
      "evidence_count": 1,
      "mcp_transport": "inprocess",
      "cache_hit": false
    },
    {
      "name": "search_runbooks",
      "ok": true,
      "duration_ms": 61.16,
      "evidence_count": 1,
      "mcp_transport": "inprocess",
      "cache_hit": false
    },
    {
      "name": "search_incident_cases",
      "ok": true,
      "duration_ms": 60.847,
      "evidence_count": 1,
      "mcp_transport": "inprocess",
      "cache_hit": false
    }
  ],
  "model_calls": [
    {
      "source": "scripted_demo",
      "scripted": true,
      "attempts": 0,
      "success": true,
      "usage": {
        "prompt_tokens": null,
        "completion_tokens": null,
        "total_tokens": null,
        "cached_prompt_tokens": null
      },
      "latency_ms": 3.427
    }
  ],
  "execution": {
    "last_command_elapsed_ms": 80.482,
    "coordinator": {
      "backend": "local",
      "durability": "process_memory_only",
      "cache_hits": 0,
      "cache_misses": 0,
      "cache_entries": 0,
      "active_locks": 1,
      "queue_total": 0,
      "queue_pending": 0
    }
  }
}
```

工具返回的证据存在，不代表模型每句话都被证据充分支持；最终决定须依照独立的发布管理流程。
