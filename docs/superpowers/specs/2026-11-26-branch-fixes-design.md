# 分支评审问题修复设计（兼容优先 / 方案 A）

- 日期：2026-11-26
- 状态：待批准
- 输入：`docs/plan/2026-11-26-branch-review-audit.md`（审计记录，含 3 个复核子智能体的确认与新增项）
- 目标分支：`master`（HEAD `850b3d5`）

## 1. 意图与为什么

修复评审发现的 6 条 Required、13 条 Consider、5 条 FYI，重点是：

1. 堵住跨 system 越权读写（R1）与批内重复发布（R2）；
2. 消除 `update_requirements` 跨审批窗口的属性丢失更新（R3）与超长审批事务（R4）；
3. 让清理脚本不再误删真实 pending/边批次（R5）；
4. 恢复审批批次的项目归属与可观测性（R6）；
5. 一并处理事务/并发/性能、错误脱敏、迁移与文档测试漂移。

**硬约束（用户确认）**：所有优化不得影响现有数据使用。落地解释为：

- **schema 只做增量**：只新增可空列/索引（本次实际只需索引），不修改/删除列，不回填、不重写历史行；
- **旧审批 payload 可继续执行**：升级前已落库的 pending 批次保持原语义（快照式更新、无版本校验），不会被新逻辑拒绝；
- **旧 Key 不被一刀切禁用**：鉴权修复采用「`system_scope ∪ project_scope 所隶属的 system`」作为允许集（方案 A），project-only 的存量 PM Key 在其项目所属 system 内行为不变；
- **查询/检索兼容**：旧数据无向量、旧批次无新字段时按现有降级路径处理（`vector_ready` 已存在）。
- **两个生产 system 域零损伤（最高优先级，2026-11-26 用户追加）**：「中方诊药云系统」（`System.code` 为 NULL，按 `name` 匹配）与「MemLake」下的全部节点/边/向量/审计/计数器/重索引任务**不得删除或改写**。落地要求：
  1. 清理脚本保持默认 dry-run，`--keep-system` 未指定即报错退出（现有保护），本次改动只允许**更保守**（pending / 悬浮 / 边批次一律保留），绝不放宽任何删除条件；
  2. 0006 迁移只做索引新建/改名，无列变更、无数据回写、无 `UPDATE`/`DELETE`；
  3. 需要 `--execute` 的破坏性操作只由用户在生产库执行，我不执行；实现与测试全部使用隔离的测试库；
  4. 交付时提供「保留域快照」对比命令（节点/向量/审计/图顶点/计数器/重索引任务计数），用于执行前后核对两个 system 完全一致；
  5. 单元测试固化：清理计划对 keep-system 节点、以及 `project_id ∈ 保留 system 关联项目` 的节点恒为空集。

### 1.1 备选方案与取舍（Approaches）

| 方案 | 做法 | 优点 | 代价 |
| --- | --- | --- | --- |
| **A. 兼容优先（采纳）** | 允许集 = `system_scope ∪ project_scope 所隶属的 system`；R3 新批次用增量 payload + `base_version`，旧快照 payload 保留原执行路径；0006 仅索引 | 不打断存量 Key 与 pending 批次；每个 PR 可独立回滚 | project-only 的 Key 在其项目所属 system 内仍可写（比纯 `system_scope` 宽），需在升级文档说明；升级前已存在的 pending 快照批次仍可能丢更新（新批次不会） |
| B. 严格优先 | 只认 `system_scope`；R3 全面改增量，存量 pending 批次标记为需重提 | 安全口径最干净、代码分支最少 | 存量 project-only Key 升级后立刻写入失败；正在审批的批次需重提，直接影响现有数据使用（与硬约束冲突） |
| C. 灰度开关 | 配置 `AUTH_SCOPE_COMPAT` / `UPDATE_PAYLOAD_COMPAT` 双模式，默认兼容 | 可灰度、可回滚 | 两套路径长期并存，测试矩阵翻倍，配置漂移风险高 |

