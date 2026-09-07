# PM Reference（详细参数表与示例）

> 本文件为 PM Skill 的参考文档，包含完整的工具参数表、嵌套结构定义和详细工作流示例。
> 主文件 `SKILL.md` 包含核心指导原则和快速参考，需要详细参数时加载本文件。

---

## 工具详细参数

### publish_requirement — 发布需求节点

system 维度：`system_id` 必填；`project_id` 可选（None=悬浮，表示"先于实现/跨项目落地"的需求）。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| system_id | UUID | 是 | 归属 system 域（需求按 system 隔离）|
| requirement | dict | 是 | 嵌套需求体（见下方结构）|
| project_id | UUID | 否 | 归属项目；省略/None=悬浮需求（跨项目建模）|
| related | dict | 否 | 版本关系与关联关系（见下方结构）|
| operation_id | str | 否 | 幂等键，同 operation_id 重复提交返回首次结果 |

**requirement 结构**：

```python
{
    "title": "用户登录功能需求",
    "content": "支持邮箱+密码登录，含记住我功能...",
    "properties": {
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

**返回**：`batch_id` + `status`（"pending_review" 或 "approved"；宽松模式已入库时 status="approved" + decision="auto_approved"，有冲突时 status="pending_review" + decision="needs_human_review"）。node_id 直到审批通过才回填。

---

### update_requirement_relations — 更新需求间关系

批量添加需求节点间的关系边，审批通过后写入知识图谱。使用嵌套的 `relations` 列表，`from_id` / `to_id` 必须为已有 Requirement 节点的 UUID。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 项目 ID |
| relations | list[dict] | 是 | 关系列表，每项结构见下方 |
| operation_id | str | 否 | 幂等键，同 operation_id 重复提交返回首次结果 |

**relations 每项结构**：

```python
{
    "from_id": UUID,        # 源需求节点 ID
    "to_id": UUID,          # 目标需求节点 ID
    "relation_type": str,   # 关系类型（见枚举）
    "properties": {}        # 关系属性（可选）
}
```

**relation_type 枚举**：
- `conflicts_with` — 冲突关系
- `duplicates` — 重复关系
- `relates_to` — 关联关系
- `supersedes` — 替代关系
- `version_of` — 版本关系

**返回**：`batch_id` + `status`（"pending_review" 或 "approved"；宽松模式已入库时 status="approved" + decision="auto_approved"）。

**行为**：产生审批批次，审批通过后写入 AGE 图边（Cypher CREATE）。

---

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

**何时用**：发现已审批入库的节点内容写错、需要修正时（版本号 +1、重新生成向量、写审计日志）。

**何时不用**：尚未提交/审批中的内容用原发布工具重提；想新增知识用 submit_dev_artifacts 或 publish_requirement。

```python
update_node(
    project_id="proj-uuid",
    node_id="node-uuid",
    content="更正后的正文...",
    tags=["auth", "login"]
)
```

---

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

**何时用**：你想"找出某类需求 / 某功能有哪些需求"时。

**何时不用**：要拿某需求的**关联代码/方案/意图**用 `get_requirement_context`；要做**变更影响分析**用 `analyze_impact_scope`。

```python
search_similar_requirements(
    query="用户登录 OAuth 第三方登录",
    system_id="sys-uuid",
    top_n=10
)
```

---

### analyze_impact_scope — 分析变更影响范围

PM/Dev 共享。从需求出发做**变更影响范围**遍历（需求→代码→依赖→方案→意图）。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 归属项目 ID |
| requirement_id | UUID | 是 | 需求节点 ID |
| max_depth | int | 否 | depends_on 依赖链遍历深度，默认 5 |

**何时用**：评估"改这个需求会影响哪些代码/方案/意图"。

**何时不用**：仅看某需求的直接关联节点用 `get_requirement_context`。

```python
analyze_impact_scope(
    project_id="proj-uuid",
    requirement_id="req-uuid",
    max_depth=5
)
```

---

### get_requirement_context — 查询需求上下文

PM/Dev/Admin 共享。给定需求 UUID，返回其**关联节点**（代码/方案/意图/踩坑）及关系链。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| requirement_id | UUID | 是 | 需求节点 ID |
| depth | int | 否 | 关系链遍历深度（1=直接关联，2=间接，最大 5），默认 2 |

**何时用**：已知需求 UUID，想看它关联了哪些代码/方案/意图/踩坑。

**何时不用**：先经 `search_similar_requirements` 拿到需求 UUID；泛搜需求用 search_similar_requirements。

```python
get_requirement_context(
    requirement_id="req-uuid",
    depth=2
)
```

---

### check_requirement_conflicts — 排查需求冲突

PM 工具。基于向量相似度检测某需求是否与库内需求**重复/矛盾**（同项目同类型高相似度节点）。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 归属项目 ID |
| requirement_id | UUID | 是 | 被检测的需求节点 ID（自动排除自身）|
| threshold | float | 否 | 相似度阈值（0~1），None 用配置默认 0.85 |
| top_n | int | 否 | 检索召回数量上限，默认 20 |

**何时用**：发布前主动排查某需求是否与已有需求重复/矛盾。

**何时不用**：审批阶段的冲突门禁（L2 关键属性比对）由 admin 审批流负责，二者互补。

```python
check_requirement_conflicts(
    project_id="proj-uuid",
    requirement_id="req-uuid"
)
```

---

### get_project_profile — 查询项目画像

PM/Dev/Admin 共享。返回项目最新的 ProjectProfile 节点（技术栈/架构/约定/团队）。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 项目 ID |

**何时用**：提交/检索前想了解项目技术栈、架构约定。

**何时不用**：想枚举可见项目列表用 `get_project_info`。

```python
get_project_profile(project_id="proj-uuid")
```

---

### get_project_info — 枚举/查询项目画像

PM/Dev/Admin 共享。list 枚举当前 key 可见的项目；get 按 project_id 查询单个。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| action | str | 是 | `list` 枚举可见项目 / `get` 查询单个项目 |
| project_id | UUID | 否 | get 时必填的项目 ID |
| include_profile | bool | 否 | 是否附完整画像属性，默认 False |
| include_scope_meta | bool | 否 | 是否附 scope 自证信息，默认 False |

**何时用（PM）**：想确认自己可被哪些项目访问、或查看项目基本信息。

**何时不用**：只查单个项目技术栈细节用 `get_project_profile`。

```python
get_project_info(action="list", include_scope_meta=True)
```

---

## 完整工作流示例

### 场景一：发布新需求（完整流程）

```
PM: "把用户登录需求录入 Mem Lake"

