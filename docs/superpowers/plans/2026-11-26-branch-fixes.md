# 分支评审问题修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 `docs/plan/2026-11-26-branch-review-audit.md` 中 6 条 Required + 13 条 Consider + 5 条 FYI，同时保证「中方诊药云系统」与「MemLake」两个生产 system 域的全部节点数据零损伤、存量 Key/审批批次/查询行为不被破坏。

**Architecture:** 按 4 个可独立回滚的 PR 落地：A 授权与数据正确性、B 事务并发与性能、C 运维脚本安全、D 文档测试迁移。鉴权采用兼容模式（`system_scope ∪ project_scope 所隶属 system`）；更新语义新增增量 payload + `base_version`，旧快照 payload 保持原执行路径；迁移只做索引，无列变更、无数据回写。

**Tech Stack:** Python 3.12 / FastMCP / SQLAlchemy 2 async / PostgreSQL + Apache AGE / Alembic / pytest / ruff。

**Spec:** `docs/superpowers/specs/2026-11-26-branch-fixes-design.md`

## Global Constraints

- **数据安全（最高优先级）**：「中方诊药云系统」（`System.code IS NULL`，按 name 匹配）与「MemLake」下的节点/边/向量/审计/计数器/重索引任务不得删除或改写。破坏性 `--execute` 只由用户在生产库执行；实现与测试只用隔离测试库。
- **兼容模式**：system 允许集 = `system_scope ∪ {project_scope 中各 project 所属 system}`；不因修复让存量 project-only Key 失效。
- **旧 payload 双读**：升级前落库的 pending 批次按原执行路径（快照 `properties`/`tags`）执行；新批次才使用增量字段与 `base_version`。
- **schema 仅增量**：本计划只有两个索引迁移（0006 新建、0007 改名），无 `UPDATE`/`DELETE`/列变更。
- **测试门禁**：每个任务先写失败测试；完成后 `source .venv/bin/activate && python -m pytest tests/unit -q` 全绿，`ruff check <改动文件>` 干净。
- **GitNexus 门禁**（AGENTS.md）：编辑函数前跑 `node .gitnexus/run.cjs impact "<symbol>" --direction upstream --repo .`；提交前跑 `node .gitnexus/run.cjs detect-changes --scope all --repo .`。
- **提交粒度**：每任务一个 commit；PR 边界见各节标题。

## Review Focus

1. **project-only 存量 Key 的写权限**：升级后仍能在其项目所属 system 内发布需求；跨 system 才被拒。
2. **旧 pending 快照批次**：仍能审批通过并落地，不被新校验拒绝、不被误判为版本冲突。
3. **bind 批次不被新 payload 校验误杀**：`node+update` 仅含 `node_id + project_id` 仍合法。
4. **清理脚本对保留域**：任何参数组合下，两个生产 system 的节点/边/批次/任务都不进入删除计划。
5. **并发审批**：两个 `review_approve` 同时到达时，只有一个成功，另一个收到 `BatchStatusError`，且不产生重复节点/边。

---

## PR-A：授权与数据正确性

### Task A1: system 写鉴权 + project∈system 一致性

**Files:**
- Modify: `src/mem_lake/gateway/dependencies.py`
- Modify: `src/mem_lake/gateway/tools/write_tools.py`（`_publish_requirements_batch:370`、`_validate_requirements_core:555`、`publish_requirement:744`）
- Test: `tests/unit/test_system_write_access.py`（新建）

**Interfaces:**
- Produces:
  - `async def get_allowed_write_system_ids() -> set[uuid.UUID] | None`（`None` = admin 不限）
  - `async def validate_system_write_access(system_id: uuid.UUID) -> None`
  - `async def validate_project_in_system(system_id: uuid.UUID, project_id: uuid.UUID) -> None`
- Consumes: `get_current_system_scope()`, `get_current_project_scope()`, `get_system_ids_by_project()`, `get_system_project_ids()`, `readonly_session()`。

- [ ] **Step 1: 写失败测试**
  覆盖：admin 返回 `None`；pm 的 `project_scope` 反查出 system 并入允许集；system 不存在抛 `ToolError`；project 不在 system 抛 `ToolError`；`_validate_requirements_core` 传他 system 时 `errors` 含「不在当前 Access Key 的 system 范围内」。
- [ ] **Step 2: 运行确认失败**：`pytest tests/unit/test_system_write_access.py -v` → ImportError。
- [ ] **Step 3: 实现三个 helper**；在三个发布/预检入口调用。预检内收集为 `errors`，publish 整次拒绝。
- [ ] **Step 4: 运行通过**：本文件 + `pytest tests/unit/test_publish_requirements.py tests/unit/test_validate_requirements.py -q`。
- [ ] **Step 5: Commit**：`fix(auth): enforce system write scope and project-system membership`

