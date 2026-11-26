---
name: mem-lake-pm
description: "Mem Lake product manager skills for publishing and managing requirement nodes in the team knowledge graph. Use when creating new requirements, updating requirement relationships (supersede/relate), or managing requirement versions. Triggers on: 需求发布, publish_requirement, 需求关系, update_requirement_relations, 需求替代, 需求关联, 悬浮需求, bind_requirement_to_project, get_node_info, requirement, PRD."
version: 1.20.0
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
- **冲突检测**：四层冲突检测（L0/L1/L2/L3）保证数据质量
- **审计可追溯**：所有写操作记录审计日志，可追溯数据来源

### 使用建议
- 检索时注意向量可能有短暂延迟（刚提交的内容可能检索不到）
- **验证写入用 `get_requirement_context(requirement_id, depth=1)`**——图遍历不读向量索引，审批通过（或宽松模式入库）后即时可见
- 如需确保数据最新，可先用 get_project_info 检查项目状态
- 发现数据问题可用 update_node 修正（需重新审批；`project_id` 留空可修悬浮需求）

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
3. 若未命中：单条用 `publish_requirement(...)`；批量录入先用
   `validate_requirements(...)` 做 dry-run（字段/引用/冲突预览），再一次
   `publish_requirements(requirements=[...], operation_id=...)` 提交——同一
   submission_id 下每条需求仍独立审批，逐条返回 batch_id/decision/conflict_hint。

## 可用工具摘要

### 写入类
- **publish_requirement** — 发布单条需求（system_id 必填，requirement 嵌套结构）
- **publish_requirements** — 批量发布：一个 submission_id 下每条需求一个批次，返回逐条 client_ref/batch_id/decision/conflict_hint
- **validate_requirements** — 发布前 dry-run：字段/引用/批内重复预检（exact/近重复）+ 冲突预览，不写库
- **update_requirement_relations** — 更新需求间关系（批量；端点支持 UUID 或 requirement_key/external_id）
- **update_node** — 修正已审批节点（PM/Dev 共享；`project_id` 留空 = 更新悬浮需求）
- **update_requirements** — 批量追加/移除标签、增量改属性（显式 node_ids，一个批次）
- **bind_requirement_to_project** — 悬浮需求 ↔ 项目归属绑定/解绑（走审批流；绑定是归属治理，不触发冲突检测）

### 检索类
- **search_similar_requirements** — 检索相似需求（向量+全文融合）
- **analyze_impact_scope** — 分析变更影响范围（需求→代码→方案→设计意图→踩坑）
- **list_requirements** — 清单式枚举需求（分页+过滤；requirement_key 直查、fields 裁剪）
- **get_requirement_context** — 查询需求上下文（关联节点+关系链）
- **check_requirement_conflicts** — 排查需求冲突（向量相似度检测）

### 查询类
- **get_project_info** — 枚举/查询项目（list/get）
- **get_node_info** — 按 node_id 反查归属（悬浮/已软删/越权均可诊断）
- **get_submission** — 按 submission_id 查看批次状态/审批人/时间线（非 admin 仅见自己）
- **get_skill** — 读取角色技能文档（SKILL/REFERENCE），按需查询不占常驻上下文

> **详细参数表和示例见 `pm/REFERENCE.md`**

## 角色边界与项目建档

- **需求内容归 PM**：需求（`Requirement`）的标题/正文/属性由 PM 维护。Dev 侧只改研发资产节点（代码/方案/意图/决策/踩坑）；命中需求会被拒并提示改用 `bind_requirement_to_project`。
- **归属治理归 PM**：绑定悬浮需求到项目，PM 全权（绑定/解绑/改绑）；Dev 只能把悬浮需求绑定到自己项目，不能解绑、不能改绑。
- **项目画像 = 项目建档**：项目名称与工作目录只存在于画像节点里。`get_project_info` 显示 `has_profile=false` 说明该项目**尚未建档**（你只会看到一个 UUID，判断不出是哪个项目）——请找 admin 用 `manage_project_profile` 补建（建档顺序：先建画像 → 再 `manage_system add_projects` 挂载 → 最后 `create_access_key` 授权；无画像的项目会被后两处入口拒绝）。
- **悬浮需求可由 PM 直接更新**：省略 `update_node` 的 `project_id` 即表示目标是无项目归属的悬浮需求（可见性按 system 口径判定）。

## 需求内容模板（建议）