**选择 A**（2026-11-26 用户确认「按你的推荐来实现」）。理由：它是唯一同时满足「修复越权与丢更新」和「不影响现有数据使用」的选项；B 会破坏存量流程，C 的成本大于收益，且 A 的实现本身已可通过 revert 单个 PR 回滚。

## 2. 非目标 / 反目标

- 不做数据库重建、不要求重新导入、不做历史数据修正；
- 不改变已审批节点的内容/版本；不重跑历史审批；
- 不引入新的外部依赖；
- 反目标：为了“兼容”而保留越权（例如 `system_id` 完全放行）或保留静默丢更新；安全修复不以破坏存量流程为代价，但不放弃修复本身。

## 3. 方案总览（4 个可独立回滚的 PR）

| PR | 主题 | 覆盖结论 |
| --- | --- | --- |
| A | 授权与数据正确性 | R1、R2、R3、R6、C11、C12、C13、C14 |
| B | 事务、并发与性能 | R4、C7、C8、C9、C10、C15、C16、C19 |
| C | 运维脚本安全 | R5、C17、C18 |
| D | 文档、测试与迁移 | FYI-1～5（含 e2e 修复、DEPLOYMENT、wiki） |

每个 PR 独立测试、独立可回退；A/B 不依赖 D；B 的 0006 迁移与 A 的代码无耦合。

---

## 4. PR-A 授权与数据正确性

### A1. R1：system 写/读鉴权（兼容模式）

**新增（`gateway/dependencies.py` 或 `gateway/access.py`）**

- `async def allowed_write_systems() -> set[uuid.UUID]`：
  - admin：返回空集并配合 `is_admin` 旁路；
  - 非 admin：`system_scope` ∪ 对 `project_scope` 中每个 project 查 `system_project` 得到的 system 集合（一次 `IN` 查询）。
- `async def validate_system_write_access(system_id)`：
  - admin 直接放行；
  - 目标 system 不存在 → `ToolError("system 不存在: ...")`（阻止 `SYS-` 幽灵序列）；
  - `system_id ∉ allowed` → `ToolError`，文案给出修复路径（`manage_system add_projects` 挂载项目 / `manage_system bind_keys` 授权 system）；
  - 供 `publish_requirement`、`publish_requirements`、`validate_requirements` 使用；`update_requirement_relations` 继续用两端节点可见性，不新增 system 参数。

**project∈system 校验**

- 在 `_publish_requirements_batch` / `_validate_requirements_core` 预检中，对非空 `project_id` 批量查 `get_system_project_ids`，不在清单则拒绝（dry-run 收进 `errors`，publish 整次拒绝），文案复用 `_ADD_PROJECTS_FIX`。
- `publish_requirement` 同步补；`_validate_requirements_core` 的 project 查询合并为一次。

**兼容性**：允许集由 project_scope 派生，存量 project-only Key 在其项目所属 system 内不失效；跨 system 写入被拒（这是修复目标）。升级文档提示：若某 Key 需要写入未挂载的 system，需 admin 先 `add_projects`/`bind_keys`。

**测试**：unit 覆盖 allowed 集派生、admin 旁路、幽灵 system、project 不属于 system；integration 覆盖 PM 跨 system 被拒、自己 system 通过。

### A2. R2：批内确定性查重

**共享 helper（`gateway/tools/write_tools.py` 或新 `_batch_dedupe.py`）**

- `normalize_fingerprint(title, content)`：NFKC + lower + 合并空白。
- `async def find_intra_batch_duplicates(items, embedding_client, threshold) -> dict[int, list[dict]]`：
  1. 先按规范化指纹分组，组内 >1 → exact duplicate；
  2. 若 `embedding_client` 可用：一次 `embed([build_embed_text(...) for item])`，两两余弦（仅 n≤50，O(n²) 可接受），`>= CONFLICT_SIMILARITY_THRESHOLD` 记为 near duplicate；
  3. embedding 失败时降级为仅 exact（返回 `degraded=True`）。
