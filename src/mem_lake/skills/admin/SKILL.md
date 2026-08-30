---
name: mem-lake-admin
description: "Mem Lake administrator skills for approval workflow management, access key governance, and project profile maintenance. Use when managing pending approval batches, auto-processing conflicts, issuing or revoking access keys, or maintaining project profiles. Triggers on: 审批, 待审批, access key, 密钥, 项目画像, review_auto_process, 自动审批, review_pending, review_approve, review_reject, create_access_key, revoke_access_key, list_access_keys, update_access_key_scope, rotate_access_key, set_access_key_mode, manage_project_profile."
version: 1.5.0
---

# Admin Skills（管理员）

> 本文件参数表以工具实际签名（代码）为准；如与运行时 MCP 客户端展示不符，以代码为准。

## When to Use

当人类 admin 或其 Agent 需要执行以下操作时加载本 skill：
- 查看待审批批次队列
- 自动或手动审批知识写入批次
- 创建、吊销或查看 Access Key
- 维护项目画像（ProjectProfile）
- 了解冲突检测机制与审批决策依据

## Do NOT load for

- 发布需求或提交开发产物（使用 PM Skills / Dev Skills）
- 检索知识或查询需求上下文（使用通用查询工具，无需加载角色 skill）
- 代码实现或本地开发任务（本 skill 仅指导 Mem Lake 工具使用）

## 你的角色

你是当前项目的管理员。MemLake 是团队共享的知识记忆工具，你负责运维它（审批批次、签发/吊销 Access Key、维护项目画像与 system 域），让 PM/Dev 能稳定地检索与沉淀知识。你的核心职责：

1. **审批治理**：审查 PM/Dev 提交的批次，决定通过或拒绝
2. **自动审批**：对无冲突批次自动通过，有冲突批次向人类 admin 描述并等待决策
3. **密钥管理**：为团队成员签发或吊销 Access Key（绑定角色与项目范围），并维护审核模式
4. **画像维护**：直接写入项目画像节点（不走审批流）

关键原则：**你是 admin 的助手，不是决策者**。无冲突时可以自动通过（确定性判断），有冲突时必须向人类 admin 描述冲突详情并等待明确指令。**审批默认只调 `review_auto_process`；仅当其返回 `needs_human_review` 时，才用 `review_approve`/`review_reject` 落实人类 admin 的明确决策，不要先调手动审批跳过冲突检测。**

## 可用工具

### review_pending_list — 查询待审批批次队列

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID \| None | 否 | 项目 ID 过滤，None=所有项目 |
| limit | int | 否 | 返回数量上限，默认 50 |
| offset | int | 否 | 分页偏移，默认 0 |

返回：pending_review 状态批次列表（含 is_warning 超期预警 / is_timeout 已超期标记），每项含 batch_id, project_id, batch_type, submitted_by, status, item_count, created_at

### review_auto_process — 自动处理审批批次（核心工具）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| batch_id | UUID | 是 | 审批批次 ID |

返回 `AutoProcessOutput`：

| 字段 | 类型 | 说明 |
|------|------|------|
| decision | str | `auto_approved`（无冲突已自动通过）或 `needs_human_review`（有冲突需人工决策）|
| status | str | 批次最终状态（`approved` 或仍 `pending_review`）|
| conflict_hint | dict | 冲突检测详情（见下方）|
| summary | str | 批次摘要（向人类描述时使用）|
| batch_type | str | 批次类型 |
| submitted_by | str | 提交者 |
| item_count | int | 审批项数量 |

`conflict_hint` 结构（`needs_human_review` 时）：
```
{
    "has_conflict": True,
    "conflicting_nodes": [
        {
            "new_node_title": "...",
            "new_node_type": "Requirement",
            "existing_node_id": "uuid",
            "existing_node_title": "...",
            "similarity": 0.95,
            "matched_key_attrs": {},
            "conflict_type": "duplicate"  # duplicate | contradictory
        }
    ],
    "suggestion": "review"
}
```

### review_batch_detail — 查看批次完整内容

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| batch_id | UUID | 是 | 审批批次 ID |

返回：批次详情 + 所有审批项的完整节点内容（title, content, properties, tags）

### review_approve — 审批通过批次

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| batch_id | UUID | 是 | 审批批次 ID |
| review_comment | str | 否 | 审批意见（可选，记录到审计日志）|

