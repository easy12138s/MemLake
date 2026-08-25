---
name: mem-lake-pm
description: "Mem Lake product manager skills for publishing and managing requirement nodes in the team knowledge graph. Use when creating new requirements, updating requirement relationships (supersede/relate), or managing requirement versions. Triggers on: 需求发布, publish_requirement, 需求关系, update_requirement_relations, 需求替代, 需求关联, requirement, PRD."
version: 1.3.0
---

# PM Skills（产品经理）

> 本文件参数表以工具实际签名（代码）为准；如与运行时 MCP 客户端展示不符，以代码为准。

## When to Use

当 PM 或其 Agent 需要执行以下操作时加载本 skill：
- 发布新需求到团队知识图谱
- 更新需求间关系（替代、关联、冲突）
- 管理需求版本演进
- 了解需求节点的必填字段与提交格式

## Do NOT load for

- 审批批次或管理 Access Key（使用 Admin Skills）
- 提交代码片段/方案/踩坑（使用 Dev Skills）
- 检索知识或查询需求上下文（使用通用查询工具，无需加载角色 skill）

## 你的角色

你是 Mem Lake 的 PM Agent。Mem Lake 是团队共享的知识记忆层，你发布的需求默认进入审批队列，admin 审批通过后正式写入知识图谱，供全团队所有 Agent 检索使用。若你的 Access Key 被设为宽松模式（lax_mode=true 且全局开关开启），发布会直接入库（返回 status="approved"），无需等到 admin。

核心价值：**让你的需求理解被团队所有 AI 共享**。没有 Mem Lake，你的需求文档只存在你的 AI 会话里；有了 Mem Lake，开发者的 AI 能直接检索到你定义的需求上下文。

关键原则：你只负责提交，不负责审批。默认提交后获得 batch_id，等待 admin 审批通过；宽松模式下返回 approved 即已生效。

## 可用工具

### publish_requirement — 发布需求节点

system 维度：`system_id` 必填；`project_id` 可选（None=悬浮，表示"先于实现/跨项目落地"的需求）。

`requirement` 为嵌套结构，包含 `title` / `content` / `properties`；`related` 仅含 `supersedes` / `relates_to`（无 conflicts_with）。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| system_id | UUID | 是 | 归属 system 域（需求按 system 隔离）|
| requirement | dict | 是 | 嵌套需求体（见下方结构）|
| project_id | UUID | 否 | 归属项目；省略/None=悬浮需求（跨项目建模）|
| related | dict | 否 | 版本关系与关联关系 `{"supersedes": [...], "relates_to": [...]}`（引用的需求须同 system 或对调用者可见）|
| operation_id | str | 否 | 幂等键，同 operation_id 重复提交返回首次结果 |

**requirement 结构**：

```python
{
    "title": "用户登录功能需求",
    "content": "支持邮箱+密码登录，含记住我功能...",
    "properties": {
        "requirement_id": "REQ-001",   # 必填
        "priority": "P0",              # 必填 P0/P1/P2/P3
        "module": "auth",              # 必填
        "acceptance_criteria": "...",  # 必填
        # 可选：source_doc, version
    },
    "tags": ["auth", "login"]          # 可选
}
```

**related 结构**（可选，只含下列两项）：

```python
{
    "supersedes": ["REQ-000"],      # 替代旧需求 ID 列表
    "relates_to": ["REQ-002"]       # 关联需求 ID 列表
}
```

返回：`batch_id` + `status`（"pending_review" 或 "approved"；宽松模式已入库时 status="approved" + decision="auto_approved"，有冲突时 status="pending_review" + decision="needs_human_review"）。node_id 直到审批通过才回填。

### update_requirement_relations — 更新需求间关系

批量添加需求节点间的关系边，审批通过后写入知识图谱。使用嵌套的 `relations` 列表，`from_id` / `to_id` 必须为已有 Requirement 节点的 UUID。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 项目 ID |
| relations | list[dict] | 是 | 关系列表，每项 `{"from_id": UUID, "to_id": UUID, "relation_type": str, "properties": {}}` |
| operation_id | str | 否 | 幂等键，同 operation_id 重复提交返回首次结果 |

**relation_type 枚举**：`conflicts_with` / `duplicates` / `relates_to` / `supersedes` / `version_of`

返回：`batch_id` + `status`（"pending_review" 或 "approved"；宽松模式已入库时 status="approved" + decision="auto_approved"）。

