# Admin Reference（详细参数表与示例）

> 本文件为 Admin Skill 的参考文档，包含完整的工具参数表、冲突检测机制和详细工作流示例。
> 主文件 `SKILL.md` 包含核心指导原则和快速参考，需要详细参数时加载本文件。

---

## 工具详细参数

### review_pending_list — 查询待审批批次队列

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID \| None | 否 | 项目 ID 过滤，None=所有项目 |
| limit | int | 否 | 返回数量上限，默认 50 |
| offset | int | 否 | 分页偏移，默认 0 |

**返回**：pending_review 状态批次列表（含 is_warning 超期预警 / is_timeout 已超期标记），每项含 batch_id, project_id, batch_type, submitted_by, status, item_count, created_at

---

### review_auto_process — 自动处理审批批次（核心工具）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| batch_id | UUID | 是 | 审批批次 ID |

**返回 `AutoProcessOutput`**：

| 字段 | 类型 | 说明 |
|------|------|------|
| decision | str | `auto_approved`（无冲突已自动通过）或 `needs_human_review`（有冲突需人工决策）|
| status | str | 批次最终状态（`approved` 或仍 `pending_review`）|
| conflict_hint | dict | 冲突检测详情（见下方）|
| summary | str | 批次摘要（向人类描述时使用）|
| batch_type | str | 批次类型 |
| submitted_by | str | 提交者 |
| item_count | int | 审批项数量 |

**`conflict_hint` 结构**（`needs_human_review` 时）：