### Task A2: 批内确定性查重

**Files:**
- Create: `src/mem_lake/gateway/tools/_batch_dedupe.py`
- Modify: `write_tools.py`（`_publish_requirements_batch`、`_validate_requirements_core`、工具描述约 `:843`）
- Modify: `docs/wiki/03-工具手册.md:13`、`src/mem_lake/skills/pm/SKILL.md:70`
- Test: `tests/unit/test_batch_dedupe.py`（新建）、`tests/unit/test_validate_requirements.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class DedupeEntry: client_ref, project_id, title, content, external_id`
  - `def normalize_fingerprint(title: str, content: str) -> str`
  - `def find_exact_duplicates(entries) -> dict[str, list[str]]`
  - `async def find_near_duplicates(entries, *, embedding_client, threshold: float) -> dict[str, list[str]]`
- 合并结果：`dict[client_ref, list[str]]`，值为「与 <client_ref>(project=<id|悬浮>) 重复/高度相似」。

- [ ] **Step 1: 写失败测试**：同标题+空白差异 → exact；`external_id` 相同跨 project → exact；mock 向量余弦 0.99 → near；embedding 抛异常 → 仅 exact 且 `degraded=True`。
- [ ] **Step 2: 运行确认失败**。
- [ ] **Step 3: 实现 helper**（NFKC + lower + 空白合并；`embedding_client.embed(texts, prompt_name="query")` 一次批量，两两余弦）。
- [ ] **Step 4: 接入**：publish 预检发现任一重复 → `PayloadValidationError`（整次拒绝，列出双方 client_ref/project_id）；dry-run 写入 `errors` 且 `ok=False`；embedding 不可用时降级并追加 warning。
- [ ] **Step 5: 改文案**：三处「重复预检」改为「批内 exact/近重复查重（embedding 不可用时仅 exact）」。
- [ ] **Step 6: 测试通过 + Commit**：`feat(publish): detect intra-batch duplicate requirements`

### Task A3: 增量更新 + 版本校验 + no-op 语义

**Files:**
- Modify: `src/mem_lake/knowledge/repository.py`（`update_node` 约 `:380`；新增 `VersionConflictError`）
- Modify: `src/mem_lake/gateway/tools/_shared.py`（`build_update_node_item`）
- Modify: `src/mem_lake/approval/validation.py`（`_validate_item_payload`）
- Modify: `src/mem_lake/approval/executor.py`（`_execute_node_update`）
- Modify: `write_tools.py`（`_update_requirements_batch`）
- Modify: `src/mem_lake/gateway/tools/_shared.py`（`to_tool_error` 映射 `VersionConflictError`）
- Test: `tests/unit/test_update_requirements.py`、`tests/unit/test_approval_state_machine.py`、新建 `tests/unit/test_update_node_versioning.py`

**Interfaces:**
- `update_node(..., tags_add: list[str] | None = None, tags_remove: list[str] | None = None, properties_patch: dict[str, Any] | None = None, expected_version: int | None = None)`
- `build_update_node_item(..., tags_add=None, tags_remove=None, properties_patch=None, base_version: int | None = None)`
- `class VersionConflictError(Exception)`；`to_tool_error` → `ToolError(f"版本冲突: {exc}")`

- [ ] **Step 1: 写失败测试**：`properties_patch={}` 不 bump 版本；同值 patch 不 bump；`expected_version` 不符抛 `VersionConflictError`；patch 保留执行时并发写入的其他键；旧 payload（仅 `properties`）仍整体替换。
- [ ] **Step 2: 运行确认失败**。
- [ ] **Step 3: 实现 repository 变更**：`expected_version` 首查即校验；`properties_patch` 基于当前值合并；合并后与当前值相等则不写入 `changes`；`properties` 与 `properties_patch` 互斥。
- [ ] **Step 4: 扩展 item 契约与执行器**：payload 允许新键；`_execute_node_update` 透传；`update_requirements` 提交时只读合并用于 no-op 跳过，item 带增量 + `base_version=node.version`；保留「没有实际发生变更的节点」错误。
- [ ] **Step 5: 测试通过 + Commit**：`feat(update): incremental patches with optimistic version check`

### Task A4: update_requirements 批次归属

**Files:** `write_tools.py`（`_update_requirements_batch:456`）、`tests/unit/test_update_requirements.py`