`reviewed_by` 由网关根据当前调用者的 Access Key 自动填充，**无需也不能**显式传入。
返回：`ApprovalResultOutput`（batch_id, status="approved", reviewed_at, conflict_hint）

行为：原子写入——节点 + 边 + 向量 + 审计日志在同一事务提交。

### review_reject — 审批退回批次

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| batch_id | UUID | 是 | 审批批次 ID |
| review_comment | str | 是 | 拒绝原因（必填，记录到审计日志）|

`reviewed_by` 由网关自动填充，无需传入。
返回：`ApprovalResultOutput`（batch_id, status="rejected"）

### reindex_project_vectors — 异步重建项目向量

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 归属项目 ID |
| batch_size | int | 否 | 后台批量向量化每批节点数（默认 50）|

返回：`ReindexOutput`（project_id, task_id, reindexed, status）

行为：提交即返回任务 ID（task_id），真正的向量重嵌在**后台分批执行**，避免大项目同步执行导致的 MCP 调用超时。若同一项目已有 pending/running 任务，直接返回已有任务（防重入，避免重复全量重嵌）。提交后用 `get_reindex_status` 轮询进度。

### get_reindex_status — 查询重嵌任务进度

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| task_id | UUID | 是 | reindex_project_vectors 返回的任务 ID |

返回：`ReindexStatusOutput`（task_id, project_id, status, total, processed, reindexed, error, started_at, finished_at, created_at）

status 取值：`pending` / `running` / `done` / `failed`。`done` 表示全部向量已重建完成；`failed` 时 `error` 字段含失败原因。

### manage_system — 建立并签发 system 域（admin 专属）

PM 需求按 system 隔离；System 由 admin 统一建并签发。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| action | str | 是 | `create` / `list` / `set_projects` / `bind_keys` |
| name | str | create 时必填 | 系统域名（唯一）|
| description | str \| None | create 时可选 | 系统域描述 |
| system_id | UUID | set_projects/bind_keys 时必填 | 目标 System ID |
| project_ids | list[UUID] | set_projects 时 | 该系统下归属的 project 列表（决定 dev 对悬浮需求的可见性）|
| key_ids / role_filter / grant_all | - | bind_keys 时 | 定位目标 Key（优先级 key_ids > role_filter > grant_all）|

- `create`：建 System，返回 system_id
- `list`：枚举所有 System（含其下项目数）
- `set_projects`：定义 system↔project 归属
- `bind_keys`：把该系统授权给目标 Key（进入其 scope.systems）

### create_access_key — 创建 Access Key

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| role | str | 是 | 绑定角色：`admin` / `pm` / `dev` |
| project_scope | list[UUID] | 是 | 项目范围限制（admin 传空列表 `[]` 表示不受限）|
| lax_mode | bool | 否 | 初始审核模式：`true`=宽松（免审批直接入库），`false`=严格；默认 `false` |

返回 `CreateAccessKeyOutput`：`key_id` + `plaintext`（明文仅此一次，需安全保存）+ `role` + `project_scope` + `lax_mode` + `mcp_config` + `onboarding_prompt`。

### revoke_access_key — 吊销 Access Key

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| key_id | UUID | 是 | 目标 Access Key ID |

返回 `RevokeAccessKeyOutput`：`key_id` + `status="revoked"`。吊销后该 Key 立即失效，不可恢复。

### list_access_keys — 查看 Access Key 列表

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| role | str \| None | 否 | 按角色过滤：`admin` / `pm` / `dev` |
| status_filter | str \| None | 否 | 按状态过滤：`active` / `revoked` |
| lax_mode | bool \| None | 否 | 按审核模式过滤：`true`=宽松 / `false`=严格 |

返回 `AccessKeyListOutput`：`{items: list[AccessKeyOutput], total: int}`（每项含 role / project_scope / status / lax_mode / created_at / revoked_at）。

### update_access_key_scope — 改项目范围

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_scope | list[UUID] | 是 | 新的项目范围（grant_all_projects=true 时留空列表 `[]` 表示不受限）|
| key_ids | list[UUID] \| str | 否 | 显式指定一个或多个目标 Key ID（接受 UUID 列表，或逗号/空格分隔的字符串）|
| role_filter | str | 否 | 按角色批量（如 `"dev"` → 所有 dev Key）|
| grant_all_projects | bool | 否 | `true` 时作用于全部 Key |

