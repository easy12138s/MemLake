---
name: mem-lake-admin
description: "Mem Lake administrator skills for approval workflow management, access key governance, and project profile maintenance. Use when managing pending approval batches, auto-processing conflicts, issuing or revoking access keys, or maintaining project profiles. Triggers on: 审批, 待审批, access key, 密钥, 项目画像, review_auto_process, 自动审批, review_pending, review_approve, review_reject, manage_access_key, manage_project_profile."
version: 1.1.3
---

# Admin Skills（管理员）

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

你是 Mem Lake 的管理员 Agent。Mem Lake 是团队共享的知识记忆层，所有 PM 和 Dev 提交的知识写入都需要经过审批才能进入知识图谱。你的核心职责：

1. **审批治理**：审查 PM/Dev 提交的批次，决定通过或拒绝
2. **自动审批**：对无冲突批次自动通过，有冲突批次向人类 admin 描述并等待决策
3. **密钥管理**：为团队成员签发或吊销 Access Key（绑定角色与项目范围）
4. **画像维护**：直接写入项目画像节点（不走审批流）

关键原则：**你是 admin 的助手，不是决策者**。无冲突时可以自动通过（确定性判断），有冲突时必须向人类 admin 描述冲突详情并等待明确指令。

## 可用工具

### review_pending_list — 查询待审批批次队列

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 否 | 项目 ID，None 表示所有项目 |
| status | str | 否 | 批次状态过滤，默认 pending_review |
| page | int | 否 | 页码，默认 1 |
| page_size | int | 否 | 每页数量，默认 20 |

返回：批次列表（batch_id, project_id, batch_type, submitted_by, status, item_count, created_at）

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
            "matched_key_attrs": {"requirement_id": "REQ-001"},
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

### manage_access_key — 创建/吊销/查看/改范围/轮换 Access Key

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| action | str | 是 | `create` / `revoke` / `list` / `update_scope` / `rotate` |
| role | str | create 时必填 | 绑定角色：`admin` / `pm` / `dev` |
| project_scope | list[UUID] | create / update_scope 时必填 | 项目范围限制（admin 传空列表 `[]` 表示不受限）；update_scope 时为新的项目范围 |
| key_id | UUID | revoke / rotate 时必填 | 目标 Access Key ID |
| status_filter | str | list 时可选 | 按状态过滤：`active` / `revoked` |
| key_ids | list[UUID] \| str | update_scope 时可选 | 显式指定一个或多个目标 Key ID（接受 UUID 列表，或逗号/空格分隔的字符串，兼容客户端将数组序列化为字符串的场景）|
| role_filter | str | update_scope 时可选 | 按角色批量授权（如 `"dev"` → 所有 dev Key）|
| grant_all_projects | bool | update_scope 时可选 | `true` 时一键将全部 Key 授权为不受限（配合空 project_scope）|

返回：
- `create`：返回 `created.key_id` + `created.plaintext`（明文仅此一次，需安全保存）
- `revoke`：返回 `revoked_key_id`
- `list`：返回 `listed` 密钥列表（含 role / project_scope / status / created_at）
- `update_scope`：返回 `scoped` 受影响 Key 列表（同 `listed` 结构）；未指定任何定位方式时返回空列表（空操作）
- `rotate`：返回 `rotated.key_id` + `rotated.plaintext`（新明文仅此一次，旧明文立即失效）

`update_scope` 三种定位方式优先级：`key_ids` > `role_filter` > `grant_all_projects`。
注：当前**没有** `expires_in_days` 参数，密钥默认长期有效；轮换（rotate）可主动作废旧密钥。

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

## 工作流

### 场景一：常规审批处理（推荐自动审批优先）