行为：产生审批批次，审批通过后写入 AGE 图边（Cypher CREATE）。

### submit_dev_artifacts — 批量提交开发产物

Dev 工具，PM 也可了解其签名以便与开发协作对齐。审批通过后产物节点写入知识图谱。`artifacts` 为嵌套结构，包含 `code_snippets` / `solutions` / `design_intents` / `pitfalls`。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 归属项目 ID |
| artifacts | dict | 是 | 嵌套产物体（见下方结构）|
| relations | list[dict] | 否 | 产物间关系，每项 `{"from_ref": str, "to_ref": str, "relation_type": str, "properties": {}}` |
| requirement_id | UUID | 否 | 关联需求节点 ID（自动为每个 CodeSnippet 建立 implements 边）|
| operation_id | str | 否 | 幂等键 |

**artifacts 结构**：

```python
{
    "code_snippets": [
        {"ref":"LoginService", "title":"...", "content":"...",
         "properties": {"name":"...", "type":"class/function/module/component",
                        "responsibility":"...", "file_path":"...",
                        # 可选 signature/snippet/language
                        }, "tags":["..."]}
    ],
    "solutions": [
        {"ref":"SolutionA", "title":"...", "content":"...",
         "properties": {"version":"v1", "approach":"采用的方案",
                        # 可选 alternatives
                        }, "tags":["..."]}
    ],
    "design_intents": [
        {"ref":"IntentA", "title":"...", "content":"...",
         "properties": {"rationale":"理由", "trade_offs":"权衡",
                        # 可选 references
                        }, "tags":["..."]}
    ],
    "pitfalls": [
        {"ref":"PitfallA", "title":"...", "content":"...",
         "properties": {"symptom":"症状", "root_cause":"根因", "solution":"解决方案",
                        # 可选 severity(P0-P3)
                        }, "tags":["..."]}
    ]
}
```

**relations 的 relation_type 枚举**：`implements` / `depends_on` / `realized_by` / `embodies` / `traces_to` / `described_by` / `references`

返回：`batch_id` + `status`（"pending_review" 或 "approved"；宽松模式已入库时 status="approved" + decision="auto_approved"）。

### update_node — 修正已审批节点

PM/Dev 共享。修正已写入知识图谱（审批通过）的错误节点内容；节点不存在/不属于本项目/已归档则拒绝。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 节点所属项目 ID |
| node_id | UUID | 是 | 要更新的已审批节点 UUID |
| title | str | 否 | 新标题；留空不更新 |
| content | str | 否 | 新正文；留空不更新 |
| properties | dict | 否 | 新属性字典，整体替换原属性；留空不更新 |
| tags | list | 否 | 新标签列表；留空不更新 |
| operation_id | str | 否 | 幂等键 |

- **何时用**：发现已审批入库的节点内容写错、需要修正时（版本号 +1、重新生成向量、写审计日志）。
- **何时不用**：尚未提交/审批中的内容用原发布工具重提；想新增知识用 submit_dev_artifacts 或 publish_requirement。

```python
update_node(
    project_id="proj-uuid",
    node_id="node-uuid",
    content="更正后的正文...",
    tags=["auth", "login"]
)
```

### search_similar_requirements — 检索相似需求

PM/Dev 共享。向量+全文融合检索相似需求（Requirement 类型；按 system 或 project 隔离），仅检索 approved 状态。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| query | str | 是 | 查询文本（需求描述/关键词）|
| system_id | UUID | 否 | 归属 system 域（与 project_id 至少其一必填）|
| project_id | UUID | 否 | 归属项目 ID |
| top_n | int | 否 | 返回数量上限，默认 10 |
| tags | list[str] | 否 | 标签过滤 |
| tags_op | str | 否 | 标签匹配语义：`all`=AND（默认）/`any`=OR |
| min_score | float | 否 | 向量余弦相似度下限（0~1），默认 0.5；None 关闭阈值 |
| semantic_tags | bool | 否 | 标签语义扩展，默认 False |

- **何时用**：你想"找出某类需求 / 某功能有哪些需求"时。
- **何时不用**：要拿某需求的**关联代码/方案/意图**用 `get_requirement_context`；要做**变更影响分析**用 `analyze_impact_scope`。
- 检索**需求节点(Requirement)**。要找"某类需求/某功能有哪些需求"用我；要拿某需求的**关联代码/方案/意图**用 get_requirement_context，要做**变更影响分析**用 analyze_impact_scope。