- 同批 `properties.external_id` 重复 → 视为 exact duplicate。

**接入**

- `_validate_requirements_core`：每条 item 的 `errors`/`warnings` 带上「与本批 client_ref=X 重复/高度相似」，重复项 `ok=False`。
- `_publish_requirements_batch`：预检阶段发现 exact 或 near duplicate → `PayloadValidationError`（整次拒绝，不产生批次）；降级模式只拦 exact，并在响应 `warnings`（新增字段）中说明未做语义查重。
- 修正文案：`write_tools.py` 工具描述、`docs/wiki/03-工具手册.md:13`、`skills/pm/SKILL.md:70` 明确「批内 exact/近重复查重（embedding 降级时仅 exact）」。

**兼容性**：只影响新提交；旧 pending 批次不重检。误报风险由阈值控制，并在错误文案给出 client_ref 对，PM 可拆分/合并后重提。

### A3. R3 + C12：增量更新与版本校验（旧 payload 双读）

**payload 契约扩展（`node+update` 可选键）**

- `tags_add: list[str] | None`
- `tags_remove: list[str] | None`
- `properties_patch: dict[str, Any] | None`
- `base_version: int | None`

旧键（`title`/`content`/`properties`/`tags`/`source`）保持原义，仍可单独使用。

**`build_update_node_item`** 增加上述可选参数；`_validate_item_payload` 增加：node+update 至少有一个旧字段或 patch 字段；patch 字段类型校验；`base_version` 为正整数。

**`repository.update_node`** 增加 `expected_version`、`tags_add`、`tags_remove`、`properties_patch`：

- 若 `expected_version is not None and node.version != expected_version` → 抛新异常 `VersionConflictError(node_id, expected, actual)`（`to_tool_error` 映射为可执行文案，整批回滚转人工重提）；
- 在**执行时**基于当前节点合并 patch，再 `validate_node`；
- 只有合并结果与当前值不同才进入 `changes`（C12：no-op 不入 changes、不 bump 版本、不重算向量）；
- 旧参数路径（`properties` 整体替换）保持现状。

**`approval/executor._execute_node_update`** 透传新键。

**`update_requirements`**：不再在提交时合并快照，改为构造 patch item + `base_version=node.version`（提交时读节点仅用于存在性/可见性/版本采集）。

**`update_node` 工具**：新增可选 `expected_version`（默认 None=不校验），供调用方选择乐观锁。

**兼容性**：旧 pending 批次无 patch/无 base_version → 走原逻辑（快照替换、无校验）；新批次才有新语义。升级不需要重提。

**测试**：旧 payload 回归（现有 `test_approval_flow` 保持）；patch 合并保留并发修改；版本冲突整批回滚且文案含 node_id；no-op 不 bump 版本。

### A4. R6：批次项目归属

`_update_requirements_batch` 加载节点后：

- 非空 `project_id` 去重集合：
  - 1 个 → 作为 `submit_write_batch(project_id=...)`；
  - >1 个，或「部分为空 + 部分非空」→ `PayloadValidationError("请按项目拆分（跨项目需求不能同一批更新）")`；
  - 全为空（全部悬浮）→ `project_id=None`。
- 同时校验批内 `system_id` 唯一；不一致直接拒绝。

新工具无存量待审批数据，行为收紧安全。

### A5. C11 / C13 / C14

- **C11**：`_publish_requirements_batch` / `_review_submission_core` 的 per-item `error` 改为 `str(to_tool_error(e))`（保留 batch_id/client_ref 上下文），未知异常由 `to_tool_error` 记日志并泛化。
- **C13**：`_publish_requirements_batch` 使用调用方 `submission_id` 前，查该 `submission_id` 下已有批次的 `submitted_by`（`ApprovalBatch.submitted_by`）；存在且与当前 `key_id` 不一致 → 拒绝；不存在 → 采用并归属当前 Key；不改变服务端生成的默认路径。
- **C14**：`auto_process_batch` 对「会改变 title/content/properties（含 patch）」的 update item 执行冲突检测（批量 embed、`exclude_node_id=node_id`、用合并后的 prospective properties）；有冲突 → `needs_human_review`（不写入）。`review_approve` 保持写入后提示，但批次 `conflict_hint` 增加 `conflict_checked: true` 标识；纯标签更新批次标记 `conflict_checked: false`。

