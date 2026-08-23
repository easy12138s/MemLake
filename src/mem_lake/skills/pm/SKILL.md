---
name: mem-lake-pm
description: "Mem Lake product manager skills for publishing and managing requirement nodes in the team knowledge graph. Use when creating new requirements, updating requirement relationships (supersede/relate), or managing requirement versions. Triggers on: 需求发布, publish_requirement, 需求关系, update_requirement_relations, 需求替代, 需求关联, requirement, PRD."
version: 1.2.0
---

# PM Skills（产品经理）

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

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 项目 ID |
| title | str | 是 | 需求标题 |
| content | str | 是 | 需求详细描述 |
| properties | dict | 是 | 必填字段（见下方）|
| tags | list[str] | 否 | 标签列表 |
| related | dict | 否 | 版本关系与关联关系 |

**properties 必填字段**：

| 字段 | 类型 | 说明 |
|------|------|------|
| requirement_id | str | 需求唯一标识（如 "REQ-001"）|
| priority | str | 优先级（P0/P1/P2/P3）|
| module | str | 所属模块 |
| acceptance_criteria | str | 验收标准 |

**related 结构**（可选）：

```python
{
    "supersedes": ["REQ-000"],      # 替代旧需求 ID 列表
    "relates_to": ["REQ-002"],      # 关联需求 ID 列表
    "conflicts_with": ["REQ-003"]   # 冲突需求 ID 列表
}
```

返回：`WriteToolOutput`（node_id=None 直到审批通过, batch_id, status="pending_review"/"approved"；宽松模式已入库时 status="approved" + decision="auto_approved"）

### update_requirement_relations — 更新需求间关系

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 项目 ID |
| requirement_id | str | 是 | 目标需求 ID |
| supersedes | list[str] | 否 | 新增的替代关系 |
| relates_to | list[str] | 否 | 新增的关联关系 |
| conflicts_with | list[str] | 否 | 新增的冲突关系 |

返回：`WriteToolOutput`（batch_id, status="pending_review"/"approved"；宽松模式已入库时 status="approved" + decision="auto_approved"）

行为：产生审批批次，审批通过后写入 AGE 图边（Cypher CREATE）。

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
       project_id=uuid,
       title="用户登录功能需求",
       content="支持邮箱+密码登录，含记住我功能...",
       properties={
           "requirement_id": "REQ-001",
           "priority": "P0",
           "module": "auth",
           "acceptance_criteria": "1. 邮箱登录成功 2. 错误密码提示..."
       },
       tags=["auth", "login"]
   )
   ← 返回 batch_id, status="pending_review"

3. 告知 PM："需求 REQ-001 已提交，批次 {batch_id} 待 admin 审批"
```

### 场景二：需求版本演进（替代旧需求）

```
# REQ-002 是 REQ-001 的升级版
publish_requirement(
    project_id=uuid,
    title="用户登录功能需求 V2",
    content="在 V1 基础上增加 OAuth 第三方登录...",
    properties={
        "requirement_id": "REQ-002",
        "priority": "P0",
        "module": "auth",
        "acceptance_criteria": "1. V1 所有功能 2. OAuth 登录..."
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
    requirement_id="REQ-001",
    relates_to=["REQ-005"]
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
    project_id="proj-uuid-001",
    title="用户登录功能需求",
    content="""支持邮箱+密码登录，含记住我功能。
    登录成功后跳转到首页，失败时提示错误原因。
    连续 5 次失败锁定账号 30 分钟。""",
    properties={
        "requirement_id": "REQ-001",
        "priority": "P0",
        "module": "auth",
        "acceptance_criteria": "1. 邮箱+密码登录成功 2. 记住我功能 3. 失败提示 4. 锁定机制"
    },
    tags=["auth", "login", "security"]
)
← batch_id="abc-123", status="pending_review"

PM Agent → 人类: "需求 REQ-001 已提交，批次 abc-123 待 admin 审批通过后生效。"
```

### 示例 2：需求版本演进

```
PM: "登录需求升级到 V2，增加 OAuth"

→ publish_requirement(
    project_id="proj-uuid-001",
    title="用户登录功能需求 V2",
    content="在 V1 基础上增加 Google/GitHub OAuth 第三方登录...",
    properties={
        "requirement_id": "REQ-002",
        "priority": "P0",
        "module": "auth",
        "acceptance_criteria": "1. V1 所有功能 2. OAuth 登录 3. 账号绑定"
    },
    related={"supersedes": ["REQ-001"]}
)
← batch_id="def-456"

PM Agent → 人类: "需求 REQ-002 已提交，声明替代 REQ-001，批次 def-456 待审批。"
```
