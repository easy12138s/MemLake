# Mem Lake 项目总体设计文档（PDD）

| 项目 | Mem Lake |
|------|----------|
| 文档版本 | v0.7 |
| 文档状态 | 设计基线 |
| 日期 | 2026-08-02 |
| 关联文档 | 《Mem Lake 项目价值与前景研究》 |

> 本文档定义 Mem Lake 项目的总体设计方案。知识存储基于知识图谱建模范式，MCP 网关基于 2026-07-28 无状态规范设计，RBAC 采用业务角色模型（admin/pm/dev），审批流基于批次单元设计，技术栈经审核验证确认。

---

## 目录

- [1. 项目概述](#1-项目概述)
- [2. 系统总体架构](#2-系统总体架构)
- [3. 核心功能模块设计](#3-核心功能模块设计)
- [4. 数据模型设计](#4-数据模型设计)
- [5. 关键技术方案](#5-关键技术方案)
- [6. 接口设计](#6-接口设计)
- [7. 应用场景设计](#7-应用场景设计)
- [8. 角色 Skills 规划](#8-角色-skills-规划)
- [9. 非功能性设计](#9-非功能性设计)
- [10. 演进路线](#10-演进路线)
- [11. 附录](#11-附录)

---

## 1. 项目概述

### 1.1 项目背景

软件研发团队在引入 AI 编程工具（Cursor、Codex、Trae 等）后，普遍面临三大核心问题：

1. **项目上下文缺失**：AI 工具每次对话为独立会话，无法感知项目历史需求、设计决策、代码约定。
2. **知识资产流失**：项目经验存储于开发者个人记忆，人员流动导致隐性知识流失。
3. **AI 输出质量不稳定**：AI 缺乏对企业私有规范、隐式约定、踩坑记录的认知。

此外还存在**知识治理缺位**与**数据安全合规风险**两个衍生问题。

### 1.2 项目定位

**Mem Lake 是面向软件研发团队的多智能体协作记忆基础设施**，基于 MCP（Model Context Protocol）协议构建，为研发流程中的不同角色 Agent 提供项目知识的标准化存储、检索与治理能力。

**核心设计哲学**：确定性知识存取中间件。

| 原则 | 含义 |
|------|------|
| 确定性中间件 | 仅负责存取、检索、治理等确定性操作 |
| 标准化接口 | 基于 MCP 协议暴露工具接口，兼容生态 |
| 智能判断外移 | 内容理解、推理、生成等智能判断工作由上游 Agent 完成 |
| 代码知识化 | Mem Lake 存储代码的知识描述（类名、职责、签名、关键片段），代码内容归属 Git |

### 1.3 设计目标

- MCP 协议网关（含认证与权限拦截）
- 知识图谱存储（节点 + 边 + 属性 + 向量索引）
- 三引擎检索（关键词全文 + 向量语义 + 图遍历，融合排序）
- 知识审批工作流（Agent 提交 → 管理员审核 → 可检索）
- 多角色权限管理（RBAC + 项目级隔离）
- 完全本地化部署能力

### 1.4 设计原则

遵循"**安全 → 高效 → 开放**"三重原则：

1. **安全**：完全本地化部署，数据不出内网；权限控制 + 审计日志 + 数据加密。
2. **高效**：混合检索机制，毫秒级延迟，Token 成本优化。
3. **开放**：无生态依赖，不绑定特定 Git 平台、项目管理工具、云服务或大模型供应商。

---

## 2. 系统总体架构

### 2.1 架构总览

采用**三层架构**，上下游职责边界清晰。知识存储以**知识图谱**为核心建模范式，物理实现基于 PostgreSQL 单实例集成三个扩展引擎。

```
┌─────────────────────────────────────────────────────────────┐
│  上游层：各类 AI Agent                                        │
│  产品经理 Agent / 开发者 Agent / 管理员 Agent / 其他工具      │
│  职责：需求解析、可行性分析、代码生成、内容理解与推理、治理     │
└───────────────────────────┬─────────────────────────────────┘
                            │ MCP 协议（标准工具接口）
┌───────────────────────────┴─────────────────────────────────┐
│  Mem Lake 层：确定性知识存取中间件                              │
│  ┌──────────┬──────────┬──────────┬──────────┐              │
│  │ MCP 网关 │ 知识图谱 │ 三引擎   │ 审批工作流│              │
│  │(认证拦截)│ 存储     │ 检索     │(提交→审核│              │
│  │          │(节点+边+ │(向量+全文│ →发布)  │              │
│  │          │ 属性)    │ +图遍历) │         │              │
│  └──────────┴──────────┴──────────┴──────────┘              │
│  职责：权限校验、向量化、图谱存取、三引擎检索、审批、审计       │
└───────────────────────────┬─────────────────────────────────┘
                            │ 本地接口
┌───────────────────────────┴─────────────────────────────────┐
│  下游层：PostgreSQL 17 单实例（三引擎共存）                    │
│  ├── pgvector：向量语义检索                                   │
│  ├── Apache AGE：图遍历（Cypher 查询）                        │
│  ├── PG 原生：关系元数据 + 全文索引（tsvector + GIN）          │
│  └── 本地 Embedding 模型服务（独立进程，文本向量化）           │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 组件清单与职责

| 组件 | 职责 | 对外暴露 |
|------|------|----------|
| MCP 网关 | 协议适配、认证鉴权、权限拦截、限流 | MCP 工具接口 |
| 知识图谱存储 | 节点 CRUD、边 CRUD、属性管理、向量索引写入 | 内部 API |
| 三引擎检索 | 向量语义检索 + 关键词全文检索 + 图遍历，融合排序 | 内部 API |
| 审批工作流 | 提交、审核、发布、退回、冲突检测 | MCP 工具接口 |
| 本地 Embedding 服务 | 文本向量化（离线/内网） | gRPC/HTTP |

### 2.3 部署架构

**完全本地化部署**，所有组件运行于企业内网：

```
企业内网
├── Mem Lake 服务进程（MCP 网关 + 图谱存取 + 三引擎检索 + 审批）
├── PostgreSQL 17 单实例
│   ├── pgvector 扩展（向量检索）
│   ├── Apache AGE v1.8.0 扩展（图遍历）
│   └── PG 原生（关系元数据 + 全文索引）
└── 本地 Embedding 模型服务（独立进程）
```

**部署形态**：采用 Docker 容器化部署。`apache/age` 官方镜像仅含 AGE 扩展，不含 pgvector 与 zhparser，需通过自定义 Dockerfile 安装 pgvector 与 zhparser 扩展后构建自定义镜像。所有组件通过 Docker Compose 编排，支持离线安装包分发，无外网依赖。

### 2.4 技术选型

| 类别 | 选型 | 理由 |
|------|------|------|
| 协议 | MCP 2026-07-28 规范 | 最新稳定版，无状态核心，云原生友好，Linux Foundation 治理 |
| 数据库 | PostgreSQL 17 | 成熟稳定，支持扩展机制，本地化友好 |
| 向量检索 | pgvector | PG 原生扩展，HNSW 索引，与 PG 同库事务一致 |
| 图遍历 | Apache AGE v1.8.0 | Apache 顶级项目，PG 扩展，Cypher 查询，支持多跳遍历与最短路径 |
| 全文检索 | PostgreSQL tsvector + GIN | PG 原生能力，无需额外组件 |
| Embedding 模型 | BAAI/bge-large-zh-v1.5（1024 维） | 中文优化、本地可跑、开源协议、满足数据不出内网 |
| 后端语言 | Python 3.11+ | AGE 官方 Python 驱动、pgvector 生态完善、AI 场景适配 |
| 部署 | Docker Compose | 单机部署，离线包分发，适配中型团队 |

**三引擎共存可行性**：pgvector 与 Apache AGE 均为 PostgreSQL 扩展，在同一 PG 实例内通过 `CREATE EXTENSION` 启用，共享事务与存储。该组合已有阿里云 PolarDB 企业级集成背书，并在风控、知识图谱等生产场景验证。AGE v1.8.0 对变长边遍历（VLE）做了专项性能优化，提供 `shortest_path`、`create_subgraph` 等函数，满足 Mem Lake 的多跳查询需求。

**MCP 协议选型说明**：MCP 2026-07-28 规范于 2026 年 7 月 28 日正式发布，是 MCP 诞生以来最大版本更新，核心变化是协议无状态化。Mem Lake 作为新项目直接基于此规范设计，避免基于旧版 Session 模型建设后再迁移。规范由 Linux Foundation 治理，Anthropic、OpenAI、Google、Microsoft 等企业采用，公开 MCP Server 超 1.3 万个，官方 SDK 月下载量 9700 万次。

---

## 3. 核心功能模块设计

### 3.1 MCP 协议网关

**职责**：作为系统唯一入口，基于 MCP 2026-07-28 无状态规范实现协议适配、认证、鉴权、限流。

**无状态核心**：

MCP 网关采用无状态设计，不维护会话状态，每个请求自包含路由与鉴权信息。服务端可水平扩展，任意实例处理任意请求，负载均衡器使用普通 round-robin 策略。

- 每个请求通过 `_meta` 携带 `protocolVersion` 与 `clientCapabilities`，服务端逐请求解析
- 不实现 `initialize` 握手与 `Mcp-Session-Id`，不依赖连接历史
- 实现 `server/discover` 方法，客户端可随时获取服务端支持的协议版本与工具清单

**传输协议**：

采用 Streamable HTTP 传输，单端点 POST 请求/响应模型：

- 每个请求必须携带 `Mcp-Method` 头（如 `tools/call`）与 `Mcp-Name` 头（如 `mem-lake`）
- 每个请求必须携带 `MCP-Protocol-Version` 头，值与正文 `_meta` 一致
- 服务端校验 Header 与正文一致性，不一致返回 HTTP 400 与错误码 `-32020`
- 网关层可直接基于 `Mcp-Method` 头路由，无需解包 JSON 正文

**认证机制**：

采用 MCP Access Key 认证，适配内网团队部署场景：

- 认证凭证：Header `X-MCP-Key` 传递 Access Key
- Access Key 绑定业务角色（admin / pm / dev）与项目范围
- 鉴权链路：Access Key → 角色 → 权限策略 → 操作放行/拒绝
- Access Key 仅存哈希（bcrypt），支持轮换与吊销，吊销后立即生效
- 每次请求重新验证身份，不从连接历史推断授权

**工具元数据**：

- 工具声明 `outputSchema`，返回 `structuredContent`，为客户端提供可验证的结构化输出
- 工具通过 Tool Annotations 标记风险属性：写操作标记 `readOnlyHint: false`，读操作标记 `readOnlyHint: true`
- `tools/list` 响应声明 `ttlMs`（默认 300000ms）与 `cacheScope: global`，客户端可缓存工具清单

**幂等性设计**：

写操作工具（`publish_requirement`、`submit_dev_artifacts`、`update_requirement_relations`、`review_approve`、`review_reject`）支持可选的 `operation_id` 参数：

- 客户端为同一次业务尝试生成稳定的 `operation_id`
- 服务端以 `(access_key_id, tool_name, operation_id)` 作为去重键
- 相同 key 的重复请求直接返回首次结果，不重复执行副作用
- 未提供 `operation_id` 的请求按普通调用处理，不保证幂等

**限流与审计**：

- 限流：基于 Access Key 的令牌桶 QPS 限制（默认 100 QPS）
- 审计：所有写操作记录审计日志（操作人、时间、目标、结果、operation_id）

### 3.2 知识图谱存储模块

**职责**：以知识图谱范式存储项目知识，包含节点（实体）、边（关系）、属性、向量索引。

**建模范式**：属性图（Property Graph）。节点代表知识实体，边代表实体间关系，节点和边均可携带属性。这是知识图谱的标准建模方式，区别于传统关系表的扁平存储。

**知识分层**：

| 层级 | 存储内容 | 模型特征 | 检索方式 |
|------|----------|----------|----------|
| 项目画像层 | 项目描述、技术栈、架构、约定、规范 | 扁平节点，单项目一条 | 按 project_id 直接取 |
| 知识实体层 | 需求、代码片段、实现方案、设计意图、决策、踩坑 | 图节点，每个节点携带向量与全文索引 | 向量检索 + 全文检索 |
| 知识关系层 | 实体间关系（实现、依赖、追溯、冲突等） | 图的边，边本身携带属性与语义 | Cypher 图遍历 |

**节点类型**：

| 节点类型 | 标签 | 说明 |
|----------|------|------|
| 项目画像 | `ProjectProfile` | 项目级元信息，扁平集中存储 |
| 需求 | `Requirement` | 标准化需求描述 |
| 代码片段 | `CodeSnippet` | 类名、常量、函数签名、模块职责描述、关键代码片段 |
| 实现方案 | `Solution` | 技术方案描述 |
| 设计意图 | `DesignIntent` | 设计动机与权衡 |
| 决策记录 | `Decision` | 架构/技术选型决策 |
| 踩坑记录 | `Pitfall` | 历史问题与解法 |

**代码片段存储边界**：Mem Lake 存储代码的知识描述（类名、职责、接口签名、关键片段），不存储完整代码文件。代码内容归属 Git，Mem Lake 通过 `file_path` 属性引用。

**边（关系）类型**：

| 关系类型 | 方向 | 语义 |
|----------|------|------|
| `implements` | Requirement → CodeSnippet | 需求由哪些代码实现 |
| `depends_on` | CodeSnippet → CodeSnippet | 代码模块间依赖 |
| `realized_by` | CodeSnippet → Solution | 代码由什么方案实现 |
| `embodies` | Solution → DesignIntent | 方案体现什么意图 |
| `traces_to` | DesignIntent → Decision | 意图追溯到决策 |
| `conflicts_with` | Requirement ↔ Requirement | 需求间冲突 |
| `duplicates` | Requirement ↔ Requirement | 需求重复 |
| `relates_to` | Requirement → Requirement | 需求间关联 |
| `supersedes` | Requirement → Requirement | 新版本替代旧版本 |
| `version_of` | Requirement → Requirement | 版本关系 |
| `described_by` | CodeSnippet → Pitfall | 代码关联踩坑 |
| `references` | 任意 → 任意 | 通用引用 |

**写入流程**：所有写操作统一经过审批工作流（详见 3.4 节），核心要点：

- 写工具调用后，内容暂存于 `approval_batch` + `approval_item`，不直接写入 `knowledge_node` 表与 AGE 图
- 未审批内容不参与三引擎检索
- 向量延迟生成：审批通过时才调用 Embedding 服务
- 数据模型详见第 4 节

### 3.3 三引擎检索

**职责**：对外提供向量语义检索、关键词全文检索、图遍历三种检索能力，支持融合排序。

**三引擎分工**：

| 引擎 | 实现 | 适用场景 |
|------|------|----------|
| 向量语义检索 | pgvector HNSW 索引 | "找描述用户登录的需求" |
| 关键词全文检索 | PostgreSQL tsvector + GIN | "找包含 JWT 的知识" |
| 图遍历 | Apache AGE Cypher | "需求 R1 实现涉及哪些代码、设计意图" |

**融合检索**：

- 向量检索与全文检索结果通过 RRF（Reciprocal Rank Fusion）算法融合排序
- 图遍历作为独立检索路径，可与其他引擎结果组合（先向量找起点，再图遍历展开）
- 支持过滤条件：项目、角色权限、知识状态（仅 approved）、标签、时间范围
- 返回结果：节点 ID + 原文摘要 + 相关度分数 + 元数据

**图遍历查询能力**：

- 多跳遍历：需求 → 代码 → 方案 → 意图 → 决策（支持 5 跳以内）
- 影响范围分析：从需求出发，遍历所有关联代码及其依赖
- 子图提取：按需求 ID 拉取完整实现子图
- 路径查询：使用 AGE `shortest_path` 函数查找实体间最短路径
- 模式匹配：使用 Cypher 谓词函数（`all`/`any`/`none`/`single`）匹配图模式

### 3.4 知识审批工作流

**职责**：以提交批次为审批单元，建立知识全生命周期治理闭环，确保未审批的知识不可检索。

**审批单元**：一次写工具调用产生一个审批批次（approval_batch），批次内包含若干审批项（approval_item）。批次是审批的最小单元，保证知识的原子性。

**批次类型**：

| 批次类型 | 来源工具 | 内容 |
|----------|----------|------|
| `publish_requirement` | publish_requirement | 需求节点 + 版本关系 + 关联关系 |
| `submit_dev_artifacts` | submit_dev_artifacts | 代码片段 + 方案 + 意图 + 踩坑 + 实现关系 |
| `update_requirement_relations` | update_requirement_relations | 需求间关系（冲突/关联/替代） |

**暂存机制**：

- 未审批内容暂存于 `approval_item.payload`，不写入 `knowledge_node` 表与 AGE 图
- 审批通过前，暂存内容不参与三引擎检索（向量、全文、图遍历）
- 向量延迟生成：审批通过时才调用 Embedding 服务，避免无效计算

**状态机**：

```
Agent 写工具调用
  → 创建 approval_batch（status = pending_review）
  → 所有节点和关系作为 approval_item 暂存
  → 返回 batch_id

管理员 Agent 审批
  ├─ review_approve(batch_id)
  │   → 开启 PG 事务
  │   → 遍历 approval_items：
  │   │   ├─ node → 写入 knowledge_node（status=approved）+ 生成向量 + 更新全文索引
  │   │   └─ edge → 写入 AGE 图（建立边）
  │   → 更新 approval_batch.status = approved，记录 reviewed_by 与 reviewed_at
  │   → 事务提交（任一步骤失败则全部回滚）
  │
  └─ review_reject(batch_id, reason)
      → 更新 approval_batch.status = rejected，记录 reviewed_by、reviewed_at、review_comment
      → 不写入正式图谱
      → approval_item 保留用于追溯
```

**检索可见性**：三引擎检索只查询 `knowledge_node` 表中 `status = approved` 的节点，以及 AGE 图中已审批节点的边。未审批内容不可见。

**特殊规则**：admin 角色直接写入 ProjectProfile 节点，状态直接置为 approved，不产生审批批次。

**冲突检测**：审批通过时，基于标题向量相似度 + 标签匹配检测与已有知识的冲突，相似度高于阈值则在审批记录中标记冲突提示，由管理员决策是否通过。

### 3.5 权限与访问控制

**职责**：基于业务角色的访问控制 + 项目级数据隔离。

**RBAC 模型**：

- 一个用户（Access Key）只属于一个角色
- 一个角色可对应多个用户
- 一个角色绑定一个工具集
- 角色定义对应实际研发团队职能（管理员、产品经理、开发者）

**角色与工具集映射**：

| 角色 | 绑定工具集 | 职能 |
|------|------------|------|
| `admin` | 全部工具（含审批与密钥管理） | 系统管理、知识审批与治理 |
| `pm` | publish_requirement、search_similar_requirements、analyze_impact_scope、check_requirement_conflicts、update_requirement_relations、get_project_profile、get_requirement_context、get_role_skills | 需求发布与需求关系治理 |
| `dev` | get_project_profile、get_requirement_context、search_code_snippets、submit_dev_artifacts、search_similar_requirements、analyze_impact_scope、get_role_skills | 开发产物反馈与知识检索 |

**项目级隔离**：

- 基于 PostgreSQL 行级安全（RLS）实现
- 每个知识节点归属项目，跨项目默认不可见
- Access Key 绑定角色 + 项目范围
- 管理员可创建/吊销 Access Key，指定角色与项目范围

**管理员治理**：管理员通过自身的管理员 Agent 调用 MCP 工具集完成知识审批与系统管理，不提供独立的管理控制台。所有治理操作通过 MCP 工具接口完成（工具清单见 6.1 节管理员工具表）。

---

## 4. 数据模型设计

### 4.1 模型总览

Mem Lake 采用知识图谱建模范式，物理存储基于 PostgreSQL 单实例。节点内容存储于 PG 关系表（享受向量与全文索引能力），节点间关系存储于 Apache AGE 图（享受 Cypher 图遍历能力）。两者通过节点 ID 关联，在同一事务内写入，保证一致性。

```
PostgreSQL 17 单实例
├── 关系层（PG 原生）
│   ├── knowledge_node 表        ← 图的节点（内容 + 向量 + 全文）
│   ├── project 表               ← 项目元信息
│   ├── access_key 表            ← 访问密钥
│   ├── approval_batch 表        ← 审批批次（待审批/已审批）
│   ├── approval_item 表         ← 审批项（批次内具体变更内容）
│   ├── audit_log 表             ← 审计日志
│   └── knowledge_version 表     ← 知识版本历史
├── 向量层（pgvector）
│   └── knowledge_node.content_vector（HNSW 索引）
├── 全文层（PG 原生）
│   └── knowledge_node.content_tsv（GIN 索引）
└── 图层（Apache AGE）
    └── mem_lake_graph 图        ← 节点引用 + 边（关系）
        ├── 节点：引用 knowledge_node.id
        └── 边：implements / depends_on / realized_by / ...
```

### 4.2 节点主表 Schema

节点统一存储于 `knowledge_node` 表，通过 `type` 字段区分实体类型，通过 `properties` JSONB 存储类型特有属性。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 主键 |
| project_id | UUID | 所属项目 |
| type | TEXT | 节点类型：ProjectProfile / Requirement / CodeSnippet / Solution / DesignIntent / Decision / Pitfall |
| title | TEXT | 节点标题 |
| content | TEXT | 节点正文内容 |
| content_vector | VECTOR(1024) | 内容向量（bge-large-zh-v1.5，1024 维） |
| content_tsv | TSVECTOR | 全文检索向量 |
| properties | JSONB | 类型特有属性（schema 规范见 4.4 节） |
| tags | JSONB | 标签数组 |
| source | JSONB | 来源信息（Agent、工具、原始文档引用） |
| status | TEXT | 节点状态：approved / archived |
| version | INT | 版本号，从 1 开始递增 |
| created_by | TEXT | 提交者（Access Key 标识） |
| created_at | TIMESTAMPTZ | 创建时间 |
| is_deleted | BOOLEAN | 软删除标记，默认 false |

**索引设计**：

```sql
-- 全文检索索引
CREATE INDEX idx_node_tsv ON knowledge_node USING GIN (content_tsv);

-- 向量检索索引（HNSW， cosine 距离）
CREATE INDEX idx_node_vector ON knowledge_node
  USING hnsw (content_vector vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- 业务查询索引
CREATE INDEX idx_node_project_type_status ON knowledge_node (project_id, type, status);
CREATE INDEX idx_node_project_tags ON knowledge_node USING GIN (tags);
```

**检索参数**：HNSW 检索时 `ef_search` 默认 40，按场景可调。

### 4.3 图（AGE）设计

**图名称**：`mem_lake_graph`（单图 + project_id 属性隔离）

**节点**：AGE 图中的节点引用 `knowledge_node.id`，不重复存储节点内容。节点标签对应 `knowledge_node.type`。

**边**：边存储于 AGE 图，携带关系类型与边属性。边的属性示例：

```json
{
  "created_by": "agent-key-xxx",
  "created_at": "2026-08-01T10:00:00Z",
  "reason": "需求迭代替代旧版本"
}
```

**多项目隔离**：采用单图 + `project_id` 属性隔离。所有节点与边携带 `project_id` 属性，查询时通过 WHERE 条件过滤，跨项目查询需显式指定。

**Cypher 查询示例**（影响范围分析）：

```cypher
-- 需求 R1 的影响范围：实现代码 + 依赖代码 + 方案 + 意图
SELECT * FROM cypher('mem_lake_graph', $$
  MATCH (r:Requirement {id: 'R1', project_id: 'proj-001'})
        -[:implements]->(c:CodeSnippet)
        -[:realized_by]->(s:Solution)
        -[:embodies]->(d:DesignIntent)
  OPTIONAL MATCH (c)-[:depends_on]->(dep:CodeSnippet)
  RETURN r, c, s, d, dep
$$) AS (r agtype, c agtype, s agtype, d agtype, dep agtype);
```

### 4.4 节点属性 Schema 规范

每类节点的 `properties` JSONB 字段遵循以下 schema 规范。这些规范作为 MCP 接口契约的一部分，上游 Agent 提交时需遵循，Mem Lake 负责校验。

**ProjectProfile**

```json
{
  "name": "Mem Lake",
  "description": "多智能体协作记忆基础设施",
  "tech_stack": ["Python", "PostgreSQL", "pgvector", "Apache AGE"],
  "architecture": "三层架构",
  "conventions": ["PEP8", "Conventional Commits"],
  "team": {"pm": [], "dev": []}
}
```

**Requirement**

```json
{
  "requirement_id": "REQ-2026-001",
  "priority": "P0",
  "module": "auth",
  "acceptance_criteria": ["支持账号密码登录", "支持SSO"],
  "stakeholders": ["PM张三"],
  "source_doc": "docs/prd/auth.md",
  "version": "1.0"
}
```

**CodeSnippet**

```json
{
  "name": "LoginService",
  "type": "class",
  "responsibility": "处理用户登录鉴权逻辑",
  "file_path": "src/auth/login.py",
  "repo": "my-project",
  "branch": "main",
  "signature": "class LoginService: def login(user, pwd) -> Token",
  "snippet": "def login(self, user, pwd):\n    ...",
  "language": "python"
}
```

**Solution**

```json
{
  "approach": "JWT令牌签发 + 中间件校验",
  "alternatives": ["Session方案（否决：不便于横向扩展）"],
  "version": "1.0"
}
```

**DesignIntent**

```json
{
  "rationale": "无状态、易扩展、适配微服务",
  "trade_offs": "牺牲了主动失效能力，通过黑名单补偿",
  "references": ["DEC-001"]
}
```

**Decision**

```json
{
  "decision_id": "DEC-001",
  "context": "需要支持多服务部署",
  "decision": "采用JWT",
  "alternatives": ["Session", "OAuth2"],
  "consequences": "需实现token黑名单",
  "date": "2026-08-01",
  "decider": "架构师张三"
}
```

**Pitfall**

```json
{
  "symptom": "高并发下token续期导致...",
  "root_cause": "缺少分布式锁",
  "solution": "引入Redis分布式锁",
  "severity": "high"
}
```

### 4.5 其他表 Schema

**project 表**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 主键 |
| name | TEXT | 项目名称 |
| description | TEXT | 项目描述 |
| created_at | TIMESTAMPTZ | 创建时间 |

**approval_batch 表**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 主键 |
| project_id | UUID | 归属项目 |
| batch_type | TEXT | 批次类型：publish_requirement / submit_dev_artifacts / update_requirement_relations |
| submitted_by | TEXT | 提交者 Access Key ID |
| submitter_role | TEXT | 提交者角色：pm / dev |
| summary | TEXT | 自动生成的提交摘要（提交时生成） |
| status | TEXT | pending_review / approved / rejected |
| submitted_at | TIMESTAMPTZ | 提交时间 |
| reviewed_by | TEXT | 审核人 Access Key ID |
| reviewed_at | TIMESTAMPTZ | 审核时间 |
| review_comment | TEXT | 审核意见（拒绝时填写） |
| conflict_hint | JSONB | 冲突检测提示（审批通过时生成） |
| operation_id | TEXT | 幂等操作标识 |

**approval_item 表**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 主键 |
| batch_id | UUID | 关联的审批批次 ID |
| seq | INT | 批次内序号 |
| item_type | TEXT | node / edge |
| action | TEXT | create / update / delete |
| entity_type | TEXT | 节点类型（Requirement/CodeSnippet/...）或关系类型（implements/conflicts_with/...） |
| payload | JSONB | 完整内容（节点属性或关系数据，审批通过后写入正式存储） |
| target_id | UUID | 审批通过后写入的实际节点 ID（回填） |
| created_at | TIMESTAMPTZ | 创建时间 |

**索引设计**：

```sql
CREATE INDEX idx_approval_batch_status ON approval_batch (project_id, status, submitted_at);
CREATE INDEX idx_approval_item_batch ON approval_item (batch_id, seq);
```

**knowledge_version 表**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 主键 |
| node_id | UUID | 知识节点 ID |
| version | INT | 版本号 |
| snapshot | JSONB | 该版本完整快照 |
| created_by | TEXT | 提交者 |
| created_at | TIMESTAMPTZ | 创建时间 |

**access_key 表**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 主键 |
| key_hash | TEXT | Access Key 哈希（bcrypt，不存明文） |
| role | TEXT | 业务角色：admin / pm / dev |
| project_scope | JSONB | 可访问的项目 ID 列表 |
| status | TEXT | active / revoked |
| created_at | TIMESTAMPTZ | 创建时间 |
| revoked_at | TIMESTAMPTZ | 吊销时间 |

**audit_log 表**

| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID | 主键 |
| actor | TEXT | 操作人（Access Key 标识） |
| action | TEXT | 操作类型（write / update / approve / reject / archive） |
| target_type | TEXT | 目标类型（node / edge / access_key） |
| target_id | UUID | 目标 ID |
| operation_id | TEXT | 幂等操作标识（可选，写操作携带） |
| detail | JSONB | 操作详情 |
| created_at | TIMESTAMPTZ | 操作时间 |

---

## 5. 关键技术方案

### 5.1 存储扩展与高可用

**面向中型团队（v1.0）**：

- PostgreSQL 17 单实例，满足单项目万级节点、十万级边的规模
- 向量索引按项目分区（通过 `project_id` 过滤 + 索引优化），避免单索引过大
- 定期 pg_dump 物理备份 + WAL 归档，支持时间点恢复

**面向大规模（v2.0+）**：

- 引入 PostgreSQL 只读副本实现读写分离（检索走副本，写入走主库）
- 知识内容冷热分层：近期数据在主库，历史数据归档至对象存储
- PostgreSQL 按项目 hash 分库分表，突破单库容量上限

### 5.2 数据加密与安全

**方案**：

- 传输加密：MCP 客户端到网关强制 TLS，内网通信也加密
- 存储加密：采用磁盘级加密（LUKS on Linux），数据库文件落盘即加密
- Access Key 存储：仅存哈希（bcrypt），不存明文
- 敏感元数据：字段级加密，应用层使用本地密钥加密后写入 `properties` JSONB
- 审计日志：追加写，不可修改（应用层禁止 UPDATE 操作）
- 密钥管理：本地密钥文件 + 环境变量注入，密钥文件权限 600

### 5.3 审批流工程化

审批流核心流程见 3.4 节。本节补充工程化实现细节。

**冲突检测算法**：

- 对批次内所有节点的 `title` 字段生成向量
- 与项目内已有 `approved` 状态的同类型节点做向量相似度查询
- 相似度阈值：0.85（可配置）
- 超过阈值的结果写入 `conflict_hint`，包含冲突节点 ID 与相似度分数
- 冲突提示不影响审批通过，由管理员决策

**SLA 与超期标记**：

- 待审批超过 7 天：`review_pending_list` 返回结果中标记为"即将超期"
- 待审批超过 30 天：`review_pending_list` 返回结果中标记为"超期"
- 超期标记不影响审批功能，仅作为管理员优先级判断依据

**临时引用解析**：提交时节点与关系的临时引用（`from_ref` / `to_ref`）在 payload 中保留，审批通过时在事务内解析为实际节点 ID 后写入 AGE 图。

### 5.4 检索性能优化

**方案**：

- 全文索引：GIN 索引，支持中文分词（配置 `zhparser` 扩展）
- 向量索引：HNSW（`m=16, ef_construction=64`，`ef_search=40`）
- 融合排序在应用层完成（RRF 算法，纯计算无 DB 压力）
- 图遍历：利用 AGE VLE（变长边遍历）缓存优化，限制最大遍历深度为 5 跳
- 分页检索：采用游标分页，避免大 offset 性能问题
- 向量召回 Top-K：默认 50，融合后返回 Top-N（默认 10）

**目标指标**：

- 混合检索延迟 < 1s（p99）
- 图遍历（3 跳）延迟 < 500ms（p99）
- 写入延迟（含向量化）< 500ms（p99）

### 5.5 本地化部署方案

**方案**：

- Docker Compose 编排，单机部署
- 基础镜像：`apache/age`（含 AGE 扩展），通过自定义 Dockerfile 安装 pgvector 与 zhparser 扩展后构建
- 离线镜像包：包含所有依赖镜像 + 数据库初始化脚本 + Embedding 模型文件
- Embedding 模型以独立容器形式提供，模型文件随包分发
- 配置项通过环境变量 + YAML 配置文件管理
- 升级流程：滚动升级 + 数据迁移脚本，支持版本回退

---

## 6. 接口设计

### 6.1 MCP 工具接口

MCP 工具按业务角色场景化设计，参数由 Mem Lake 定义。所有工具声明 `outputSchema` 并返回 `structuredContent`，通过 Tool Annotations 标记 `readOnlyHint` 区分读写操作。写操作支持 `operation_id` 幂等参数。工具集与角色绑定关系见 3.5 节。

**产品经理（pm）场景工具**

| 工具名 | 功能 | 只读 |
|--------|------|------|
| `publish_requirement` | 发布需求节点（含关联关系），产生审批批次 | 否 |
| `search_similar_requirements` | 语义检索相似历史需求 | 是 |
| `analyze_impact_scope` | 图遍历分析需求影响范围 | 是 |
| `check_requirement_conflicts` | 检测需求冲突与重复 | 是 |
| `update_requirement_relations` | 更新需求间关系（冲突/关联/替代），产生审批批次 | 否 |
| `get_project_profile` | 获取项目画像 | 是 |
| `get_requirement_context` | 按需求 ID 拉取完整实现子图 | 是 |
| `get_role_skills` | 获取角色 Skills 指导文档（见第 8 章） | 是 |

**开发者（dev）场景工具**

| 工具名 | 功能 | 只读 |
|--------|------|------|
| `get_project_profile` | 获取项目画像 | 是 |
| `get_requirement_context` | 按需求 ID 拉取完整实现子图 | 是 |
| `search_code_snippets` | 语义检索代码片段 + 关联方案/意图 | 是 |
| `submit_dev_artifacts` | 批量提交开发产物（代码+方案+意图+关系），产生审批批次 | 否 |
| `search_similar_requirements` | 语义检索相似历史需求 | 是 |
| `analyze_impact_scope` | 图遍历分析需求影响范围 | 是 |
| `get_role_skills` | 获取角色 Skills 指导文档（见第 8 章） | 是 |

**管理员（admin）场景工具**

admin 角色拥有全部工具（含 pm 与 dev 工具集），下表仅列出管理员专属工具：

| 工具名 | 功能 | 只读 |
|--------|------|------|
| `review_pending_list` | 待审批批次队列（含摘要、提交者、时间、超期标记） | 是 |
| `review_batch_detail` | 查看批次内所有审批项的完整内容 | 是 |
| `review_approve` | 审批通过批次（原子性写入图谱） | 否 |
| `review_reject` | 审批退回批次（附原因） | 否 |
| `list_knowledge` | 按条件分页列出已审批知识 | 是 |
| `query_audit_log` | 审计日志查询 | 是 |
| `manage_access_key` | 创建/吊销/查看 Access Key，指定角色与项目范围 | 否 |
| `get_role_skills` | 获取角色 Skills 指导文档（见第 8 章） | 是 |

**协议方法**：除工具调用外，MCP 网关实现 `server/discover` 方法，返回服务端协议版本、能力清单与实现信息，供客户端预发现。

**工具入参示例**（`publish_requirement`，含幂等参数）：

```json
{
  "project_id": "proj-001",
  "operation_id": "op_01K_PUBLISH_REQ_001",
  "requirement": {
    "title": "用户登录鉴权",
    "content": "实现基于JWT的用户登录...",
    "properties": {
      "requirement_id": "REQ-2026-001",
      "priority": "P0",
      "module": "auth",
      "acceptance_criteria": ["支持账号密码登录", "支持SSO"],
      "source_doc": "docs/prd/auth.md",
      "version": "1.0"
    },
    "tags": ["auth", "P0"]
  },
  "related": {
    "supersedes": ["REQ-2025-003"],
    "relates_to": ["REQ-2026-002"]
  }
}
```

**工具出参示例**（`publish_requirement`）：

```json
{
  "batch_id": "01K_BATCH_001",
  "status": "pending_review",
  "submitted_at": "2026-08-01T10:00:00Z",
  "item_count": 3
}
```

写工具调用统一返回 `batch_id`、`status`、`submitted_at`、`item_count`，调用方据此次追踪审批进度。

**工具入参示例**（`submit_dev_artifacts`，含幂等参数与临时引用）：

```json
{
  "project_id": "proj-001",
  "requirement_id": "REQ-2026-001",
  "operation_id": "op_01K_SUBMIT_DEV_001",
  "artifacts": {
    "code_snippets": [
      {"ref": "LoginService", "name": "LoginService", "type": "class", ...}
    ],
    "solutions": [
      {"ref": "JWT方案", "title": "JWT令牌方案", ...}
    ],
    "design_intents": [
      {"ref": "JWT意图", "title": "选择JWT而非Session的原因", ...}
    ]
  },
  "relations": [
    {"from": "REQ-2026-001", "type": "implements", "to_ref": "LoginService"},
    {"from_ref": "LoginService", "type": "realized_by", "to_ref": "JWT方案"},
    {"from_ref": "JWT方案", "type": "embodies", "to_ref": "JWT意图"}
  ]
}
```

Mem Lake 负责将 `from_ref`/`to_ref` 临时引用解析为实际节点 ID 并建立图关系。

### 6.2 认证鉴权

认证模型与鉴权链路见 3.1 节。所有角色（含管理员）统一通过 MCP Access Key 认证。

**请求头规范**：

| Header | 说明 | 示例 |
|--------|------|------|
| `X-MCP-Key` | Access Key 认证凭证 | `ak_xxx...` |
| `Mcp-Method` | 调用的 MCP 方法 | `tools/call` |
| `Mcp-Name` | 服务端名称 | `mem-lake` |
| `MCP-Protocol-Version` | 协议版本 | `2026-07-28` |

---

## 7. 应用场景设计

### 7.1 软件研发场景（核心场景）

**产品经理 Agent 流程**：

```
解析 PRD
  → search_similar_requirements 检索历史相似需求
  → check_requirement_conflicts 检测冲突
  → analyze_impact_scope 分析新需求影响范围
  → publish_requirement 发布结构化需求节点 + 关联关系（产生审批批次）
  → update_requirement_relations 维护需求间关系（产生审批批次）
```

**开发者 Agent 流程**：

```
接到任务
  → get_project_profile 获取项目整体认知
  → get_requirement_context 拉取需求实现子图（代码+方案+意图）
  → search_code_snippets 检索可复用的老代码模块
开发完成
  → submit_dev_artifacts 批量提交代码片段+方案+意图+关系（产生审批批次）
```

**管理员 Agent 流程**：

```
  → review_pending_list 查看待审批批次队列
  → review_batch_detail 查看批次内完整内容
  → review_approve / review_reject 审批决策
  → list_knowledge 浏览已审批知识库
  → query_audit_log 审计追溯
  → manage_access_key 管理密钥与角色绑定
  → admin 可直接写入 ProjectProfile（不经审批）
```

### 7.2 多行业适配方向（远期）

制造业、金融业、国央企并非"软件研发团队"，需要重新定义角色与知识 schema。该方向作为远期演进，当前版本聚焦软件研发场景。

### 7.3 研发流程其他角色

测试 Agent、运维 Agent、架构师 Agent 等可通过 admin 创建对应业务角色与工具集接入。知识 schema 通用，通过 `type` 字段与 `tags` 区分角色产出。新角色需在 3.5 节角色与工具集映射表中登记。

---

## 8. 角色 Skills 规划

### 8.1 Skills 定位

Skills 是 Mem Lake 提供给上游 Agent 的角色使用指导文档，目的是让上游 Agent 知道如何有效地扮演对应角色并正确使用 Mem Lake 的 MCP 工具集。

- **形式**：结构化文档，可被上游 Agent 加载为系统提示或参考上下文
- **职责边界**：Mem Lake 负责提供与分发 Skills，不执行 Skills 内容
- **版本化**：Skills 支持版本管理，随系统迭代更新
- **优先级**：本章为低优先级规划项，Skills 内容产出在 v1.x 阶段随各角色 Agent 接入实践逐步形成

### 8.2 Skills 分发

通过 MCP 工具 `get_role_skills` 获取对应角色的 Skills 文档。上游 Agent 初始化时调用该工具加载角色指导，作为后续调用 Mem Lake 工具集的上下文依据。

### 8.3 产品经理 Agent Skills

| Skill | 指导内容 |
|------|---------|
| 需求结构化 | 如何将 PRD 解析为符合 Requirement schema 规范的结构化数据 |
| 历史检索 | 如何使用 `search_similar_requirements` 检索并解读相似历史需求 |
| 影响分析 | 如何使用 `analyze_impact_scope` 解读影响范围并形成可行性判断 |
| 冲突检测 | 如何使用 `check_requirement_conflicts` 判断需求冲突与重复 |
| 关系维护 | 如何使用 `update_requirement_relations` 维护需求间冲突/关联/替代关系 |
| 需求发布 | 如何使用 `publish_requirement` 发布需求并建立版本关系 |

### 8.4 开发者 Agent Skills

| Skill | 指导内容 |
|------|---------|
| 项目认知 | 如何使用 `get_project_profile` 建立项目整体认知 |
| 需求理解 | 如何使用 `get_requirement_context` 解读需求实现子图（代码+方案+意图） |
| 代码检索 | 如何使用 `search_code_snippets` 检索可复用代码与关联踩坑记录 |
| 产物组织 | 如何组织 CodeSnippet/Solution/DesignIntent 的结构化反馈内容 |
| 批量提交 | 如何使用 `submit_dev_artifacts` 建立临时引用关系并一次性提交完整开发产物 |

### 8.5 管理员 Agent Skills

| Skill | 指导内容 |
|------|---------|
| 审批队列管理 | 如何使用 `review_pending_list` 管理待审批批次队列，识别超期标记 |
| 批次内容审查 | 如何使用 `review_batch_detail` 查看批次内完整内容并判断知识质量 |
| 审批决策 | 如何使用 `review_approve`/`review_reject` 进行审批决策，处理冲突提示 |
| 知识巡检 | 如何使用 `list_knowledge` 浏览已审批知识库发现异常与遗漏 |
| 审计追溯 | 如何使用 `query_audit_log` 进行操作追溯与合规检查 |
| 角色与密钥管理 | 如何使用 `manage_access_key` 创建密钥、分配业务角色与项目范围 |
| 项目画像管理 | 如何直接写入 ProjectProfile（不经审批流程） |

---

## 9. 非功能性设计

### 9.1 性能指标

| 指标 | 目标值 | 说明 |
|------|--------|------|
| 混合检索延迟 | <1s（p99） | 向量 + 全文融合检索端到端 |
| 图遍历延迟（3 跳） | <500ms（p99） | AGE Cypher 多跳遍历 |
| 写入延迟 | <500ms（p99） | 含本地 Embedding 向量化 |
| 并发 | 100 QPS | 面向中型团队（50-100 人研发团队） |
| 单项目存储容量 | 万级节点、十万级边 | 满足中型项目全生命周期 |
| 向量召回 Top-K | 50 | 融合后返回 Top-10 |

### 9.2 安全合规

- 完全本地化部署，数据不出内网
- 传输加密（TLS）+ 存储加密（磁盘级）
- 权限控制（RBAC + RLS）+ 审计日志
- Access Key 哈希存储，敏感字段加密
- ISO 27001 认证获取作为中期合规目标

### 9.3 可扩展性

- 存储层支持水平扩展：只读副本 + 分库分表（v2.0+）
- 检索引擎通过抽象接口设计，向量后端可替换为专用向量数据库（远期）
- 图存储通过 GraphStore 抽象层，后端可替换为 Neo4j（远期）

### 9.4 可观测性

- 结构化日志（JSON 格式），区分操作日志与系统日志
- Prometheus 指标暴露（检索延迟、写入延迟、审批队列长度、节点/边数量）
- 健康检查接口（`/health`，检查 DB 连接、AGE 扩展、Embedding 服务状态）
- 审计日志独立于系统日志，不可修改

---

## 10. 演进路线

### 10.1 阶段规划

| 阶段 | 目标 |
|------|------|
| v1.0（MVP） | MCP 网关 + 知识图谱存储 + 三引擎检索 + 审批流 + 管理员 Agent 治理工具集，Docker Compose 本地化部署可跑通 |
| v1.x | 审批流工程化深化、性能调优、备份恢复、K8s 部署支持、角色 Skills 内容产出 |
| v2.0 | ISO 认证、只读副本读写分离、自动审批规则、生态扩展（开发者工具包） |
| 远期 | 多行业适配、轻量化版本、多模态理解、IDE 深度集成 |

### 10.2 GraphStore 抽象层

为保留图存储后端的可演进性，Mem Lake 在 AGE 之上设计 `GraphStore` 抽象接口，定义图操作原语：

| 接口方法 | 功能 |
|----------|------|
| `add_node(id, labels, properties)` | 添加节点 |
| `add_edge(from, to, type, properties)` | 添加边 |
| `neighbors(id, edge_type, depth)` | 邻居遍历 |
| `find_path(from, to, max_depth)` | 路径查询 |
| `match_pattern(pattern)` | 图模式匹配 |
| `subgraph(node_ids)` | 子图提取 |

v1.0 实现为基于 Apache AGE 的 `AGEGraphStore`。未来若需替换为 Neo4j 等其他图后端，只需新增实现类，业务代码无感。

---

## 11. 附录

### 11.1 术语表

| 术语 | 含义 |
|------|------|
| MCP | Model Context Protocol，模型上下文协议 |
| RRF | Reciprocal Rank Fusion，倒数排名融合 |
| RBAC | Role-Based Access Control，基于角色的访问控制 |
| RLS | Row Level Security，行级安全（PostgreSQL 特性） |
| AGE | Apache A Graph Extension，PostgreSQL 图数据库扩展 |
| pgvector | PostgreSQL 向量检索扩展 |
| HNSW | Hierarchical Navigable Small World，向量索引算法 |
| VLE | Variable Length Edges，AGE 变长边遍历 |
| Property Graph | 属性图，知识图谱标准建模范式（节点+边+属性） |
| Cypher | 图查询语言，openCypher 标准 |
| Knowledge Node | 知识节点，Mem Lake 图谱中的实体 |
| Knowledge Edge | 知识边，Mem Lake 图谱中的关系 |

### 11.2 节点类型与关系类型速查

**节点类型**：ProjectProfile / Requirement / CodeSnippet / Solution / DesignIntent / Decision / Pitfall

**关系类型**：implements / depends_on / realized_by / embodies / traces_to / conflicts_with / duplicates / relates_to / supersedes / version_of / described_by / references

### 11.3 三引擎技术栈

| 引擎 | 技术 | 作用 |
|------|------|------|
| 向量语义检索 | pgvector（HNSW 索引） | 语义相似度检索 |
| 关键词全文检索 | PostgreSQL tsvector + GIN | 精确关键词检索 |
| 图遍历 | Apache AGE（Cypher） | 多跳关系遍历 |

三引擎共存于 PostgreSQL 17 单实例，共享事务与存储。

### 11.4 项目基础环境

#### 开发环境

| 项目 | 版本/配置 |
|------|-----------|
| 操作系统 | Windows + WSL2（Ubuntu，数据存储于 D 盘） |
| Python | 3.11（Conda 环境名：`memlake`） |
| Docker | Docker Desktop 4.84.0（Engine 29.6.2，WSL2 后端） |
| Docker Compose | v5.3.1 |
| Docker 镜像加速器 | `docker.1ms.run`、`docker.xuanyuan.me` |

#### Python 依赖清单

| 依赖 | PyPI 包名 | 版本 | 用途 |
|------|-----------|------|------|
| FastMCP | `fastmcp` | 3.4.5 | MCP 网关框架 |
| SQLAlchemy | `sqlalchemy[asyncio]` | 2.0.51 | ORM 与数据库会话管理 |
| psycopg | `psycopg[binary,pool]` | 3.3.4 | PostgreSQL 异步驱动 |
| Apache AGE Python 驱动 | `apache-age-python` | 0.0.7 | AGE Cypher 执行与 AGType 解析（导入名 `age`） |
| pgvector | `pgvector` | 0.5.0 | PostgreSQL 向量列访问 |
| sentence-transformers | `sentence-transformers` | 5.6.1 | bge-large-zh-v1.5 模型加载与推理 |
| pydantic | `pydantic` | 2.13.4 | 数据模型校验 |
| bcrypt | `bcrypt` | 5.0.0 | Access Key 哈希 |
| PyYAML | `pyyaml` | 6.0.3 | YAML 配置解析 |

#### 开发工具链

| 工具 | 版本 | 用途 |
|------|------|------|
| pytest | 9.1.1 | 单元测试与集成测试 |
| pytest-asyncio | 1.4.0 | 异步测试支持 |
| pytest-cov | 7.1.0 | 测试覆盖率 |
| ruff | 0.16.1 | 代码风格检查与格式化 |
| mypy | 2.3.0 | 静态类型检查 |

#### Embedding 模型

| 项目 | 配置 |
|------|------|
| 模型 | BAAI/bge-large-zh-v1.5 |
| 维度 | 1024 |
| 下载源 | ModelScope（`AI-ModelScope/bge-large-zh-v1.5`），国内网络优先 |
| 备选下载源 | hf-mirror.com、BAAI 智源官方 |
| 部署方式 | 独立容器（FastAPI + sentence-transformers），模型文件挂载或打入镜像 |

#### 项目结构

```
mem_lake/
├── src/mem_lake/
│   ├── gateway/          # MCP 网关层（FastMCP）
│   │   ├── tools/        # 工具定义（pm/dev/admin）
│   ├── knowledge/        # 知识图谱存储模块
│   ├── search/           # 三引擎检索模块
│   ├── approval/         # 审批工作流模块
│   ├── auth/             # RBAC 与访问控制
│   ├── embedding/        # Embedding 服务客户端
│   ├── db/               # 数据库基础设施
│   └── audit/            # 审计日志
├── tests/                # 测试（unit + integration）
├── deploy/               # Docker 部署配置
└── pyproject.toml        # 依赖管理与构建配置
```

---

> 本文档为设计基线（v0.7），知识存储基于知识图谱建模范式，MCP 网关基于 2026-07-28 无状态规范设计，RBAC 采用业务角色模型（admin/pm/dev），审批流基于批次单元设计，技术栈经审核验证。后续将基于此基线进入各模块详细设计阶段。