**兼容性**：只影响处理中的批次与新批次；不重写历史。auto-process 对 update 更严格（更安全），可在升级说明中注明。

---

## 5. PR-B 事务、并发与性能

### B1. R4：审批期批量向量重算

- `review_approve`：update items 先以 `regenerate_vector=False` 应用并记录「title/content/properties 实际变更」的节点；循环结束后调用一次 `repository.regenerate_node_vectors_batch(session, nodes, embedding_client)`（复用 `_write_facets_batch`，一次 embed）。
- `auto_process_batch` 走同一路径（复用 review_approve）。
- 旧快照 update item 同样适用（它们也会触发 changes），行为等价、仅批量。
- create 路径不变（仍后台补向量）。
- 兼容性：审批返回后向量在**同一事务内**已更新，无窗口；不改变 search 语义。

### B2. C15：先预检、后写入

- `review_approve`/`auto_process_batch` 执行前：一次 `IN` 查询装载全部 node+update 目标，校验存在、approved、未软删；缺失/归档 → `PayloadValidationError` 列出 node_id（在任何写操作之前）。
- 不引入 per-item savepoint，保持批次 all-or-nothing；错误信息精确到节点。

### B3. C10：审批行锁

- `get_batch_detail(..., for_update=False)`；`get_pending_batch(..., for_update=True)` 默认在审批入口加 `with_for_update()`（只锁 `approval_batch` 主行，`selectinload` items 不锁）。
- `review_approve`、`review_reject`、`auto_process_batch` 三者统一走加锁入口；`review_batch_detail` 只读不加锁。
- 兼容性：无数据变更；仅改变并发行为（后来者收到 `BatchStatusError`）。

### B4. C7：批量 dry-run 与入队聚合

- `search/fusion.hybrid_search` 增加可选 `query_vector`：提供时跳过 `embed_one`，vector 引擎直接消费；其余不变。
- `_validate_requirements_core`：一次性 `embed([build_embed_text(...)])`，逐条 `hybrid_search(query_vector=...)`；候选 `vector_ready` 一次性 `IN` 查询（不再每条开 session）。
- `submit_write_batch(..., enqueue_embed: bool = True)`；`_publish_requirements_batch` 传 False，循环结束把同一 submission 的 created ids 聚合为一次 `_safe_enqueue_embed`（project_id=None 也支持）。
- `_review_submission_core`：跨批次聚合 created ids，循环结束后一次入队。
- `review_submission` 增加 `limit`（默认 200）/`offset`，串行处理，不做并发（事务清晰优先）；`has_more` 见 C9。

### B5. C8：批量 ref 解析 + external_id 索引

- 新增 `resolve_requirement_refs(refs: list[str]) -> dict[str, uuid.UUID]`：单 session；UUID 形态直接走 UUID 分支；其余按 `requirement_key IN (...)` 与 `properties->>'external_id' IN (...)` 两条查询合并；多义/不存在沿用现有错误文案。
- `_submit_requirement_relations` 一次性解析全部 `from_ref/to_ref`，去掉逐端点开 session。
- 0006 迁移新增表达式索引 `CREATE INDEX IF NOT EXISTS idx_node_external_id ON knowledge_node ((properties->>'external_id'))`（约 2 万行，短时构建；不阻塞后续读写语义）。

### B6. C9：截断与分页