```python
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

---

### review_batch_detail — 查看批次完整内容

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| batch_id | UUID | 是 | 审批批次 ID |

**返回**：批次详情 + 所有审批项的完整节点内容（title, content, properties, tags）

---

### review_approve — 审批通过批次

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| batch_id | UUID | 是 | 审批批次 ID |
| review_comment | str | 否 | 审批意见（可选，记录到审计日志）|

`reviewed_by` 由网关根据当前调用者的 Access Key 自动填充，**无需也不能**显式传入。

**返回**：`ApprovalResultOutput`（batch_id, status="approved", reviewed_at, conflict_hint）

**行为**：原子写入——节点 + 边 + 向量 + 审计日志在同一事务提交。

---

### review_reject — 审批退回批次

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| batch_id | UUID | 是 | 审批批次 ID |
| review_comment | str | 是 | 拒绝原因（必填，记录到审计日志）|

`reviewed_by` 由网关自动填充，无需传入。

**返回**：`ApprovalResultOutput`（batch_id, status="rejected"）

---

### reindex_project_vectors — 异步重建项目向量

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 归属项目 ID |
| batch_size | int | 否 | 后台批量向量化每批节点数（默认 50）|

**返回**：`ReindexOutput`（project_id, task_id, reindexed, status）

**行为**：提交即返回任务 ID（task_id），真正的向量重嵌在**后台分批执行**，避免大项目同步执行导致的 MCP 调用超时。若同一项目已有 pending/running 任务，直接返回已有任务（防重入，避免重复全量重嵌）。提交后用 `get_reindex_status` 轮询进度。

---

### get_reindex_status — 查询重嵌任务进度

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| task_id | UUID | 是 | reindex_project_vectors 返回的任务 ID |

**返回**：`ReindexStatusOutput`（task_id, project_id, status, total, processed, reindexed, error, started_at, finished_at, created_at）

**status 取值**：`pending` / `running` / `done` / `failed`。`done` 表示全部向量已重建完成；`failed` 时 `error` 字段含失败原因。

---

### manage_system — 建立并签发 system 域（admin 专属）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| action | str | 是 | `create` / `list` / `set_projects` / `bind_keys` |
| name | str | create 时必填 | 系统域名（唯一）|
| description | str \| None | create 时可选 | 系统域描述 |
| system_id | UUID | set_projects/bind_keys 时必填 | 目标 System ID |
| project_ids | list[UUID] | set_projects 时 | 该系统下归属的 project 列表 |
| key_ids / role_filter / grant_all | - | bind_keys 时 | 定位目标 Key（优先级 key_ids > role_filter > grant_all）|

**action 说明**：
- `create`：建 System，返回 system_id
- `list`：枚举所有 System（含其下项目数）
- `set_projects`：定义 system↔project 归属
- `bind_keys`：把该系统授权给目标 Key（进入其 scope.systems）

---

### create_access_key — 创建 Access Key

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| role | str | 是 | 绑定角色：`admin` / `pm` / `dev` |
| project_scope | list[UUID] | 是 | 项目范围限制（admin 传空列表 `[]` 表示不受限）|
| lax_mode | bool | 否 | 初始审核模式：`true`=宽松，`false`=严格；默认 `false` |

**返回** `CreateAccessKeyOutput`：`key_id` + `plaintext`（明文仅此一次）+ `role` + `project_scope` + `lax_mode` + `mcp_config` + `onboarding_prompt`。

---

### revoke_access_key — 吊销 Access Key

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| key_id | UUID | 是 | 目标 Access Key ID |

**返回** `RevokeAccessKeyOutput`：`key_id` + `status="revoked"`。吊销后该 Key 立即失效，不可恢复。

---

### list_access_keys — 查看 Access Key 列表

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| role | str \| None | 否 | 按角色过滤 |
| status_filter | str \| None | 否 | 按状态过滤：`active` / `revoked` |
| lax_mode | bool \| None | 否 | 按审核模式过滤 |

**返回** `AccessKeyListOutput`：`{items: list[AccessKeyOutput], total: int}`

---

### update_access_key_scope — 改项目范围

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_scope | list[UUID] | 是 | 新的项目范围 |
| key_ids | list[UUID] \| str | 否 | 显式指定目标 Key ID |
| role_filter | str | 否 | 按角色批量 |
| grant_all_projects | bool | 否 | `true` 时作用于全部 Key |

**返回** `AccessKeyListOutput`；三种定位优先级 `key_ids` > `role_filter` > `grant_all_projects`

---

### rotate_access_key — 轮换密钥

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| key_id | UUID | 是 | 目标 Access Key ID |

**返回** `CreateAccessKeyOutput`：新明文仅此一次，旧明文立即失效。Key ID 不变。

---

### set_access_key_mode — 改审核模式

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| lax_mode | bool | 是 | 新的审核模式 |
| key_ids | list[UUID] \| str | 否 | 显式指定目标 Key ID |
| role_filter | str | 否 | 按角色批量 |
| grant_all_projects | bool | 否 | `true` 时作用于全部 Key |

**返回** `AccessKeyListOutput`；三种定位优先级同 `update_access_key_scope`

---

### manage_project_profile — 直接写入项目画像（不走审批）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | create 时可省略 | 项目 ID |
| action | str | 是 | `create` / `update` |
| profile | dict | 是 | 画像内容 |
| node_id | UUID | update 时必填 | 现有 ProjectProfile 节点 ID |

**返回**：`ManageProjectProfileOutput`（project_id, node_id, action, status="approved", version）

---

### list_knowledge — 分页列出项目知识节点

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 项目 ID |
| node_type | str \| None | 否 | 节点类型过滤 |
| status | str \| None | 否 | 状态过滤 |
| limit | int | 否 | 返回数量上限，默认 100 |
| offset | int | 否 | 分页偏移，默认 0 |

---

### query_audit_log — 查询审计日志

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID \| None | 否 | 项目 ID 过滤 |
| actor | str \| None | 否 | 操作者 Access Key ID 过滤 |
| action | str \| None | 否 | 操作类型过滤 |
| target_type | str \| None | 否 | 目标类型过滤 |
| target_id | UUID \| None | 否 | 目标 ID 过滤 |
| start_time | datetime \| None | 否 | 起始时间 |
| end_time | datetime \| None | 否 | 结束时间 |
| limit | int | 否 | 返回数量上限，默认 100 |
| offset | int | 否 | 分页偏移，默认 0 |

---

## 冲突检测机制

`review_auto_process` 使用三层检测判断是否有冲突：

| 层级 | 检测内容 | 不冲突条件 |
|------|---------|-----------|
| L0 硬判定 | 类型关键标识字段精确匹配 | 同项目同类型下关键标识字段完全相同 → 直接判冲突 |
| L1 硬门控 | 项目 + 节点类型 | 不同项目或不同类型 → 直接通过 |
| L2 关键属性 | 类型特有标识字段 | 向量召回候选中关键属性不同 → 排除 |
| L3 内容语义 | 向量相似度 | 相似度 < 0.85 → 直接通过 |

**各节点类型的关键标识字段**：

| 节点类型 | 关键标识字段 |
|----------|-------------|
| Requirement | 无（仅走 L3） |
| CodeSnippet | name + file_path |
| Solution | approach |
| DesignIntent | rationale |
| Pitfall | symptom |
| ProjectProfile | name |

---

## 冲突描述模板

当 `decision="needs_human_review"` 时，按以下模板向人类 admin 描述：

```
批次 {batch_id}（{summary}）检测到 {N} 个冲突节点，需要人工审查：