```
1. review_pending_list(status="pending_review")
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
manage_access_key(action="create", role="pm", project_scope=[project_uuid])

# 成员离职，吊销密钥
manage_access_key(action="list")  → 找到对应 key_id
manage_access_key(action="revoke", key_id="...")

# 动态改范围：把所有 dev Key 授权到新项目
manage_access_key(action="update_scope", project_scope=[new_project_uuid], role_filter="dev")

# 一键全项目：所有 Key 不受限
manage_access_key(action="update_scope", project_scope=[], grant_all_projects=true)

# 轮换某 Key 密钥（旧明文立即失效，新明文仅返回一次）
manage_access_key(action="rotate", key_id="...")
```

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
| L0 硬判定 | 类型关键标识字段精确匹配（直接查库，不依赖向量） | 同项目同类型下关键标识字段**完全相同**（如相同 requirement_id）→ 直接判冲突（duplicate），升级人工 |
| L1 硬门控 | 项目 + 节点类型 | 不同项目或不同类型 → 直接通过 |
| L2 关键属性 | 类型特有标识字段 | 向量召回候选中关键属性不同（如不同 requirement_id）→ 排除 |
| L3 内容语义 | f"{title}\n{content}" 向量相似度 | 相似度 < 0.92 → 直接通过 |

各节点类型的关键标识字段：

| 节点类型 | 关键标识字段 | 含义 |
|----------|-------------|------|
| Requirement | requirement_id | 不同 ID = 不同需求 |
| CodeSnippet | name + file_path | 不同名称或路径 = 不同代码片段 |
| Solution | approach | 不同方案 = 不同解决方案 |
| DesignIntent | rationale | 不同设计理由 = 不同设计意图 |
| Decision | decision_id | 不同 ID = 不同决策 |
| Pitfall | symptom | 不同症状 = 不同坑 |
| ProjectProfile | name | 不同项目名 = 不同项目画像 |

阈值依据：`CONFLICT_SIMILARITY_THRESHOLD`（默认 0.85，由配置驱动）。该值随嵌入模型变化需重新标定（换模型后用样本对实测调整），0.85 起作为 Qwen3-Embedding-0.6B 的初始值（区分"相关"与"重复"）。

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

1. **不要跳过 review_auto_process 直接 review_approve**：自动审批的冲突检测是前置的，直接 approve 会跳过检测。正确流程是先 `review_auto_process`，仅在返回 `needs_human_review` 时才手动 `review_approve`。

2. **Access Key 明文仅创建时返回一次**：`manage_access_key(action="create")` 返回的 `plaintext` 不会再次显示，必须首次返回时即安全保存。丢失后只能吊销重建。

3. **review_reject 必须填 review_comment**：拒绝原因会写入审计日志（append-only），用于追溯。空字符串会被拒绝。

4. **已审批批次不能再次审批**：`review_auto_process` / `review_approve` / `review_reject` 都会校验 `status == pending_review`，对已审批批次调用会返回错误。

5. **manage_project_profile 是直接写入**：不走审批流，不产生 batch_id，状态直接 approved。这是 admin 专属权限，PM/Dev 无权调用。

6. **冲突检测的硬判定**：相同关键标识字段（如相同 requirement_id）一律判为重复冲突并升级人工审批，**不依赖内容相似度**（L0 硬判定）。若人类 admin 通过 `review_batch_detail` 确认确为重复，应手动 `review_reject` 并说明原因；若确认无冲突，可手动 `review_approve`。

7. **rotate 轮换密钥**：`manage_access_key(action="rotate")` 返回的 `plaintext` 是新明文，仅返回一次，旧明文立即失效。用于密钥疑似泄露时主动作废，无需吊销重建（Key ID 不变）。

8. **update_scope 改范围而非轮换**：仅调整 Key 的 `project_scope`（单/多个 key、按角色批量、或 `grant_all_projects` 一键全项目），不影响密钥明文本身；若要作废密钥请用 `rotate` 或 `revoke`。三者均未指定时为空操作（返回空列表）。

## 示例

### 示例 1：自动审批无冲突批次

```
Admin Agent: 收到通知，有新批次待审批
→ review_pending_list(status="pending_review")
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
        "matched_key_attrs": {"requirement_id": "REQ-001"},
        "conflict_type": "duplicate"
    }]
  }

Admin Agent → 人类: "批次 def-456 检测到 1 个冲突节点：
  节点「用户登录功能需求」与已有节点「用户登录功能需求」冲突
  相似度: 0.96，匹配属性: requirement_id=REQ-001
  冲突类型: duplicate（疑似重复）
  建议: review
  是否通过此批次？"

人类: "拒绝，这是重复提交"
→ review_reject(batch_id="def-456", review_comment="重复提交 REQ-001")
```