**Interfaces:** 无新符号；行为约束：节点 `project_id` 集合唯一且非空 → 传入；全空 → `None`；混合/多值 → `PayloadValidationError`；批内 `system_id` 必须唯一。

- [ ] **Step 1: 写失败测试**：同项目 2 节点 → 批次 `project_id` 为该值；跨项目 → 报错含「按项目拆分」；全悬浮 → `None`；跨 system → 报错。
- [ ] **Step 2: 运行确认失败** → **Step 3: 实现** → **Step 4: 通过**。
- [ ] **Step 5: Commit**：`fix(update): attribute update batches to their single project`

### Task A5: 错误脱敏 + submission 归属

**Files:** `write_tools.py:445`、`review_tools.py:320`、`tests/unit/test_publish_requirements.py`、`tests/unit/test_review_submission.py`

**Interfaces:**
- 新增 `async def _assert_submission_ownership(submission_id: uuid.UUID, key_id: str) -> None`（写工具内部；查 `ApprovalBatch.submitted_by`，不一致抛 `PayloadValidationError`）。

- [ ] **Step 1: 写失败测试**：内部异常（`RuntimeError("secret dsn")`）在结果里只出现 `工具调用失败: RuntimeError`；他人 submission_id → 拒绝；自己的 → 通过。
- [ ] **Step 2: 确认失败** → **Step 3: `str(to_tool_error(e))` + 归属校验** → **Step 4: 通过**。
- [ ] **Step 5: Commit**：`fix(gateway): sanitize batch errors and verify submission ownership`

### Task A6: auto_process 对 update 的冲突检测

**Files:** `src/mem_lake/approval/policy.py`（`auto_process_batch:30`）、`tests/unit/test_auto_approval.py`

**Interfaces:** `auto_process_batch` 返回的 `conflict_hint` 增加 `checked_updates: int`；有冲突时 `decision="needs_human_review"` 且不写入。

- [ ] **Step 1: 写失败测试**：update item 把标题改成与现有节点近似 → `needs_human_review`，节点版本未变；纯 tags 更新 → `auto_approved` 且 `checked_updates=0`。
- [ ] **Step 2: 确认失败** → **Step 3: 实现**（预加载目标节点 → 计算预期 title/content/properties → 批量 embed → `detect_conflicts(exclude_node_id=...)`）→ **Step 4: 通过**。
- [ ] **Step 5: Commit**：`feat(approval): conflict-check content updates in auto mode`

---

## PR-B：事务、并发与性能

### Task B1: 审批期批量向量重算

**Files:** `approval/executor.py`、`knowledge/repository.py`（新增 `regenerate_node_vectors_batch`）、`review_tools.py`（两个 approve 路径）、`tests/unit/test_approval_state_machine.py`、`tests/unit/test_review_submission.py`

**Interfaces:**
- `async def regenerate_node_vectors_batch(session, *, nodes: list[KnowledgeNode], embedding_client) -> int`（内部调 `_write_facets_batch`）
- `_execute_node_update` 改为 `regenerate_vector=False`，返回 `(node, vector_dirty: bool)`。

- [ ] **Step 1: 写失败测试**：50 节点 update（mock embedding 计数）→ 只调用 `embed` 1 次；纯 tags 更新 → 0 次；title 变更 → 1 次且 `node_embedding` 行已更新。
- [ ] **Step 2: 确认失败** → **Step 3: 实现**（执行前后比较 title/content/properties；循环后一次批量重算）→ **Step 4: 通过**。
- [ ] **Step 5: Commit**：`perf(approval): batch facet regeneration for update batches`

### Task B2: 审批前节点存在性预检

**Files:** `approval/executor.py`、`tests/unit/test_approval_state_machine.py`

- [ ] **Step 1: 失败测试**：批次含已软删节点 → 抛 `PayloadValidationError` 且错误含该 node_id；任何节点都未写入。
- [ ] **Step 2–4: 实现 + 通过**（写入前一次 `IN` 查询，`is_deleted=False`）。
- [ ] **Step 5: Commit**：`fix(approval): preflight update targets before writing`

### Task B3: 审批行锁

**Files:** `approval/repository.py`（`get_batch_detail`、`get_pending_batch`）、`tests/unit/test_approval_state_machine.py`

**Interfaces:** `get_batch_detail(session, batch_id, *, for_update: bool = False)`；`get_pending_batch` 固定 `for_update=True`。

- [ ] **Step 1: 失败测试**：编译 SQL 断言含 `FOR UPDATE`（或双次调用第二次抛 `BatchStatusError` 的伪并发测试）。
- [ ] **Step 2–4: 实现 + 通过**（`with_for_update()` 只锁主行）。
- [ ] **Step 5: Commit**：`fix(approval): lock batch row during state transitions`

