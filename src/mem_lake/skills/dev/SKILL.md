---
name: mem-lake-dev
description: "Mem Lake developer skills for submitting development artifacts (code snippets, solutions, design intents, pitfalls) to the team knowledge graph. Use when recording code implementations, design decisions, solutions, or pitfalls encountered during development. Triggers on: 代码片段, submit_dev_artifacts, 方案, 设计意图, 踩坑, CodeSnippet, Solution, DesignIntent, Pitfall, ref, 批量提交."
version: 1.2.0
---

# Dev Skills（开发者）

## When to Use

当开发者或其 Agent 需要执行以下操作时加载本 skill：
- 提交代码片段（CodeSnippet）到团队知识图谱
- 记录解决方案（Solution）或设计意图（DesignIntent）
- 沉淀开发中遇到的坑（Pitfall）及解决方案
- 批量提交开发产物并建立节点间关系

## Do NOT load for

- 发布需求（使用 PM Skills）
- 审批批次或管理 Access Key（使用 Admin Skills）
- 检索知识或查询需求上下文（使用通用查询工具，无需加载角色 skill）

## 你的角色

你是 Mem Lake 的开发者 Agent。Mem Lake 是团队共享的知识记忆层，你提交的开发产物会进入审批队列，admin 审批通过后正式写入知识图谱，供全团队所有 Agent 检索使用。

核心价值：**让你的开发经验被团队所有 AI 共享**。没有 Mem Lake，你踩过的坑只有你的 AI 知道；有了 Mem Lake，其他开发者的 AI 能检索到你记录的坑和解决方案，新人 AI 也能快速了解项目的设计意图。

关键原则：你只负责提交，不负责审批。提交后获得 batch_id，等待 admin 审批通过。

## 可用工具

### submit_dev_artifacts — 批量提交开发产物

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 项目 ID |
| requirement_id | UUID | 是 | 关联的需求节点 ID（自动为每个 CodeSnippet 构造 implements 边） |
| code_snippets | list[dict] | 否 | 代码片段列表 |
| solutions | list[dict] | 否 | 解决方案列表 |
| design_intents | list[dict] | 否 | 设计意图列表 |
| pitfalls | list[dict] | 否 | 踩坑记录列表 |
| relations | list[dict] | 否 | 节点间关系（用 ref 引用）|

**各产物类型必填字段**：

#### CodeSnippet（代码片段）

| 字段 | 类型 | 说明 |
|------|------|------|
| ref | str | 批次内唯一引用名（如 "LoginService"）|
| title | str | 代码片段标题 |
| content | str | 代码内容或详细说明 |
| properties.name | str | 代码元素名称 |
| properties.type | str | 类型（class/function/module/component）|
| properties.responsibility | str | 职责描述 |
| properties.file_path | str | 文件路径 |
| tags | list[str] | 否，标签 |

#### Solution（解决方案）

| 字段 | 类型 | 说明 |
|------|------|------|
| ref | str | 引用名 |
| title | str | 方案标题 |
| content | str | 方案详细描述 |
| properties.approach | str | 采用的方案 |
| properties.alternatives | str | 备选方案 |
| tags | list[str] | 否，标签 |

#### DesignIntent（设计意图）

| 字段 | 类型 | 说明 |
|------|------|------|
| ref | str | 引用名 |
| title | str | 设计意图标题 |
| content | str | 详细描述 |
| properties.rationale | str | 设计理由 |
| properties.trade_offs | str | 权衡取舍 |
| tags | list[str] | 否，标签 |

#### Pitfall（踩坑记录）

| 字段 | 类型 | 说明 |
|------|------|------|
| ref | str | 引用名 |
| title | str | 坑的标题 |
| content | str | 详细描述 |
| properties.symptom | str | 症状表现 |
| properties.root_cause | str | 根本原因 |
| properties.solution | str | 解决方案 |
| properties.severity | str | 严重程度（P0/P1/P2/P3）|
| tags | list[str] | 否，标签 |

**relations 结构**（用 ref 引用批次内节点或已有节点 UUID）：

```python
[
    {
        "from_ref": "LoginService",      # ref 名或已有节点 UUID
        "relation_type": "implements",  # implements/depends_on/realized_by/embodies/traces_to/described_by/references
        "to_ref": "req-uuid"             # ref 名或已有节点 UUID
    }
]
```

**自动构造的关系**：系统根据提交的 `requirement_id` 参数，自动为批次内**每个** CodeSnippet 构造 `Requirement --implements--> CodeSnippet` 关系（无需在 relations 中手动声明）。