```python
search_similar_requirements(
    query="用户登录 OAuth 第三方登录",
    system_id="sys-uuid",
    top_n=10
)
```

### analyze_impact_scope — 分析变更影响范围

PM/Dev 共享。从需求出发做**变更影响范围**遍历（需求→代码→依赖→方案→意图）。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 归属项目 ID |
| requirement_id | UUID | 是 | 需求节点 ID |
| max_depth | int | 否 | depends_on 依赖链遍历深度，默认 5 |

- **何时用**：评估"改这个需求会影响哪些代码/方案/意图"。
- **何时不用**：仅看某需求的直接关联节点用 `get_requirement_context`。
- 从需求出发做**变更影响范围**遍历(需求→代码→依赖→方案→意图)。用于"改这个需求会影响哪些代码"；仅看直接关联用 get_requirement_context。

```python
analyze_impact_scope(
    project_id="proj-uuid",
    requirement_id="req-uuid",
    max_depth=5
)
```

### get_requirement_context — 查询需求上下文

PM/Dev/Admin 共享。给定需求 UUID，返回其**关联节点**（代码/方案/意图/踩坑）及关系链。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| requirement_id | UUID | 是 | 需求节点 ID |
| depth | int | 否 | 关系链遍历深度（1=直接关联，2=间接，最大 5），默认 2 |

- **何时用**：已知需求 UUID，想看它关联了哪些代码/方案/意图/踩坑。
- **何时不用**：先经 `search_similar_requirements` 拿到需求 UUID；泛搜需求用 search_similar_requirements。
- 给定需求 UUID，返回其**关联节点**(代码/方案/意图/踩坑)及关系链。先经 search_similar_requirements 拿到需求 UUID；泛搜需求用 search_similar_requirements。

```python
get_requirement_context(
    requirement_id="req-uuid",
    depth=2
)
```

### check_requirement_conflicts — 排查需求冲突

PM 工具。基于向量相似度检测某需求是否与库内需求**重复/矛盾**（同项目同类型高相似度节点）。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 归属项目 ID |
| requirement_id | UUID | 是 | 被检测的需求节点 ID（自动排除自身）|
| threshold | float | 否 | 相似度阈值（0~1），None 用配置默认 0.85 |
| top_n | int | 否 | 检索召回数量上限，默认 20 |

- **何时用**：发布前主动排查某需求是否与已有需求重复/矛盾。
- **何时不用**：审批阶段的冲突门禁（L2 关键属性比对）由 admin 审批流负责，二者互补。
- 主动排查某需求是否与库内需求**重复/矛盾**(向量相似度)。仅 PM 用；与审批流冲突检测互补。

```python
check_requirement_conflicts(
    project_id="proj-uuid",
    requirement_id="req-uuid"
)
```

### get_project_profile — 查询项目画像

PM/Dev/Admin 共享。返回项目最新的 ProjectProfile 节点（技术栈/架构/约定/团队）。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 项目 ID |

- **何时用**：提交/检索前想了解项目技术栈、架构约定。
- **何时不用**：想枚举可见项目列表用 `get_project_info`。

```python
get_project_profile(project_id="proj-uuid")
```

### get_project_info — 枚举/查询项目画像

PM/Dev/Admin 共享。list 枚举当前 key 可见的项目；get 按 project_id 查询单个。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| action | str | 是 | `list` 枚举可见项目 / `get` 查询单个项目 |
| project_id | UUID | 否 | get 时必填的项目 ID |
| include_profile | bool | 否 | 是否附完整画像属性，默认 False |
| include_scope_meta | bool | 否 | 是否附 scope 自证信息，默认 False |

- **何时用（PM）**：想确认自己可被哪些项目访问、或查看项目基本信息。
- **何时不用**：只查单个项目技术栈细节用 `get_project_profile`。

```python
get_project_info(action="list", include_scope_meta=True)
```

### get_role_skills — 获取角色 Skills 文档

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| role | str | 否 | 指定角色，None 返回当前调用者角色 |

返回：role, skills_markdown, version, installation_guide

## 工作流

### 场景一：发布新需求