### Task B4: dry-run 批量 embed + 入队聚合

**Files:** `search/fusion.py:198`、`write_tools.py:555`、`_shared.py:submit_write_batch`、`review_tools.py`、`tests/unit/test_fusion.py`、`tests/unit/test_validate_requirements.py`、`tests/unit/test_publish_requirements.py`

**Interfaces:**
- `hybrid_search(..., query_vector: list[float] | None = None)`
- `submit_write_batch(..., enqueue_embed: bool = True)`

- [ ] **Step 1: 失败测试**：dry-run 50 条只 embed 1 次、`get_node_ids_with_vectors` 1 次；publish 50 条 lax 只入队 1 次/项目。
- [ ] **Step 2–4: 实现 + 通过**（一次批量 embed → 逐条 `hybrid_search(query_vector=...)`；created ids 按 project 聚合后入队）。
- [ ] **Step 5: Commit**：`perf(gateway): batch embeddings for validation and enqueue`

### Task B5: 批量 ref 解析 + external_id 索引（0006）

**Files:** `knowledge/repository.py`（新增 `find_requirements_by_refs`）、`write_tools.py:667-737`、`alembic/versions/0006_node_external_id_index.py`（新建）、`tests/unit/test_requirement_relations.py`、`tests/unit/test_alembic_migrations.py`

**Interfaces:**
- `async def find_requirements_by_refs(session, refs: list[str]) -> list[KnowledgeNode]`
- `async def _resolve_requirement_refs(refs: list[str]) -> dict[str, uuid.UUID]`

- [ ] **Step 1: 失败测试**：50 条关系只开 1 个 session、2 条 SQL；歧义 ref 报错文案不变；迁移 upgrade/downgrade 幂等。
- [ ] **Step 2–4: 实现 + 通过**（UUID 直接解析；其余 `requirement_key IN` + `properties->>'external_id' IN`）。
- [ ] **Step 5: Commit**：`perf(relations): batch requirement ref resolution with external_id index`

### Task B6: review_submission keyset 分页 + get_submission 总数

**Files:** `review_tools.py:227`、`approval/repository.py`（`list_pending_batches` 增 `batch_ids`、`order_asc`）、`tests/unit/test_review_submission.py`

**Interfaces:**
- `review_submission(..., after_batch_id: uuid.UUID | None = None, limit: int = 200)`
- 输出新增 `has_more: bool`、`remaining: int`
- `get_submission(..., limit: int = 200, offset: int = 0)`；`total` 为真实 `count()`，新增 `returned`

- [ ] **Step 1: 失败测试**：指定 `batch_ids` 不在前 200 条仍被处理；`has_more` 正确；`total` 与 `returned` 区分。
- [ ] **Step 2–4: 实现 + 通过**（SQL 层 `id.in_(batch_ids)`；keyset `(submitted_at, id)`）。
- [ ] **Step 5: Commit**：`fix(review): stable pagination for submissions`

### Task B7: 分数口径 `score_kind`

**Files:** `search/fusion.py:42/99`、`write_tools.py:627`、`tests/unit/test_fusion.py`、`tests/unit/test_validate_requirements.py`

**Interfaces:** `SearchResult.score_kind: str | None`（`"vector"` / `"fulltext"`）；`ValidateRequirementItemOutput.conflicts[]` 增加 `score_kind`。

- [ ] **Step 1: 失败测试**：全文精确命中（无向量）不被阈值丢弃且 `score_kind="fulltext"`；向量命中仍按阈值过滤。
- [ ] **Step 2–4: 实现 + 通过**（`_apply_vector_scores` 标记；filter 按 kind 分支）。
- [ ] **Step 5: Commit**：`fix(search): distinguish vector and fulltext scores`

### Task B8: manage_system list 批量化

**Files:** `gateway/tools/manage_tools.py:632`、`tests/unit/test_gateway_tools_manage.py`

- [ ] **Step 1: 失败测试**：3 个 system 只执行 1 次 `system_project` 查询（mock session 计数）。
- [ ] **Step 2–4: 实现 + 通过**（一次 `IN` + 分组）。
- [ ] **Step 5: Commit**：`perf(manage): batch system project lookups`

---

## PR-C：运维脚本安全

### Task C1: 批次删除保护

**Files:** `scripts/cleanup_test_leftovers.py:270-295`、`tests/unit/test_cleanup_script_guard.py`