返回 `AccessKeyListOutput`：`{items: list[AccessKeyOutput], total: int}`（items=受影响 Key 列表）；三种定位优先级 `key_ids` > `role_filter` > `grant_all_projects`；三者均未指定时为空操作（items 为空列表、total=0）。

### rotate_access_key — 轮换密钥

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| key_id | UUID | 是 | 目标 Access Key ID |

返回 `CreateAccessKeyOutput`：`key_id` + `plaintext`（新明文仅此一次，旧明文立即失效）+ `role` + `project_scope` + `lax_mode` + `mcp_config` + `onboarding_prompt`。Key ID 不变，用于密钥疑似泄露时主动作废。

### set_access_key_mode — 改审核模式

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| lax_mode | bool | 是 | 新的审核模式：`true`=宽松（免审批直接入库），`false`=严格 |
| key_ids | list[UUID] \| str | 否 | 显式指定一个或多个目标 Key ID（接受 UUID 列表，或逗号/空格分隔的字符串）|
| role_filter | str | 否 | 按角色批量（如 `"dev"` → 所有 dev Key）|
| grant_all_projects | bool | 否 | `true` 时作用于全部 Key |

返回 `AccessKeyListOutput`：`{items: list[AccessKeyOutput], total: int}`（items=受影响 Key 列表，含新的 lax_mode）；三种定位优先级同 `update_access_key_scope`。

`set_access_key_mode`/`update_access_key_scope` 三种定位方式优先级：`key_ids` > `role_filter` > `grant_all_projects`。
注：宽松模式仅在 Key 标记 lax_mode=true **且**全局开关 `LAX_MODE_ENABLED=true` 时对提交方生效；宽松由冲突检测把关（无冲突直接入库，有冲突停到队列）。当前**没有** `expires_in_days` 参数，密钥默认长期有效；轮换（rotate）可主动作废旧密钥。

### manage_project_profile — 直接写入项目画像（不走审批）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | create 时可省略 | 项目 ID；省略时服务端自动生成新项目 ID 并通过出参 `project_id` 返回 |
| action | str | 是 | `create` / `update` |
| profile | dict | 是 | 画像内容：title, content, properties（必填 name/description/tech_stack/architecture 等；可选 work_dir/团队/repo）, tags |
| node_id | UUID | update 时必填 | 现有 ProjectProfile 节点 ID |

返回：`ManageProjectProfileOutput`（**project_id**, node_id, action, status="approved", version）

特殊：admin 专属，直接写入 ProjectProfile 节点，状态直接 approved，不产生审批批次。
`project_id` 在 create 时若省略，服务端生成并返回，调用方无需预先自行生成 UUID；
`get_project_info` 列表中的 `name` 优先取 `properties.name`（缺省回退画像 title）。
`work_dir` / `repo` 为可选元数据，登记后 `get_project_info` 会回显，用于自证隔离与定位。

### get_project_info — 枚举/查询项目画像（三角色共享）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| action | str | 是 | `list`（枚举当前 key 可见项目）/ `get`（查单个） |
| project_id | UUID | get 时必填 | 项目 ID |
| include_profile | bool | 否 | true 时附完整画像属性（properties） |
| include_scope_meta | bool | 否 | true 时附 scope 自证（scope_type/visible_count/visible_uuids） |

返回：`action` + `projects`（list）/ `project`（get）+ 可选 `scope`。
list 时 admin 枚举全量项目，pm/dev 仅返回 scope 内项目；get 时 pm/dev 访问 scope 外项目返回权限拒绝。
`include_scope_meta=true` 回显 key 可见范围，是自证项目隔离边界的载体。

### get_role_skills — 获取角色 Skills 文档

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| role | str | 否 | 指定角色，None 返回当前调用者角色 |

返回：role, skills_markdown, version, installation_guide

### get_project_profile — 查询项目画像（三角色共享）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 项目 ID |

返回：项目最新的 ProjectProfile 节点；项目尚未创建画像时 `profile=None`，可调用 `manage_project_profile`（admin）创建。

何时用：需要读取项目技术栈/架构/约定/团队等画像元信息时。
何时不用：仅需列举可见项目范围时用 `get_project_info`，无需取完整画像。

示例：
```
get_project_profile(project_id="...")
→ {"profile": {"title": "...", "properties": {...}}} 或 {"profile": None}
```

### get_requirement_context — 查询需求上下文（三角色共享）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| requirement_id | UUID | 是 | 需求节点 ID |
| depth | int | 否 | 关系链遍历深度（1=直接关联，2=间接关联，最大 5），默认 2 |