**⚠️ 坑/方案/意图不会自动挂载到需求**：自动关系**仅限 CodeSnippet**。Pitfall、Solution、DesignIntent 提交后只是孤立节点，**不会**自动与 `requirement_id` 建立任何边。若希望它们出现在某需求下（或在图谱中与其它节点相连），必须在 `relations` 中显式声明，例如把坑挂到需求：

```python
relations=[
    {"from_ref": str(requirement_id), "relation_type": "described_by", "to_ref": "AsyncSessionLeak"}
]
```

`relation_type` 可选 `implements / depends_on / realized_by / embodies / traces_to / described_by / references / relates_to` 等。

返回：`WriteToolOutput`（node_id=None 直到审批通过, batch_id, status="pending_review"）

### get_role_skills — 获取角色 Skills 文档

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| role | str | 否 | 指定角色，None 返回当前调用者角色 |

返回：role, skills_markdown, version, installation_guide

## 工作流

### 场景一：提交单个代码片段

```
1. 准备代码片段信息
   - 确认 project_id
   - 编写 title, content
   - 填充 properties（name, type, responsibility, file_path）
   - 声明 ref 名

2. submit_dev_artifacts(
       project_id=uuid,
       code_snippets=[{
           "ref": "LoginService",
           "title": "用户登录服务实现",
           "content": "class LoginService: ...",
           "properties": {
               "name": "LoginService",
               "type": "class",
               "responsibility": "处理用户认证逻辑",
               "file_path": "src/auth/login_service.py"
           },
           "tags": ["auth", "login"]
       }]
   )
   ← 返回 batch_id, status="pending_review"

3. 告知开发者："代码片段已提交，批次 {batch_id} 待 admin 审批"
```

### 场景二：批量提交 + 建立关系

```
# 提交登录服务代码 + 对应方案 + 关联到已有需求
submit_dev_artifacts(
    project_id=uuid,
    code_snippets=[{
        "ref": "LoginService",
        "title": "用户登录服务实现",
        "content": "class LoginService: ...",
        "properties": {
            "name": "LoginService",
            "type": "class",
            "responsibility": "处理用户认证逻辑",
            "file_path": "src/auth/login_service.py"
        }
    }],
    solutions=[{
        "ref": "TokenAuth",
        "title": "Token 认证方案",
        "content": "采用 JWT Token 方案...",
        "properties": {
            "approach": "JWT Token + Redis 缓存",
            "alternatives": "Session-based auth（因扩展性差未采用）"
        }
    }],
     relations=[
         {"from_ref": "LoginService", "relation_type": "implements", "to_ref": "req-uuid"},
         {"from_ref": "TokenAuth", "relation_type": "traces_to", "to_ref": "LoginService"}
     ]
)
```

### 场景三：记录踩坑并挂到需求

```
# 开发中遇到并解决了坑，沉淀到知识图谱，并关联到对应需求
submit_dev_artifacts(
    project_id=uuid,
    requirement_id=req_uuid,   # 必填；但坑不会自动挂到需求，需下方 relations 显式声明
    pitfalls=[{
        "ref": "AsyncSessionLeak",
        "title": "async SQLAlchemy Session 泄漏导致连接池耗尽",
        "content": "在高并发下出现 PoolExhausted 错误...",
        "properties": {
            "symptom": "PoolExhausted: Connection pool exhausted",
            "root_cause": "AsyncSession 未在 finally 中 close",
            "solution": "使用 async with session: 上下文管理器",
            "severity": "P1"
        },
        "tags": ["async", "sqlalchemy", "bug"]
    }],
    # 坑不会自动挂载到需求，必须显式声明关系，否则它只是孤立节点
    relations=[
        {"from_ref": str(req_uuid), "relation_type": "described_by", "to_ref": "AsyncSessionLeak"}
    ]
)
```

### 场景四：记录设计意图

```
# 记录为什么选择当前架构（同样需显式 relations 才会与需求/方案相连）
submit_dev_artifacts(
    project_id=uuid,
    requirement_id=req_uuid,
    design_intents=[{
        "ref": "WhyPGOverMongo",
        "title": "为什么选择 PostgreSQL 而非 MongoDB",
        "content": "知识图谱需要关系型 + 向量 + 全文检索...",
        "properties": {
            "rationale": "PostgreSQL 支持 pgvector + AGE + zhparser 三合一",
            "trade_offs": "放弃 MongoDB 的 schema-free 灵活性，换取事务一致性"
        }
    }]
)
```

## 常见陷阱

1. **每个产物必须声明 ref 名**：ref 是批次内的唯一引用名，relations 通过 ref 引用节点。不声明 ref 会导致无法建立关系。ref 名在批次内唯一即可，不必全局唯一。