- [ ] **Step 1: 失败测试**：pending 批次（`target_id=NULL`, `project_id=NULL`）不进入删除集；边批次（无 target_id）不进入；保留 system 的已审批批次不进入。
- [ ] **Step 2–4: 实现 + 通过**（`status != pending_review` + `project_id IS NOT NULL` + payload 级 `from_ref/to_ref/node_id` 文本比对）。
- [ ] **Step 5: Commit**：`fix(cleanup): never delete pending or floating approval batches`

### Task C2: 快照覆盖受保护项目 + dry-run 告警

**Files:** `scripts/cleanup_test_leftovers.py:171-200/391-420`、`tests/unit/test_cleanup_script_guard.py`

- [ ] **Step 1: 失败测试**：`protected_project_ids` 的节点/向量/审计/图顶点计入快照；dry-run 在 pending 待删数 >0 时输出告警行。
- [ ] **Step 2–4: 实现 + 通过** → **Step 5: Commit**：`fix(cleanup): snapshot protected projects and warn on pending batches`

### Task C3: 重索引任务保护复用 protected_node_ids

**Files:** `scripts/cleanup_test_leftovers.py:296-308`、`tests/unit/test_cleanup_script_guard.py`

- [ ] **Step 1: 失败测试**：`project_id=None`、`target_node_ids` 指向保留项目节点的任务不删。
- [ ] **Step 2–4: 实现 + 通过** → **Step 5: Commit**：`fix(cleanup): protect reindex tasks targeting kept project nodes`

---

## PR-D：文档、测试与迁移

### Task D1: approval_batch 索引名对齐（0007）

**Files:** `approval/models.py:40-45/84-96`、`alembic/versions/0007_rename_approval_batch_submission_index.py`（新建）、`tests/unit/test_alembic_migrations.py`

- [ ] **Step 1: 失败测试**：元数据索引名为 `idx_approval_batch_submission`；0007 upgrade/downgrade 幂等（存在 `ix_` 则 rename）。
- [ ] **Step 2–4: 实现 + 通过** → **Step 5: Commit**：`chore(db): align approval_batch submission index name`

### Task D2: user_hint 文案

**Files:** `gateway/tools/manage_tools.py:151-155/373-377`、`tests/unit/test_gateway_tools_manage.py`

- [ ] **Step 1: 失败测试**：`CreateAccessKeyOutput.model_fields["user_hint"].description` 含 `memlake://skills/` 且不含 `GitHub`；docstring 断言同上。
- [ ] **Step 2–4: 实现 + 通过** → **Step 5: Commit**：`docs(tools): update user_hint to MCP resources`

### Task D3: e2e 修复

**Files:** `tests/e2e/test_mcp_e2e.py`

- [ ] **Step 1: 改写**：场景 1 建 System；`publish_requirement` 传 `system_id`；`get_role_skills`→`get_skill`、`get_project_profile`→`get_project_info`；断言改 `content/version/uri/sha256` 与 `get_skill`。
- [ ] **Step 2: 静态校验**：`python -m py_compile tests/e2e/test_mcp_e2e.py`；grep 确认无已删工具名。
- [ ] **Step 3: Commit**：`test(e2e): align scenarios with current tool surface`

### Task D4: DEPLOYMENT / wiki 升级说明

**Files:** `DEPLOYMENT.md:98/119/183-192`、`docs/wiki/03-工具手册.md:43`、三个工具 docstring

- [ ] **Step 1**：补新装顺序（profile → add_projects → create_access_key）、admin 失去 8 个内容写工具、画像前置、`work_dir` 必填、0005/0006/0007 迁移。
- [ ] **Step 2**：工具数改 40；wiki 改「21 管理 + 11 查询 = 32」。
- [ ] **Step 3: Commit**：`docs: document breaking changes and migration steps`

---

## Self-Review

- **Spec coverage**：R1→A1，R2→A2，R3→A3，R4→B1，R5→C1，R6→A4；C7→B4，C8→B5，C9→B6，C10→B3，C11→A5，C12→A3，C13→A5，C14→A6，C15→B2，C16→B7，C17→C2，C18→C3，C19→B8；FYI-1→D1，FYI-2→D2，FYI-3→D3，FYI-4→D4，FYI-5→D4。
- **类型一致性**：`VersionConflictError`、`DedupeEntry`、`regenerate_node_vectors_batch`、`find_requirements_by_refs`、`score_kind` 在任务间签名一致。
- **Review Focus 对应测试**：A1（project-only 仍可写）、A3（旧 payload 回归）、A3（bind 校验）、C1（保留域）、B3（并发唯一成功）。