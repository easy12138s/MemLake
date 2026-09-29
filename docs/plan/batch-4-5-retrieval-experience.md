# 检索体验双批次实施计划（批次四 + 批次五）

> 依据：2026-09-29 Dev Agent 实际使用体验报告（5 场景 24 次调用，第一视角）
> 根因结论：**可见性缺陷三下游一根本因**——症状检索失效（P1-1）、Agent 重复沉淀（场景 D 的"因"）、
> 冲突检测候选域为空（场景 D 的"漏"），均源于 system 级知识被 project 隔离切断。
> 已拍板决策：Pitfall 自动边用 `described_by`；tags 灌数据不做；批次四→批次五顺序实施。

## 批次四：P0 闭环（影响分析契约 + 可见性统一）

### 4.1 `analyze_impact_scope` 读取侧扩展（消 P0-1 存量侧）

- `graph.py::impact_analysis` 遍历规则扩展：
  - 新增 `Requirement --described_by--> Pitfall`（depth 1）→ 新返回段 `pitfalls`
  - 新增 `references` 边遍历（存量按官方契约沉淀的资产立即可见），按 `node_type` 归入对应段
- `ImpactScopeOutput` 增加 `pitfalls` 字段（加法兼容）
- 对不存在的 `requirement_id` 从"返回全空结构"改为明确报错（消报告附录的静默 not_found）

### 4.2 写入侧自动建边语义映射（消 P0-1 增量侧）

- `submit_dev_artifacts` 传 `requirement_id` 时四类产物统一自动建边：
  - CodeSnippet → `implements`、Solution → `realized_by`、DesignIntent → `embodies`、Pitfall → `described_by`
- docstring / dev REFERENCE 同步（"仅 CodeSnippet 自动建边"表述更新）

### 4.3 `search_code_snippets` 支持 system 维度（消 P1-1 症状式检索）

- `project_id` 改可选 + 新增 `system_id`（Key 绑定兜底，复用 `resolve_search_scope_fallback`）
- `system_id` → `FilterSpec.project_ids = get_system_project_ids(sid)`

### 4.4 `detect_conflicts` 候选域扩展（消场景 D 查重失效）

- `policy.py` 宽松路径：asset 类型节点的 L1 候选域从单 project 扩为 system 维度
- 行为变化（即修复）：跨 project 语义重复进入 `needs_human_review`；L2/L3 门禁不变

### 4.5 部署侧操作（非代码）

- 上线后 admin 执行 `manage_system(action=add_projects)` 把存量项目挂入相应 system——
  不挂载则 4.3/4.4 候选域仍为空

### 批次四测试与验收

- RED 集成用例 ×4：references 存量影响分析可见 / 四映射自动边 / system 维度跨 project 检索 / 跨 project 查重进 review
- 全量回归 + ruff/mypy 基线 → 镜像重建 → 线上复测报告案例

## 批次五：体验完备（自证 + 回执 + 存量治理）

### 5.1 `candidates_total` 真总数（消 P0-3）

- 融合列表不再受 `candidate_n` 截断 → 语义修正为"min_score 过滤后、top_n 截断前"的真实候选数
- 出参加 `truncated` 标记；测试锚点：同 query 不同 top_n → candidates_total 恒定

### 5.2 `list_requirements` 增强（消 P2-2/P2-3）

- `requirement_key` 精确参数；`fields` 字段白名单裁剪

### 5.3 写入回执增强（消 P1-2）

- lax/auto_approved 回执带 `created:[{ref,node_id,node_type,title}]` + `edges_created/edges_failed`

### 5.4 存量重复语料检测

- `scripts/find_duplicate_nodes.py` 运维脚本：同类型 + embedding 相似输出跨版本近似对；归档走 update_node

### 5.5 小项

- `EmbeddingError` 加 `retryable` 标志（ConnectError→True；ReadTimeout→False 防叠压）
- skills 文档同步 + bump 1.11.0

## 执行顺序与兼容性声明

```
批次四 TDD → GREEN → 全量回归 → 提交
批次五 TDD → GREEN → 全量回归 → 提交
镜像重建 → admin add_projects 挂存量项目 → 线上复测报告场景 B/C/D
```

- 4.4 的行为变化（跨 project 重复从静默通过变为 needs_human_review）是修复而非回归，commit 中明示
- 其余均为加法兼容（pitfalls 字段、可选参数、回执扩展字段）
