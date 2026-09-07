# Dev Reference（详细参数表与示例）

> 本文件为 Dev Skill 的参考文档，包含完整的工具参数表、嵌套结构定义和详细工作流示例。
> 主文件 `SKILL.md` 包含核心指导原则和快速参考，需要详细参数时加载本文件。

---

## 工具详细参数

### submit_dev_artifacts — 批量提交开发产物

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 项目 ID |
| artifacts | dict | 否 | 嵌套产物集合，结构见下 |
| requirement_id | UUID | 否 | 关联的需求节点 ID（自动为每个 CodeSnippet 构造 implements 边） |
| relations | list[dict] | 否 | 节点间关系（用 ref 引用）|
| operation_id | str | 否 | 幂等键，同 operation_id 重复提交返回首次结果 |

**artifacts 内部结构**：

```python
artifacts={
    "code_snippets":  [ {...} ],   # 见下方 CodeSnippet 必填字段
    "solutions":      [ {...} ],   # 见下方 Solution 必填字段
    "design_intents": [ {...} ],   # 见下方 DesignIntent 必填字段
    "pitfalls":       [ {...} ],   # 见下方 Pitfall 必填字段
}
```

---

### 各产物类型必填字段

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
| properties.version | str | 版本号（必填）|
| properties.approach | str | 采用的方案（必填）|
| properties.alternatives | str | 备选方案（可选）|
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

---

### 最简调用模板

**CodeSnippet**：
```python
submit_dev_artifacts(project_id=uuid, artifacts={"code_snippets":[{
    "ref": "MySvc", "title": "标题", "content": "代码或说明",
    "properties": {"name":"MySvc","type":"class",
                   "responsibility":"职责","file_path":"src/x.py"}
}]})
```

**Solution**：
```python
submit_dev_artifacts(project_id=uuid, artifacts={"solutions":[{
    "ref":"SolA","title":"方案","content":"描述",
    "properties":{"version":"v1","approach":"采用的方案"}
}]})
```

**DesignIntent**：
```python
submit_dev_artifacts(project_id=uuid, artifacts={"design_intents":[{
    "ref":"WhyA","title":"意图","content":"描述",
    "properties":{"rationale":"理由","trade_offs":"权衡"}
}]})
```

**Pitfall**：
```python
submit_dev_artifacts(project_id=uuid, artifacts={"pitfalls":[{
    "ref":"BugA","title":"坑","content":"描述",
    "properties":{"symptom":"症状","root_cause":"根因",
                   "solution":"解决方案","severity":"P1"}
}]})
```

---

### relations 结构

用 ref 引用批次内节点或已有节点 UUID：

```python
[
    {
        "from_ref": "LoginService",      # ref 名或已有节点 UUID
        "relation_type": "implements",   # 见下方枚举
        "to_ref": "req-uuid"             # ref 名或已有节点 UUID
    }
]
```

**relation_type 枚举**：`implements` / `depends_on` / `realized_by` / `embodies` / `traces_to` / `described_by` / `references`

**自动构造的关系**：
- 传入 `requirement_id` 时，系统自动为每个 CodeSnippet 构造 `Requirement --implements--> CodeSnippet` 关系
- 省略 `requirement_id` 时，系统自动把每个产物挂到本项目的 `ProjectProfile` 节点

**⚠️ 坑/方案/意图不会自动挂载到需求**：若希望它们出现在某需求下，必须在 `relations` 中显式声明：

```python
relations=[
    {"from_ref": str(requirement_id), "relation_type": "described_by", "to_ref": "AsyncSessionLeak"}
]
```

**返回**：`WriteToolOutput`（node_id=None 直到审批通过, batch_id, status="pending_review"/"approved"）

---

### update_node — 修正已审批通过的节点

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 节点所属项目 ID |
| node_id | UUID | 是 | 要更新的已审批节点 UUID |
| title | str | 否 | 新标题；留空则不更新 |
| content | str | 否 | 新正文；留空则不更新 |
| properties | dict | 否 | 新属性字典，整体替换原属性；留空则不更新 |
| tags | list[str] | 否 | 新标签列表；留空则不更新 |
| operation_id | str | 否 | 幂等键 |

```python
update_node(
    project_id="proj-uuid-001",
    node_id="node-uuid-xxx",
    content="修正后的正确描述...",
    properties={"name": "LoginService", "type": "class",
                "responsibility": "处理用户认证逻辑", "file_path": "src/auth/login_service.py"}
)
```

---

### search_similar_requirements — 检索相似需求

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| query | str | 是 | 查询文本 |
| system_id | UUID | 否 | 归属 system 域 |
| project_id | UUID | 否 | 归属项目 ID |
| top_n | int | 否 | 返回数量上限，默认 10 |
| tags | list[str] | 否 | 标签过滤 |
| tags_op | str | 否 | `all`=AND（默认），`any`=OR |
| min_score | float | 否 | 向量相似度下限，默认 0.5 |
| semantic_tags | bool | 否 | 标签语义扩展，默认 False |

```python
search_similar_requirements(
    query="用户登录认证",
    system_id="sys-uuid-001",
    top_n=10
)
```

---