1. 节点「{new_node_title}」（{new_node_type}）
   与已有节点「{existing_node_title}」冲突
   - 相似度: {similarity}
   - 匹配属性: {matched_key_attrs}
   - 冲突类型: {conflict_type}（duplicate=疑似重复 / contradictory=疑似矛盾）

建议: {suggestion}

是否通过此批次？（通过/拒绝）
```

---

## 完整工作流示例

### 场景一：常规审批处理（自动审批优先）

```
Admin Agent: 收到通知，有新批次待审批

步骤 1：查看待审批队列
→ review_pending_list(limit=50, offset=0)
← 返回 2 个批次

步骤 2：对每个批次调用自动处理
→ review_auto_process(batch_id="abc-123")
← decision="auto_approved", status="approved"
Admin Agent → 人类: "批次 abc-123（REQ-001 用户登录需求）已自动审批通过，无冲突。"

→ review_auto_process(batch_id="def-456")
← decision="needs_human_review", conflict_hint={...}
Admin Agent → 人类: "批次 def-456 检测到 1 个冲突节点..."
等待人类回复...
人类: "拒绝" → review_reject(batch_id="def-456", review_comment="重复提交")
```

---

### 场景二：人工审查特定批次

```
步骤 1：查看批次详情
→ review_batch_detail(batch_id="abc-123")
← 返回批次详情 + 所有审批项内容

步骤 2：基于内容判断
→ review_approve(batch_id="abc-123", review_comment="内容准确，通过")
```

---

### 场景三：密钥管理

```
# 新成员入职
create_access_key(role="pm", project_scope=[project_uuid])

# 成员离职
list_access_keys() → 找到 key_id
revoke_access_key(key_id="...")

# 动态改范围
update_access_key_scope(project_scope=[new_uuid], role_filter="dev")

# 轮换密钥
rotate_access_key(key_id="...")

# 改审核模式
set_access_key_mode(lax_mode=true, key_ids=["dev-key-uuid"])
```

---

### 场景四：项目画像维护

```
# 新项目接入
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
# 出参 project_id 即服务端生成的项目 ID
```

---

## 返回值结构

### AutoProcessOutput

```python
{
    "decision": "auto_approved",     # 或 "needs_human_review"
    "status": "approved",            # 或 "pending_review"
    "conflict_hint": {...},          # 仅 needs_human_review 时
    "summary": "批次摘要",
    "batch_type": "write",
    "submitted_by": "key-uuid",
    "item_count": 1
}
```

### ApprovalResultOutput

```python
{
    "batch_id": "uuid",
    "status": "approved",            # 或 "rejected"
    "reviewed_at": "2026-09-07T10:00:00Z",
    "conflict_hint": {...}
}
```