- `list_pending_batches` 增加可选 `batch_ids: list[UUID] | None`，SQL 层 `IN` 过滤（`review_submission` 不再在 limit 后过滤）。
- `review_submission`：`limit/offset`，输出 `has_more`（limit+1 探测）。
- `get_submission`：`limit/offset`，`total` 改为独立 `count()`，并新增 `returned`；`total` 语义修正。
- 输出字段新增为可选，旧客户端不感知。

### B7. C16：分数口径

- `SearchResult`/`SearchItemOutput` 增加 `score_kind: "vector" | "fulltext"`（默认 None 兼容旧构造）。
- 冲突/阈值过滤逻辑：`vector_score is not None` → 用余弦阈值；`vector_score is None` 但全文命中 → **保留为候选**并标 `score_kind="fulltext"`（不再静默丢弃）。
- 影响：`check_requirement_conflicts`、`validate_requirements` 的冲突面可能增加（更保守），自动通过更少；在升级说明注明。

### B8. C19：`manage_system list` 批量化

- 一次 `SELECT system_id, project_id FROM system_project WHERE system_id IN (...)` 聚合，替换逐 System 查询。

---

## 6. PR-C 运维脚本安全

- **R5**：`_plan` 的批次删除条件增加：
  - `ApprovalBatch.status != 'pending_review'`（待审批一律不删）；
  - `ApprovalBatch.project_id.is_not(None)`（悬浮批次一律保留）；
  - 如仍需清理测试悬浮/边批次，再做 payload 级保护（`payload->>'from_ref'`/`to_ref`/`node_id`/`system_id`/`project_id` 与保留集文本比对）。
- **C17**：`_keep_snapshot` 纳入 `protected_project_ids` 的节点/向量/审计/图顶点；dry-run 增加按 `status` 的待删批次统计，`pending_review` 非 0 时打印告警。
- **C18**：任务保护的 `protected_ids` 复用 `protected_node_ids`（system ∪ protected project），不再 system-only。
- 兼容性：只影响脚本判定（更保守），不删除额外数据；dry-run 输出变化。

---

## 7. PR-D 文档、测试与迁移

- **FYI-1**：`ApprovalBatch.__table_args__` 改显式 `Index("idx_approval_batch_submission", "submission_id")`，去掉 `index=True`；0006 迁移用幂等 `DO` 块（`ix_approval_batch_submission_id` 存在则 `ALTER INDEX ... RENAME`，否则 `CREATE INDEX IF NOT EXISTS`），downgrade 反向 rename/create。仅索引变更。
- **FYI-2**：`CreateAccessKeyOutput.user_hint` 描述与 `create_access_key` docstring 改为资源 URI + `get_skill`。
- **FYI-3**：修复 `tests/e2e/test_mcp_e2e.py`：场景 1 先建 System；`publish_requirement` 传 `system_id`；改用 `get_skill`/`get_project_info`；断言改为 `content/version/uri/sha256`；`DEPLOYMENT.md:119` 工具数改 40（或写按角色计数）。
- **FYI-4**：`DEPLOYMENT.md` 升级章节补：admin 失去 8 个 PM/Dev 内容写工具、画像前置（含存量项目补建顺序）、`work_dir` 必填（含 update 需重填）、新装顺序（profile → add_projects → create_key）、0005/0006 迁移；三个工具 docstring 补前置条件。
- **FYI-5**：`docs/wiki/03-工具手册.md:43` 改为「21 管理 + 11 查询 = 32」。

---

## 8. 数据安全与迁移策略

- 0006 仅做索引（重命名 + 表达式索引），无列变更、无回填、无行重写；按 `IF EXISTS/IF NOT EXISTS` 幂等编写，可重复执行。
- 代码对旧 payload 保持双读；旧批次、旧 Key、旧节点无需任何操作。
- 所有新字段均为可选；响应模型新增字段均有默认值。
- 回滚：每个 PR 可单独 revert；0006 downgrade 恢复旧索引名（若原库是 `ix_` 形态）。
- 数据影响评估明确写入 PR 描述：`无列变更；仅索引与行为`。