2. **ref 名在批次内唯一即可**：不同批次可以有相同的 ref 名（如 "LoginService"），系统会在审批通过时解析为实际节点 UUID。不要用 UUID 作为 ref（那是已有节点的 ID）。

3. **relations 中 from_ref/to_ref 可以是 ref 名或已有节点 UUID**：
   - 引用本批次内新建的节点 → 用 ref 名
   - 引用知识图谱中已有的节点 → 用节点 UUID

4. **提交后不可修改**：批次一旦提交，内容不可修改。如需修改，只能等 admin 拒绝后重新提交，或提交新版本节点。

5. **不要假设提交即生效**：status="pending_review" 意味着产物尚未写入知识图谱。其他 Agent 此时检索不到。需等待 admin 审批通过后才可被检索。

6. **content 应包含实际代码或详细说明**：content 与核心属性都会用于向量生成（系统按类型纳入关键属性，如 CodeSnippet 的 name/responsibility、Pitfall 的 symptom/root_cause 等），内容越详细检索越准确。CodeSnippet 的 content 应包含实际代码片段，Pitfall 的 content 应包含错误堆栈和解决过程。

7. **批量提交优于多次单条提交**：一次批量提交多个产物 + relations，系统会在审批通过时同事务写入所有节点和边，保证关系完整性。多次单条提交可能导致中间状态（节点已写入但关系未写入）。

8. **properties 字段缺失会被 schema 校验拒绝**：submit_dev_artifacts 在工具层即校验各类型 properties 必填字段，缺失会直接返回错误。

9. **tags 为精确标签，AND/OR 由 tags_op 控制**：tags 是节点级精确标签。检索时默认 `tags_op="all"`（节点须包含全部给定标签，等价于子集匹配）；希望命中任一标签用 `tags_op="any"`（OR 语义）。语义相近但字面不同的标签（如「性能」与「N+1」）默认不会自动匹配；若需语义相近召回，在 `search_similar_requirements` / `search_code_snippets` 中传 `semantic_tags=true`，系统会用 embedding 把给定标签扩展为项目内语义相近的标签（如「性能」≈「N+1」）。

## 示例

### 示例 1：完整批量提交

```
Dev: "把登录模块的实现和方案录入 Mem Lake"

→ submit_dev_artifacts(
    project_id="proj-uuid-001",
    code_snippets=[{
        "ref": "LoginService",
        "title": "用户登录服务实现",
        "content": """class LoginService:
    async def login(self, email: str, password: str) -> Token:
        user = await self.user_repo.find_by_email(email)
        if not user or not verify_password(password, user.password_hash):
            raise AuthError("Invalid credentials")
        return self.token_service.generate(user.id)""",
        "properties": {
            "name": "LoginService",
            "type": "class",
            "responsibility": "处理用户认证逻辑，含密码校验和 Token 生成",
            "file_path": "src/auth/login_service.py"
        },
        "tags": ["auth", "login"]
    }],
    solutions=[{
        "ref": "TokenAuth",
        "title": "JWT Token 认证方案",
        "content": "采用 JWT Token 方案，access_token 有效期 2h，refresh_token 7d...",
        "properties": {
            "approach": "JWT Token + Redis 黑名单",
            "alternatives": "Session-based（因水平扩展困难未采用）"
        }
    }],
     relations=[
         {"from_ref": "LoginService", "relation_type": "implements", "to_ref": "req-001-uuid"},
         {"from_ref": "TokenAuth", "relation_type": "traces_to", "to_ref": "LoginService"}
     ]
)
← batch_id="abc-123", status="pending_review"

Dev Agent → 人类: "已提交 1 个代码片段 + 1 个方案，关联到 REQ-001，批次 abc-123 待审批。"
```

### 示例 2：记录踩坑

```
Dev: "昨天踩的 async session 泄漏的坑记一下"

→ submit_dev_artifacts(
    project_id="proj-uuid-001",
    pitfalls=[{
        "ref": "AsyncSessionLeak",
        "title": "async SQLAlchemy Session 泄漏导致连接池耗尽",
        "content": """高并发下出现 PoolExhausted 错误。
错误堆栈: sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached
排查发现 AsyncSession 未在异常路径中 close。""",
        "properties": {
            "symptom": "PoolExhausted: Connection pool exhausted",
            "root_cause": "AsyncSession 在异常路径未 close，连接泄漏",
            "solution": "使用 async with session: 上下文管理器替代手动 close",
            "severity": "P1"
        },
        "tags": ["async", "sqlalchemy", "bug", "P1"]
    }]
)
← batch_id="def-456"

Dev Agent → 人类: "踩坑记录已提交，批次 def-456 待审批。"
```
