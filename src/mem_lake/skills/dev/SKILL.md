---
name: mem-lake-dev
description: "Mem Lake developer skills for submitting development artifacts (code snippets, solutions, design intents, pitfalls) to the team knowledge graph. Use when recording code implementations, design decisions, solutions, or pitfalls encountered during development. Triggers on: 代码片段, submit_dev_artifacts, 方案, 设计意图, 踩坑, CodeSnippet, Solution, DesignIntent, Pitfall, ref, 批量提交."
version: 1.5.0
---

# Dev Skills（开发者）

> 本文件参数表以工具实际签名（代码）为准；如与运行时 MCP 客户端展示不符，以代码为准。

## 数据信任说明

### 数据所有者
- **知识图谱数据**：由 admin 角色通过审批工作流管理
- **向量索引**：由系统自动生成，admin 可触发全量重建（reindex_project_vectors）

### 数据新鲜度
- **写入生效**：审批通过后节点立即生效，向量异步生成（可能有短暂延迟）
- **宽松模式**：无冲突时直接入库，向量后台异步生成
- **向量重建**：admin 可通过 reindex_project_vectors 触发全量重建

### 数据认证
- **审批即认证**：admin 审批通过的数据视为已认证
- **冲突检测**：三层冲突检测（L0/L1/L2/L3）保证数据质量
- **审计可追溯**：所有写操作记录审计日志，可追溯数据来源

### 使用建议
- 检索时注意向量可能有短暂延迟（刚提交的内容可能检索不到）
- 如需确保数据最新，可先用 get_project_info 检查项目状态
- 发现数据问题可用 update_node 修正（需重新审批）

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

你是当前项目的开发者。MemLake 是团队共享的知识记忆工具，你在工作中用它检索已有经验、沉淀产出。你提交的开发产物默认进入审批队列，admin 审批通过后正式写入知识图谱，供全团队所有 Agent 检索使用。若你的 Access Key 被设为宽松模式（lax_mode=true 且全局开关开启），提交会直接入库（返回 status="approved"），无需等到 admin。

核心价值：**让你的开发经验被团队所有 AI 共享**。没有 Mem Lake，你踩过的坑只有你的 AI 知道；有了 Mem Lake，其他开发者的 AI 能检索到你记录的坑和解决方案，新人 AI 也能快速了解项目的设计意图。

关键原则：你只负责提交，不负责审批。默认提交后获得 batch_id，等待 admin 审批通过；宽松模式下返回 approved 即已生效。提交后**不要立即检索**刚提交的内容——宽松模式向量在后台异步生成，刚提交瞬间可能检索不到；严格模式需等 admin 审批通过后才会写入图谱。

## 核心工作流：先检索后提交

提交开发产物前，**先查重、再提交**：
1. 用 `search_code_snippets(query=..., project_id=...)`（查代码/方案/意图/坑）和 `search_similar_requirements(...)`（查关联需求）检索已有相似内容；
2. 若命中已有节点：不要重复提交新节点，改用 `submit_dev_artifacts(...)` 的 `relations`（from_ref/to_ref 引用命中节点 UUID 或批次内 ref）建立 `depends_on`/`realized_by`/`embodies`/`traces_to`/`described_by` 等引用边，让新产物挂接到既有知识上；
3. 若未命中：再提交新产物。

> **实现前先看需求（system 维度）**：需求可按 `system_id` 隔离、且可能是"悬浮"（project 为空、先于实现）。要定位可见的 System 需求，用 `search_similar_requirements(project_id=...)` 或加 `system_id=...`（你被 admin 通过 `manage_system.bind_keys` 绑定的 system），拿到需求 UUID 后 `submit_dev_artifacts(requirement_id=UUID, ...)` 建 implements 边。

## 可用工具摘要

### 写入类
- **submit_dev_artifacts** — 批量提交开发产物（代码/方案/意图/坑）
- **update_node** — 修正已审批节点（PM/Dev 共享）

### 检索类
- **search_similar_requirements** — 检索相似需求（向量+全文融合）
- **search_code_snippets** — 检索研发资产（代码/方案/意图/坑）
- **analyze_impact_scope** — 分析变更影响范围
- **get_requirement_context** — 查询需求上下文

### 查询类
- **get_project_profile** — 查询项目画像
- **get_project_info** — 枚举/查询项目
- **get_role_skills** — 获取角色 Skills 文档

> **详细参数表和示例见 `dev/REFERENCE.md`**

## 关键要点

1. **每个产物必须声明 ref 名**：ref 是批次内的唯一引用名，relations 通过 ref 引用节点。

2. **relations 中 from_ref/to_ref 可以是 ref 名或已有节点 UUID**：
   - 引用本批次内新建的节点 → 用 ref 名
   - 引用知识图谱中已有的节点 → 用节点 UUID

3. **提交后不可修改**：批次一旦提交，内容不可修改。如需修改，只能等 admin 拒绝后重新提交。

4. **提交后如何跟进（不要轮询、不要立即检索）**：
   - **严格模式**：返回 `status="pending_review"`，拿到 batch_id 后只需告知用户「待 admin 审批」
   - **宽松模式**：返回 `status="approved"` + `decision="auto_approved"` 即已直接入库

5. **content 应包含实际代码或详细说明**：content 会用于向量生成，内容越详细检索越准确。

6. **批量提交优于多次单条提交**：一次批量提交多个产物 + relations，系统会在审批通过时同事务写入所有节点和边。

7. **properties 字段缺失会被 schema 校验拒绝**：submit_dev_artifacts 在工具层即校验各类型 properties 必填字段。

8. **tags 为精确标签，AND/OR 由 tags_op 控制**：希望语义相近召回时传 `semantic_tags=true`。

## 参考文件

详细工具参数表、嵌套结构定义和完整工作流示例见独立参考文件：

- **Dev**: 参考 `dev/REFERENCE.md`

按需加载方式：
1. 使用 `get_role_skills(role="dev")` 获取主文件
2. 如需详细工具参数或示例，加载 `dev/REFERENCE.md`
3. REFERENCE.md 仅在需要时加载，节省 context token