步骤 1：先检索查重
→ search_similar_requirements(
    query="用户登录",
    system_id="sys-uuid-001",
    top_n=5
)
← 返回 0 条相似需求，确认无重复

步骤 2：发布新需求
→ publish_requirement(
    system_id="sys-uuid-001",
    requirement={
        "title": "用户登录功能需求",
        "content": """支持邮箱+密码登录，含记住我功能。
        登录成功后跳转到首页，失败时提示错误原因。
        连续 5 次失败锁定账号 30 分钟。""",
        "properties": {
            "priority": "P0",
            "module": "auth",
            "acceptance_criteria": "1. 邮箱+密码登录成功 2. 记住我功能 3. 失败提示 4. 锁定机制"
        },
        "tags": ["auth", "login", "security"]
    }
)
← batch_id="abc-123", status="pending_review"

步骤 3：告知 PM
PM Agent → 人类: "需求已提交（主键由服务端分配），批次 abc-123 待 admin 审批通过后生效。"
```

---

### 场景二：需求版本演进（替代旧需求）

```
PM: "登录需求升级到 V2，增加 OAuth"

步骤 1：检索旧需求
→ search_similar_requirements(
    query="用户登录",
    system_id="sys-uuid-001"
)
← 找到旧需求 "用户登录功能需求" (node_id=old-req-uuid)

步骤 2：发布新需求并声明替代关系
→ publish_requirement(
    system_id="sys-uuid-001",
    requirement={
        "title": "用户登录功能需求 V2",
        "content": "在 V1 基础上增加 Google/GitHub OAuth 第三方登录...",
        "properties": {
            "priority": "P0",
            "module": "auth",
            "acceptance_criteria": "1. V1 所有功能 2. OAuth 登录 3. 账号绑定"
        }
    },
    related={"supersedes": ["old-req-uuid"]}  # 声明替代关系
)
← batch_id="def-456", status="pending_review"

PM Agent → 人类: "新需求已提交，声明替代旧需求，批次 def-456 待审批。"
```

---

### 场景三：更新已有需求关系

```
PM: "把登录需求和密码重置需求建立关联"

步骤 1：检索两个需求
→ search_similar_requirements(query="用户登录", system_id="sys-uuid")
← 找到 "用户登录功能需求" (node_id=req-login-uuid)

→ search_similar_requirements(query="密码重置", system_id="sys-uuid")
← 找到 "密码重置功能需求" (node_id=req-reset-uuid)

步骤 2：建立关联关系
→ update_requirement_relations(
    project_id="proj-uuid",
    relations=[
        {
            "from_id": "req-login-uuid",
            "to_id": "req-reset-uuid",
            "relation_type": "relates_to",
            "properties": {"reason": "密码重置是登录的辅助流程"}
        }
    ]
)
← batch_id="ghi-789", status="pending_review"

PM Agent → 人类: "已建立两个需求的关联关系，批次 ghi-789 待审批。"
```

---

## 返回值结构

### WriteToolOutput（写入工具统一返回）

```python
{
    "batch_id": "abc-123",           # 审批批次 ID
    "status": "pending_review",      # pending_review / approved
    "node_id": None,                 # 审批通过后回填，未审批时为 None
    "decision": "auto_approved"      # 仅宽松模式返回：auto_approved / needs_human_review
}
```

### SearchResult（检索工具返回）

```python
{
    "node_id": "uuid",
    "title": "节点标题",
    "content": "节点内容摘要（前200字符）",
    "node_type": "Requirement",
    "score": 0.85,                   # 向量相似度（0~1），图遍历为 None
    "source": "fused",               # vector / fulltext / graph / fused
    "properties": {...},
    "tags": [...],
    "version": 1,                    # 节点版本号
    "vector_generated_at": "2026-09-07T10:00:00Z",  # 向量生成时间
    "data_age_hours": 2.5            # 数据年龄（小时）
}
```
