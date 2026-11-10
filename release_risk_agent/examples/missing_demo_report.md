# 发布风险评审报告

- 评审编号：verify-missing
- 变更编号：missing
- 调查范围：order-service / staging
- 状态：incomplete
- 评审材料指纹：cbb17476a340f225c2ccf5698411b30f196177c165a6e1c2bfff850d998d3b76
- 模型模式：demo
- 协调后端：local

本报告是发布风险评审材料；acknowledge只确认已阅读，不批准发布。没有执行发布、回滚或其他生产变更。
模拟材料标记synthetic=true；本次模型为固定脚本，不代表Qwen效果。

## 规则检查结果

| 级别 | 规则 | 发现与说明 | 证据 |
|---|---|---|---|
| warning | MISSING_CI_MATERIAL | 缺少可用CI检查材料；尚未取得明确通过/失败的检查记录；缺材料不等于检查已失败。 |  |
| warning | MISSING_CAPACITY_MATERIAL | 缺少可用容量材料；缺少目标总连接需求或数据库可用预算，不能认定容量足够，也不能认定已超额。 |  |

## 模型辅助分析（需复核）

尚未生成分析。

## 审核与质量状态

质量问题：[{"code": "required_current_material_missing", "path": "evidence", "message": "缺少必需的当前发布、配置、代码、测试或依赖容量资料；不能声称评审完整。"}]
人工记录：{"reviewer": "scripted-test-reviewer", "decision": "acknowledge", "comment": "自动化演示意见，非真实人工审核", "proposed_digest": "cbb17476a340f225c2ccf5698411b30f196177c165a6e1c2bfff850d998d3b76", "reviewed_at": "2026-10-30T07:47:59.428452+00:00", "scope": {"change_id": "missing", "service": "order-service", "environment": "staging", "snapshot_id": "release-demo-2026-10-v1"}, "outcome": "incomplete", "production_approval": false, "record_type": "release_risk_report_review"}

## 工具与证据

| ID | 类型 | 来源服务 | 模拟数据 |
|---|---|---|---|
| missing-release | release_snapshot | order-service | True |
| missing-config | config_diff | order-service | True |
| missing-code | code_diff | order-service | True |
| missing-runbook | runbook | order-service | True |
| missing-case | incident_case | order-service | True |

## 执行观测

```json
{
  "tool_records": [
    {
      "name": "get_release_snapshot",
      "ok": true,
      "duration_ms": 62.149,
      "evidence_count": 1,
      "mcp_transport": "inprocess",
      "cache_hit": false
    },
    {
      "name": "get_config_diff",
      "ok": true,
      "duration_ms": 62.107,
      "evidence_count": 1,
      "mcp_transport": "inprocess",
      "cache_hit": false
    },
    {
      "name": "get_code_diff",
      "ok": true,
      "duration_ms": 61.401,
      "evidence_count": 1,
      "mcp_transport": "inprocess",
      "cache_hit": false
    },
    {
      "name": "get_ci_checks",
      "ok": true,
      "duration_ms": 61.404,
      "evidence_count": 0,
      "mcp_transport": "inprocess",
      "cache_hit": false
    },
    {
      "name": "get_dependency_budget",
      "ok": true,
      "duration_ms": 62.802,
      "evidence_count": 0,
      "mcp_transport": "inprocess",
      "cache_hit": false
    },
    {
      "name": "search_runbooks",
      "ok": true,
      "duration_ms": 62.451,
      "evidence_count": 1,
      "mcp_transport": "inprocess",
      "cache_hit": false
    },
    {
      "name": "search_incident_cases",
      "ok": true,
      "duration_ms": 62.347,
      "evidence_count": 1,
      "mcp_transport": "inprocess",
      "cache_hit": false
    }
  ],
  "model_calls": [],
  "execution": {
    "last_command_elapsed_ms": 90.628,
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