返回：基于图遍历获取需求节点的关联节点列表（关联的代码/方案/意图/踩坑节点 + 关系链），按深度排序返回。

何时用：需一次性了解某需求关联的代码、方案、设计意图、踩坑记录及其关系链时。
何时不用：仅需检索相似需求时用 `search_similar_requirements`；需求节点不存在时 `requirement=None`、related_nodes 为空。

示例：
```
get_requirement_context(requirement_id="...", depth=2)
→ {"requirement": {...}, "related_nodes": [...关联节点按深度排序]}
```

### list_knowledge — 分页列出项目知识节点（Admin）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 项目 ID |
| node_type | str \| None | 否 | 节点类型过滤（None 表示所有类型）|
| status | str \| None | 否 | 状态过滤：`approved`（默认，仅已审批）/ `archived`（仅已归档）/ None（所有状态，含已归档）|
| limit | int | 否 | 返回数量上限，默认 100 |
| offset | int | 否 | 分页偏移，默认 0 |

返回：项目知识节点列表（按时间倒序）。

何时用：直接查看项目下所有节点（不走融合检索），排查已归档或特定类型节点时。
何时不用：按语义检索研发资产用 `search_code_snippets`（Dev 工具）。

示例：
```
list_knowledge(project_id="...", status=None)   # 含已归档
list_knowledge(project_id="...", node_type="Pitfall", status="approved")
```

### query_audit_log — 查询审计日志（Admin）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID \| None | 否 | 项目 ID 过滤（None 表示所有项目）|
| actor | str \| None | 否 | 操作者 Access Key ID 过滤 |
| action | str \| None | 否 | 操作类型过滤：`write` / `update` / `archive` |
| target_type | str \| None | 否 | 目标类型过滤：`node` / `edge` |
| target_id | UUID \| None | 否 | 目标 ID 过滤 |
| start_time | datetime \| None | 否 | 起始时间（ISO 8601）|
| end_time | datetime \| None | 否 | 结束时间（ISO 8601）|
| limit | int | 否 | 返回数量上限，默认 100 |
| offset | int | 否 | 分页偏移，默认 0 |

返回：审计日志记录列表（多条件过滤 + 分页）。审计日志为 append-only，记录所有知识图谱写操作。

何时用：需要追溯某 key/某节点/某时间段的所有知识写入操作时（如宽松模式入库审计、密钥轮换审计）。
何时不用：仅需查看待审批批次用 `review_pending_list`。

示例：
```
query_audit_log(project_id="...", actor="key-uuid", action="update")
query_audit_log(target_id="node-uuid", start_time="2026-08-01T00:00:00")
```

## 工作流

### 场景一：常规审批处理（推荐自动审批优先）

```
1. review_pending_list(limit=50, offset=0)
   → 获取待审批批次队列

2. 对每个批次调用 review_auto_process(batch_id=...)
   ├─ decision="auto_approved"
   │   → 告知人类："批次 {batch_id}（{summary}）已自动审批通过，无冲突"
   │   → 无需进一步操作，批次已写入知识图谱
   │
   └─ decision="needs_human_review"
       → 向人类 admin 描述冲突详情（见下方"冲突描述模板"）
       → 等待人类明确回复"通过"或"拒绝"
        ├─ 人类回复"通过" → review_approve(batch_id, review_comment="同意，无冲突")
        └─ 人类回复"拒绝" → review_reject(batch_id, review_comment="...")
```

### 场景二：人工审查特定批次

```
1. review_batch_detail(batch_id=...)
   → 查看批次内所有审批项的完整内容

2. 基于内容判断是否通过
    ├─ review_approve(batch_id, review_comment="同意")
    └─ review_reject(batch_id, review_comment="...")
```

### 场景三：密钥管理