```
1. 准备需求信息
   - 确认 project_id（从项目配置获取）
   - 编写 title, content
   - 填充 properties（requirement_id, priority, module, acceptance_criteria）
   - 可选：添加 tags

 2. publish_requirement(
        system_id=sys_uuid,
        requirement={
            "title": "用户登录功能需求",
            "content": "支持邮箱+密码登录，含记住我功能...",
            "properties": {
                "requirement_id": "REQ-001",
                "priority": "P0",
                "module": "auth",
                "acceptance_criteria": "1. 邮箱登录成功 2. 错误密码提示..."
            },
            "tags": ["auth", "login"]
        }
    )
    ← 返回 batch_id, status="pending_review"

3. 告知 PM："需求 REQ-001 已提交，批次 {batch_id} 待 admin 审批"
```

### 场景二：需求版本演进（替代旧需求）

```
# REQ-002 是 REQ-001 的升级版
publish_requirement(
    system_id=sys_uuid,
    requirement={
        "title": "用户登录功能需求 V2",
        "content": "在 V1 基础上增加 OAuth 第三方登录...",
        "properties": {
            "requirement_id": "REQ-002",
            "priority": "P0",
            "module": "auth",
            "acceptance_criteria": "1. V1 所有功能 2. OAuth 登录..."
        }
    },
    related={
        "supersedes": ["REQ-001"]  # 声明替代关系
    }
)
```

### 场景三：更新已有需求关系

```
# 发现 REQ-001 与 REQ-005 有关联
update_requirement_relations(
    project_id=uuid,
    relations=[
        {"from_id": "REQ-001", "to_id": "REQ-005",
         "relation_type": "relates_to", "properties": {}}
    ]
)
← 返回 batch_id, status="pending_review"
```

## 常见陷阱

1. **requirement_id 必须全局唯一**：同一项目内不能有重复的 requirement_id。如果发布时与已有需求冲突，admin 审批阶段会检测到（内容相似度 ≥ 0.85 或相同 requirement_id 硬键命中）。

2. **supersedes/relates_to 中的 ID 必须已存在**：引用的 requirement_id 必须是知识图谱中已审批通过的节点。引用不存在的 ID 会导致审批失败。

3. **提交后不可修改**：批次一旦提交，内容不可修改。如需修改，只能等 admin 拒绝后重新提交，或在 admin 审批通过后发布新版本（用 supersedes 关系）。

4. **properties 字段缺失会被 schema 校验拒绝**：publish_requirement 在工具层即校验 properties 必填字段，缺失会直接返回错误（不会进入审批队列）。

5. **不要假设提交即生效（严格模式下）**：严格模式下 status="pending_review" 意味着需求尚未写入知识图谱，其他 Agent 此时检索不到，需等待 admin 审批通过。**例外**：若你的 Key 为宽松模式，提交返回 `status="approved"` + `decision="auto_approved"` 说明已直接入库、可被检索；若返回 `decision="needs_human_review"` 则说明有冲突，批次停在队列需 admin 处理。

6. **content 应足够详细**：content 会用于向量生成（f"{title}\n{content}"），内容越详细，检索准确性越高。避免只写一句话描述。

## 示例

### 示例 1：发布 P0 需求

```
PM: "把用户登录需求录入 Mem Lake"

→ publish_requirement(
    system_id="sys-uuid-001",
    requirement={
        "title": "用户登录功能需求",
        "content": """支持邮箱+密码登录，含记住我功能。
        登录成功后跳转到首页，失败时提示错误原因。
        连续 5 次失败锁定账号 30 分钟。""",
        "properties": {
            "requirement_id": "REQ-001",
            "priority": "P0",
            "module": "auth",
            "acceptance_criteria": "1. 邮箱+密码登录成功 2. 记住我功能 3. 失败提示 4. 锁定机制"
        },
        "tags": ["auth", "login", "security"]
    }
)
← batch_id="abc-123", status="pending_review"

PM Agent → 人类: "需求 REQ-001 已提交，批次 abc-123 待 admin 审批通过后生效。"
```

### 示例 2：需求版本演进

```
PM: "登录需求升级到 V2，增加 OAuth"

→ publish_requirement(
    system_id="sys-uuid-001",
    requirement={
        "title": "用户登录功能需求 V2",
        "content": "在 V1 基础上增加 Google/GitHub OAuth 第三方登录...",
        "properties": {
            "requirement_id": "REQ-002",
            "priority": "P0",
            "module": "auth",
            "acceptance_criteria": "1. V1 所有功能 2. OAuth 登录 3. 账号绑定"
        }
    },
    related={"supersedes": ["REQ-001"]}
)
← batch_id="def-456"

PM Agent → 人类: "需求 REQ-002 已提交，声明替代 REQ-001，批次 def-456 待审批。"
```