```text
背景：为什么要做（业务问题/触发事件）
范围：本期做什么、明确不做什么（out of scope）
业务规则：角色/状态流转/边界条件
验收标准（properties.acceptance_criteria）：可验证的 1/2/3...
依赖：关联需求/项目/外部系统（用 relates_to/supersedes 建边）
待确认：未定项单独标注，后续用 update_requirements 批量清理「待确认」标签
```

`validate_requirements` 会对缺少验收标准、含「待确认」标签、正文出现表名/索引/
幂等键等实现细节给出 warnings（只提示，不阻断）。

## 常见陷阱

1. **需求主键由服务端分配**：不必（也不应）自生成需求编号。提交后节点会带 `requirement_key`（如 `HIS-0001`，按 system 域可读序号）返回。需求间的重复/矛盾判定基于内容语义相似度（L3，≥ 0.85），不再依赖任何业务编号。

2. **supersedes/relates_to 端点必须已存在且对你可见**：可直接传 UUID，也可传 `requirement_key`（如 `SYS-1800`）或 `external_id`；歧义/不存在会明确报错。悬浮需求关系可省略 project_id，按两端自身 system/project 鉴权。

3. **提交后不可修改**：批次一旦提交，内容不可修改。如需修改，只能等 admin 拒绝后重新提交，或在 admin 审批通过后发布新版本（用 supersedes 关系）。

4. **properties 字段缺失会被 schema 校验拒绝**：publish_requirement 在工具层即校验 properties 必填字段，缺失会直接返回错误（不会进入审批队列）。

5. **提交后如何跟进（不要轮询、不要立即检索）**：
   - **严格模式（正常态，默认）**：回执 `mode="strict"` + `status="pending_review"`——批次已成功提交，只需拿到 `batch_id` 并告知用户「已提交，待 admin 审批」，无需轮询或重试；审批通过前需求未写入图谱，其他 Agent 检索不到。
   - **宽松模式**：回执 `mode="lax"` + `status="approved"` + `decision="auto_approved"` 即已直接入库生效、可被检索；若返回 `decision="needs_human_review"` 表示有冲突，批次停在队列等待 admin 人工决策（你这边无需再操作）。
   - **回执 mode 三态**：`strict` / `lax` / `lax_global_off`（Key 标记宽松但全局开关关闭，已按严格处理）——三态均表示提交成功，只是入库时点不同。
   - **不要提交后立即检索刚提交的内容**：宽松模式向量在后台异步生成，刚提交瞬间可能检索不到；严格模式则要等审批通过后才写入。

6. **content 应足够详细**：content 会用于向量生成（f"{title}\n{content}"），内容越详细，检索准确性越高。避免只写一句话描述。

## 参考文件

详细工具参数表、嵌套结构定义和完整工作流示例见独立参考文件：

- **PM**: 参考 `pm/REFERENCE.md`

按需加载方式（MCP Resource，无需下载落盘）：
1. 主文档即本文件：读取资源 `memlake://skills/pm/SKILL`（或调用 `get_skill()`）
2. 详细参数：读取资源 `memlake://skills/pm/REFERENCE`（或 `get_skill(file="REFERENCE")`）
3. 工具参数以 tools/list 的实际 schema 为准；本文件与 REFERENCE 补充用法与工作流

## Skills 文件放置指南

技能文档随 MCP 服务分发（同一 `X-MCP-Key` 鉴权、按需读取），**无需从 GitHub 下载**。
仅当客户端不支持 MCP Resources 时，才用下面的放置方式兜底（GitHub raw 可能滞后于镜像版本）。

### 跨客户端项目级（推荐，首选）
- `.agents/skills/mem-lake-{role}/SKILL.md`（符合 Agent Skills 标准 agentskills.io，主流 Agent 通用）

### Claude Code
- 用户级：`~/.claude/skills/mem-lake-{role}/SKILL.md`；项目级：`.claude/skills/mem-lake-{role}/SKILL.md`

### Cursor
- `.cursor/rules/mem-lake-{role}.mdc`（包装为 .mdc 格式）

### Codex CLI (OpenAI)
- `.agents/skills/mem-lake-{role}/SKILL.md`（同首选）；或追加到 `AGENTS.md`

### Gemini CLI
- `.gemini/rules/mem-lake-{role}.md`

`{role}` 替换为实际角色（admin/pm/dev）；放置后重启 Agent 会话生效；
不确定目录格式请查阅你的 Agent 官方文档。