```
# 新成员入职，签发 PM 角色 Access Key
create_access_key(role="pm", project_scope=[project_uuid])

# 成员离职，吊销密钥
list_access_keys()  → 找到对应 key_id
revoke_access_key(key_id="...")

# 动态改范围：把所有 dev Key 授权到新项目
update_access_key_scope(project_scope=[new_project_uuid], role_filter="dev")

# 一键全项目：所有 Key 不受限
update_access_key_scope(project_scope=[], grant_all_projects=true)

# 轮换某 Key 密钥（旧明文立即失效，新明文仅返回一次）
rotate_access_key(key_id="...")

# 把某 dev Key 设为宽松（免审批直接入库）；key_ids 指 key_id_list
set_access_key_mode(lax_mode=true, key_ids=["dev-key-uuid"])

# 按角色批量把全部 dev 设为宽松
set_access_key_mode(lax_mode=true, role_filter="dev")

# 关闭某 Key 的宽松（改回严格审批）
set_access_key_mode(lax_mode=false, key_ids=["dev-key-uuid"])

# 全局熔断：.env 设 LAX_MODE_ENABLED=false 并重启后，所有 Key 的宽松标记都不生效
```

### 宽松模式治理

- **开启/关闭**：`set_access_key_mode(lax_mode=true/false, ...)`，定位方式同 update_access_key_scope（key_ids > role_filter > grant_all_projects），可单 key 或按角色/全部批量。
- **即时生效**：认证每请求查库，改完立即作用于下一次提交，无需重启（全局开关除外）。
- **混合并存**：同一项目可同时有宽松 Key（提交即入库）与严格 Key（走审批），互不影响。
- **全局熔断**：`LAX_MODE_ENABLED=false`（.env + 重启）时即便 Key 标记宽松也强制走审批，用于紧急收紧。
- **仍是审批把关**：宽松提交仍走三层冲突检测，无冲突才直接入库；有冲突返回 needs_human_review 停在队列，admin 用 review_auto_process/review_* 照常处理。
- **审计可追溯**：宽松直接入库会落一条 status=approved 的批记录 + approve（detail 标 lax）审计，`query_audit_log` 可查 update_mode/approve；存量 pending 批次与已入库节点不会因切换模式而迁移或回滚。

### 场景四：项目画像维护

```
# 新项目接入，创建画像（不传 project_id，由服务端自动生成并返回）
manage_project_profile(
    action="create",
    profile={
        "title": "xxx 服务",
        "content": "...",
        "properties": {
            "name": "xxx 服务",
            "description": "...",
            "tech_stack": ["Python", "FastAPI", "PostgreSQL"],
            "architecture": "monolith"
        },
        "tags": []
    }
)
# 出参 project_id 即服务端生成的项目 ID，后续需求/产物提交复用它
```

## 冲突检测机制

`review_auto_process` 使用三层检测判断是否有冲突：

| 层级 | 检测内容 | 不冲突条件 |
|------|---------|-----------|
| L0 硬判定 | 类型关键标识字段精确匹配（直接查库，不依赖向量） | 同项目同类型下关键标识字段**完全相同**（如相同 name+file_path）→ 直接判冲突（duplicate），升级人工（Requirement 无 L0/L2 关键标识字段，仅走 L3） |
| L1 硬门控 | 项目 + 节点类型 | 不同项目或不同类型 → 直接通过 |
| L2 关键属性 | 类型特有标识字段 | 向量召回候选中关键属性不同（如不同 name）→ 排除（Requirement 不适用） |
| L3 内容语义 | build_embed_text（标题+正文+关键属性段）向量相似度 | 相似度 < 0.85（CONFLICT_SIMILARITY_THRESHOLD，已实测标定） → 直接通过 |

各节点类型的关键标识字段：

| 节点类型 | 关键标识字段 | 含义 |
|----------|-------------|------|
| Requirement | 无（规范主键为服务端分配的 requirement_key） | 仅按 L3 内容相似度判重（≥ 0.85） |
| CodeSnippet | name + file_path | 不同名称或路径 = 不同代码片段 |
| Solution | approach | 不同方案 = 不同解决方案 |
| DesignIntent | rationale | 不同设计理由 = 不同设计意图 |
| Decision | decision_id | 不同 ID = 不同决策 |
| Pitfall | symptom | 不同症状 = 不同坑 |
| ProjectProfile | name | 不同项目名 = 不同项目画像 |

阈值依据：`CONFLICT_SIMILARITY_THRESHOLD`（默认 0.85，由配置驱动）。该值随嵌入模型变化需重新标定：换模型后用 `scripts/calibrate_conflict_threshold.py` 按真实数据实测（推荐 query-doc 运行时模式，加 `--embedding-url`）。2026-08-22 已按 Qwen3-Embedding-0.6B 实测标定（query-doc 模式，跨 MemLake/ReqRadar 两项目）：相关不同实体最高 0.772、同实体改写最低 0.914，0.85 落在空隙内且余量均衡，维持不变。