### search_code_snippets — 检索研发资产

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 归属项目 ID |
| query | str | 是 | 查询文本 |
| top_n | int | 否 | 返回数量上限，默认 10 |
| tags | list[str] | 否 | 标签过滤 |
| tags_op | str | 否 | `all`=AND（默认），`any`=OR |
| min_score | float | 否 | 向量相似度下限，默认 0.5 |
| semantic_tags | bool | 否 | 标签语义扩展，默认 False |

```python
search_code_snippets(
    project_id="proj-uuid-001",
    query="yaml 缩进",
    top_n=10
)
```

---

### analyze_impact_scope — 变更影响范围分析

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | UUID | 是 | 归属项目 ID |
| requirement_id | UUID | 是 | 需求节点 ID |
| max_depth | int | 否 | 依赖链遍历深度，默认 5 |

```python
analyze_impact_scope(
    project_id="proj-uuid-001",
    requirement_id="req-uuid-001",
    max_depth=5
)
```

---

### get_requirement_context — 查询需求上下文

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| requirement_id | UUID | 是 | 需求节点 ID |
| depth | int | 否 | 遍历深度，默认 2 |

```python
get_requirement_context(requirement_id="req-uuid-001", depth=2)
```

---

## 完整工作流示例

### 场景一：提交单个代码片段

```
Dev: "把登录模块的实现录入 Mem Lake"

1. 先检索查重
→ search_code_snippets(project_id="proj-uuid-001", query="用户登录")
← 返回 0 条相似内容

2. 提交代码片段
→ submit_dev_artifacts(
    project_id="proj-uuid-001",
    artifacts={
        "code_snippets": [{
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
                "responsibility": "处理用户认证逻辑",
                "file_path": "src/auth/login_service.py"
            },
            "tags": ["auth", "login"]
        }]
    }
)
← batch_id="abc-123", status="pending_review"

3. 告知开发者
Dev Agent → 人类: "代码片段已提交，批次 abc-123 待 admin 审批。"
```

---

### 场景二：批量提交 + 建立关系

```
Dev: "把登录模块的实现和方案录入 Mem Lake"

→ submit_dev_artifacts(
    project_id="proj-uuid-001",
    artifacts={
        "code_snippets": [{
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
        "solutions": [{
            "ref": "TokenAuth",
            "title": "Token 认证方案",
            "content": "采用 JWT Token 方案...",
            "properties": {
                "version": "v1",
                "approach": "JWT Token + Redis 缓存",
                "alternatives": "Session-based auth（因扩展性差未采用）"
            }
        }]
    },
    relations=[
        {"from_ref": "LoginService", "relation_type": "implements", "to_ref": "req-uuid"},
        {"from_ref": "TokenAuth", "relation_type": "traces_to", "to_ref": "LoginService"}
    ]
)
← batch_id="def-456", status="pending_review"
```

---

### 场景三：记录踩坑并挂到需求

```
Dev: "昨天踩的 async session 泄漏的坑记一下"

→ submit_dev_artifacts(
    project_id="proj-uuid-001",
    requirement_id=req_uuid,
    artifacts={
        "pitfalls": [{
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
        }]
    },
    relations=[
        {"from_ref": str(req_uuid), "relation_type": "described_by", "to_ref": "AsyncSessionLeak"}
    ]
)
← batch_id="ghi-789", status="pending_review"
```

---

### 场景四：记录设计意图

```
Dev: "记录为什么选择 PostgreSQL 而非 MongoDB"

→ submit_dev_artifacts(
    project_id="proj-uuid-001",
    requirement_id=req_uuid,
    artifacts={
        "design_intents": [{
            "ref": "WhyPGOverMongo",
            "title": "为什么选择 PostgreSQL 而非 MongoDB",
            "content": "知识图谱需要关系型 + 向量 + 全文检索...",
            "properties": {
                "rationale": "PostgreSQL 支持 pgvector + AGE + zhparser 三合一",
                "trade_offs": "放弃 MongoDB 的 schema-free 灵活性，换取事务一致性"
            }
        }]
    }
)
← batch_id="jkl-012", status="pending_review"
```

---

### 场景五：记录游离知识点（不绑定需求）

```
Dev: "记录 YAML 缩进的坑，不绑定具体需求"

→ submit_dev_artifacts(
    project_id="proj-uuid-001",
    artifacts={
        "pitfalls": [{
            "ref": "YamlIndentTrap",
            "title": "YAML 缩进错误导致服务启动失败",
            "content": "2 空格 vs 4 空格混用被解析为嵌套结构...",
            "properties": {
                "symptom": "service fails to start",
                "root_cause": "mixed indentation",
                "solution": "统一 2 空格缩进",
                "severity": "P2"
            },
            "tags": ["yaml", "config"]
        }]
    }
)
← batch_id="mno-345", status="pending_review"

# 审批通过后自动生成：ProjectProfile --references--> YamlIndentTrap
```

---

## 返回值结构

### WriteToolOutput

```python
{
    "batch_id": "abc-123",
    "status": "pending_review",      # pending_review / approved
    "node_id": None,                 # 审批通过后回填
    "decision": "auto_approved"      # 仅宽松模式返回
}
```

### SearchResult

```python
{
    "node_id": "uuid",
    "title": "节点标题",
    "content": "节点内容摘要（前200字符）",
    "node_type": "CodeSnippet",
    "score": 0.85,
    "source": "fused",
    "properties": {...},
    "tags": [...],
    "version": 1,
    "vector_generated_at": "2026-09-07T10:00:00Z",
    "data_age_hours": 2.5
}
```
