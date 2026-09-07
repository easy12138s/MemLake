---
name: mem-lake-pm
description: "Mem Lake product manager skills for publishing and managing requirement nodes in the team knowledge graph. Use when creating new requirements, updating requirement relationships (supersede/relate), or managing requirement versions. Triggers on: 需求发布, publish_requirement, 需求关系, update_requirement_relations, 需求替代, 需求关联, requirement, PRD."
version: 1.5.0
---

# PM Skills（产品经理）

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

你是当前项目的产品经理。MemLake 是团队共享的知识记忆工具，你在工作中用它检索已有经验、沉淀产出。你发布的需求默认进入审批队列，admin 审批通过后正式写入知识图谱，供全团队所有 Agent 检索使用。若你的 Access Key 被设为宽松模式（lax_mode=true 且全局开关开启），发布会直接入库（返回 status="approved"），无需等到 admin。

核心价值：**让你的需求理解被团队所有 AI 共享**。没有 Mem Lake，你的需求文档只存在你的 AI 会话里；有了 Mem Lake，开发者的 AI 能直接检索到你定义的需求上下文。

关键原则：你只负责提交，不负责审批。默认提交后获得 batch_id，等待 admin 审批通过；宽松模式下返回 approved 即已生效。提交后**不要立即检索**刚提交的内容——宽松模式向量在后台异步生成，刚提交瞬间可能检索不到；严格模式需等 admin 审批通过后才会写入图谱。

## 核心工作流：先检索后提交

发布需求前，**先查重、再提交**：
1. 用 `search_similar_requirements(query=..., system_id=.../project_id=...)` 检索相似/冲突需求；
2. 若命中已有需求：不要重复发布，改用 `update_requirement_relations(from_id=新需求, to_id=命中需求, relation_type="supersedes"/"relates_to")` 建立关联边（如确为替代/关联场景），或在命中需求上用 `update_node` 增补；
3. 若未命中：再调用 `publish_requirement(...)` 发布新需求。

## 可用工具摘要

### 写入类
- **publish_requirement** — 发布需求节点（system_id 必填，requirement 嵌套结构）
- **update_requirement_relations** — 更新需求间关系（批量添加关系边）
- **update_node** — 修正已审批节点（PM/Dev 共享）

### 检索类
- **search_similar_requirements** — 检索相似需求（向量+全文融合）
- **analyze_impact_scope** — 分析变更影响范围（需求→代码→依赖→方案→意图）
- **get_requirement_context** — 查询需求上下文（关联节点+关系链）
- **check_requirement_conflicts** — 排查需求冲突（向量相似度检测）

### 查询类
- **get_project_profile** — 查询项目画像（技术栈/架构/约定）
- **get_project_info** — 枚举/查询项目（list/get）
- **get_role_skills** — 获取角色 Skills 文档

> **详细参数表和示例见 `pm/REFERENCE.md`**

## 常见陷阱

1. **需求主键由服务端分配**：不必（也不应）自生成需求编号。提交后节点会带 `requirement_key`（如 `HIS-0001`，按 system 域可读序号）返回。需求间的重复/矛盾判定基于内容语义相似度（L3，≥ 0.85），不再依赖任何业务编号。

2. **supersedes/relates_to 中的 ID 必须已存在**：引用的 requirement_id 必须是知识图谱中已审批通过的节点。引用不存在的 ID 会导致审批失败。

3. **提交后不可修改**：批次一旦提交，内容不可修改。如需修改，只能等 admin 拒绝后重新提交，或在 admin 审批通过后发布新版本（用 supersedes 关系）。

4. **properties 字段缺失会被 schema 校验拒绝**：publish_requirement 在工具层即校验 properties 必填字段，缺失会直接返回错误（不会进入审批队列）。

5. **提交后如何跟进（不要轮询、不要立即检索）**：
   - **严格模式**：返回 `status="pending_review"` 表示批次在队列等待 admin 审批。**拿到 batch_id 后只需告知用户「待 admin 审批」，无需轮询**；审批通过前需求未写入图谱，其他 Agent 检索不到。
   - **宽松模式**：返回 `status="approved"` + `decision="auto_approved"` 即已直接入库生效、可被检索；若返回 `decision="needs_human_review"` 表示有冲突，批次停在队列等待 admin 人工决策（你这边无需再操作）。
   - **不要提交后立即检索刚提交的内容**：宽松模式向量在后台异步生成，刚提交瞬间可能检索不到；严格模式则要等审批通过后才写入。

6. **content 应足够详细**：content 会用于向量生成（f"{title}\n{content}"），内容越详细，检索准确性越高。避免只写一句话描述。

## 参考文件

详细工具参数表、嵌套结构定义和完整工作流示例见独立参考文件：

- **PM**: 参考 `pm/REFERENCE.md`

按需加载方式：
1. 使用 `get_role_skills(role="pm")` 获取主文件
2. 如需详细工具参数或示例，加载 `pm/REFERENCE.md`
3. REFERENCE.md 仅在需要时加载，节省 context token