## 9. Builder check 决议（2026-11-26，实现方决定）

1. **迁移归属**：拆为两个独立迁移 —— `0006_node_external_id_index`（PR-B/B5）与 `0007_rename_approval_batch_submission_index`（PR-D/FYI-1）。revision 链 `0005 → 0006 → 0007`，均幂等（`IF NOT EXISTS` / `DO` 块），每个 PR 可独立回滚。
2. **node+update 合法变更集**：允许 `title` / `content` / `properties` / `tags` / `source` / `project_id` / `tags_add` / `tags_remove` / `properties_patch`；`bind_requirement` 批次以 `node_id + project_id` 为合法组合；`base_version` 仅作校验，不计入变更集。
3. **批内查重范围**：整个 `publish_requirements` 调用（system 级）两两比较，不按 project 分组；非空 `external_id` 相同即 exact duplicate（跨 project 同样算重复）；错误/警告同时给出双方 `client_ref` 与 `project_id`。
4. **review_submission 分页**：不用 offset，改 keyset —— `after_batch_id` + 按 `(submitted_at asc, id asc)` 取 `limit+1` 判断 `has_more`；`batch_ids` 在 SQL 层 `IN` 过滤；另返回 `remaining` 计数。避免待审集合收缩导致跳批与饥饿。
5. **update_requirements 的 no-op**：保留「提交时跳过无实际变更节点」的现有语义 —— 提交时只读合并出预期值用于比较（不写快照），payload 仍只带增量 + `base_version`；全为 no-op 时仍抛「没有实际发生变更的节点」。现有单测与 docstring 语义不变。
6. **B1 变更集判定**：`_execute_node_update` 先 `get_node` 快照，执行后比较 title/content/properties 得出真实变更集（不依赖 C12 的等值比较），只对真实变更节点批量重算向量。

## 10. 测试与验证

- 单元：A1 allowed 集、A2 指纹/近重复（mock embedding）、A3 patch/版本/no-op/旧 payload、A4 拆批、A5 错误脱敏/归属/update 冲突、B1 批量向量、B3 加锁参数、B4 query_vector、B5 ref 批量解析、B6 分页、B7 score_kind、C 脚本判定（纯逻辑）。
- 集成（需库）：R1 跨/同 system、R3 并发覆盖与版本冲突、R4 单次 embed 断言（mock embedding 调用次数）、R5 守卫 SQL 语义、C9 截断、C13 归属。
- 回归：现有 unit 全量；`test_approval_flow` 旧 payload 用例；`test_lax_mode`。
- e2e：静态修复，需真实服务时再跑（当前无 CI DB）。
- 门禁：ruff；提交前按 AGENTS.md 跑 GitNexus `impact` 与 `detect_changes`。

## 11. 验收标准（每 PR）

- A：跨 system 读写被拒且文案可执行；同 system 存量 Key 行为不变；批内重复被拦；旧 pending 批次仍可审批并保留并发修改。
- B：审批 50 节点 update 只产生 1 次 facet embed；并发重复审批只有一个成功；dry-run 50 条不再每条 3 个 session；批次/提交接口可分页。
- C：dry-run 中 pending/悬浮/边批次不再进入待删集；快照能发现 protected project 资产变化。
- D：e2e 可启动并走通场景 1；文档与新行为一致；索引 autogenerate 无漂移。

## 12. 留给实施者的决定

- R2 near-duplicate 的阈值是否复用 `CONFLICT_SIMILARITY_THRESHOLD`（本设计采用复用；实现时可配置化）。
- B4 `hybrid_search` 增加 `query_vector` 的具体签名（保持向后兼容的默认 None）。
- C14 对 update 的冲突检测范围（仅 title/content/properties 变更；纯 tags 不检）。
- 0006 表达式索引是否并发构建（2 万行规模建议普通构建，避免 Alembic 事务限制；如线上表更大，改为 `postgresql_concurrently` + autocommit 迁移脚本）。