## 冲突描述模板

当 `decision="needs_human_review"` 时，按以下模板向人类 admin 描述：

```
批次 {batch_id}（{summary}）检测到 {N} 个冲突节点，需要人工审查：

1. 节点「{new_node_title}」（{new_node_type}）
   与已有节点「{existing_node_title}」冲突
   - 相似度: {similarity}
   - 匹配属性: {matched_key_attrs}
   - 冲突类型: {conflict_type}（duplicate=疑似重复 / contradictory=疑似矛盾）

建议：{suggestion}

是否通过此批次？（通过/拒绝）
```

## 常见陷阱

1. **默认用 review_auto_process，不要跳过它直接 review_approve**：审批的默认入口是 `review_auto_process`——它先跑三层冲突检测，无冲突即 `auto_approved` 直接入库，有冲突才 `needs_human_review` 交人工。**手动 `review_approve`/`review_reject` 只在 `review_auto_process` 返回 `needs_human_review`、且人类 admin 给出明确指令后才使用**，直接 approve 会跳过冲突检测。

2. **Access Key 明文仅创建时返回一次**：`create_access_key` 返回的 `plaintext` 不会再次显示，必须首次返回时即安全保存。丢失后只能吊销重建。

3. **review_reject 必须填 review_comment**：拒绝原因会写入审计日志（append-only），用于追溯。空字符串会被拒绝。

4. **已审批批次不能再次审批**：`review_auto_process` / `review_approve` / `review_reject` 都会校验 `status == pending_review`，对已审批批次调用会返回错误。

5. **manage_project_profile 是直接写入**：不走审批流，不产生 batch_id，状态直接 approved。这是 admin 专属权限，PM/Dev 无权调用。

6. **冲突检测的硬判定**：相同关键标识字段（如代码片段的 name+file_path）一律判为重复冲突并升级人工审批，**不依赖内容相似度**（L0 硬判定；Requirement 无关键标识字段，判重依赖内容语义相似度）。若人类 admin 通过 `review_batch_detail` 确认确为重复，应手动 `review_reject` 并说明原因；若确认无冲突，可手动 `review_approve`。

7. **rotate 轮换密钥**：`rotate_access_key` 返回的 `plaintext` 是新明文，仅返回一次，旧明文立即失效。用于密钥疑似泄露时主动作废，无需吊销重建（Key ID 不变）。

8. **update_scope 改范围而非轮换**：仅调整 Key 的 `project_scope`（单/多个 key、按角色批量、或 `grant_all_projects` 一键全项目），不影响密钥明文本身；若要作废密钥请用 `rotate` 或 `revoke`。三者均未指定时为空操作（返回空列表）。

9. **宽松模式生效需两者同时为真**：某 Key 标记 lax_mode=true 仅当其提交时才免审批，且全局开关 `LAX_MODE_ENABLED` 必须为 true（否则一律走审批）。宽松下仍有冲突会停在 pending，不是"宽松即可绕过冲突检测"——冲突检测始终是质量门禁。

## 示例

### 示例 1：自动审批无冲突批次

```
Admin Agent: 收到通知，有新批次待审批
→ review_pending_list(limit=50, offset=0)
← 返回 1 个批次: batch_id=abc-123, summary="REQ-001 用户登录需求", submitted_by="pm"

→ review_auto_process(batch_id="abc-123")
← decision="auto_approved", status="approved"

Admin Agent → 人类: "批次 abc-123（REQ-001 用户登录需求）已自动审批通过，无冲突。"
```

### 示例 2：有冲突需人工决策

```
→ review_auto_process(batch_id="def-456")
← decision="needs_human_review", conflict_hint={
    "conflicting_nodes": [{
        "new_node_title": "用户登录功能需求",
        "existing_node_title": "用户登录功能需求",
        "similarity": 0.96,
        "matched_key_attrs": {},
        "conflict_type": "duplicate"
    }]
  }

 Admin Agent → 人类: "批次 def-456 检测到 1 个冲突节点：
   节点「用户登录功能需求」与已有节点「用户登录功能需求」冲突
   相似度: 0.96（内容相似，无关键属性匹配）
   冲突类型: duplicate（疑似重复）
   建议: review
   是否通过此批次？"

 人类: "拒绝，这是重复提交"
 → review_reject(batch_id="def-456", review_comment="重复提交")
```
