---
name: mem-lake-admin
description: "Mem Lake administrator skills for approval workflow management, access key governance, and project profile maintenance. Use when managing pending approval batches, auto-processing conflicts, issuing or revoking access keys, or maintaining project profiles. Triggers on: 审批, 待审批, access key, 密钥, 项目画像, review_auto_process, 自动审批, review_pending, review_approve, review_reject, create_access_key, revoke_access_key, list_access_keys, update_access_key_scope, rotate_access_key, set_access_key_mode, manage_project_profile."
version: 1.5.0
---

# Admin Skills（管理员）

> 本文件参数表以工具实际签名（代码）为准；如与运行时 MCP 客户端展示不符，以代码为准。

## 数据信任说明

### 数据所有者
- **知识图谱数据**：由 admin 角色通过审批工作流管理
- **向量索引**：由系统自动生成，admin 可触发全量重建（reindex_project_vectors）
- **Access Key**：由 admin 签发和管理
- **项目画像**：由 admin 直接创建和维护（不走审批）

### 数据新鲜度
- **写入生效**：审批通过后节点立即生效，向量异步生成（可能有短暂延迟）
- **宽松模式**：无冲突时直接入库，向量后台异步生成
- **向量重建**：admin 可通过 reindex_project_vectors 触发全量重建
- **启动对账**：系统启动时自动将残留 pending/running 任务置为 failed

### 数据认证
- **审批即认证**：admin 审批通过的数据视为已认证
- **冲突检测**：三层冲突检测（L0/L1/L2/L3）保证数据质量
- **审计可追溯**：所有写操作记录审计日志，可追溯数据来源
- **幂等保证**：operation_id 唯一约束，防止重复提交

### 数据质量监控
- **图统计**：get_graph_stats 查看节点/边数量和质量基线
- **质量报告**：get_graph_quality_report 查看孤立节点、缺失向量等问题
- **审计日志**：query_audit_log 追踪所有数据变更历史

### 使用建议
- 定期使用 get_graph_quality_report 检查数据质量
- 发现向量不一致时使用 reindex_project_vectors 重建
- 使用 query_audit_log 追溯数据变更历史

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

你是当前项目的管理员。MemLake 是团队共享的知识记忆工具，你负责运维它（审批批次、签发/吊销 Access Key、维护项目画像与 system 域），让 PM/Dev 能稳定地检索与沉淀知识。你的核心职责：

1. **审批治理**：审查 PM/Dev 提交的批次，决定通过或拒绝
2. **自动审批**：对无冲突批次自动通过，有冲突批次向人类 admin 描述并等待决策
3. **密钥管理**：为团队成员签发或吊销 Access Key（绑定角色与项目范围），并维护审核模式
4. **画像维护**：直接写入项目画像节点（不走审批流）

关键原则：**你是 admin 的助手，不是决策者**。无冲突时可以自动通过（确定性判断），有冲突时必须向人类 admin 描述冲突详情并等待明确指令。**审批默认只调 `review_auto_process`；仅当其返回 `needs_human_review` 时，才用 `review_approve`/`review_reject` 落实人类 admin 的明确决策，不要先调手动审批跳过冲突检测。**

## 可用工具摘要

### 审批类
- **review_pending_list** — 查询待审批批次队列
- **review_auto_process** — 自动处理审批批次（核心工具）
- **review_batch_detail** — 查看批次完整内容
- **review_approve** — 审批通过批次
- **review_reject** — 审批退回批次

### 密钥管理类
- **create_access_key** — 创建 Access Key
- **revoke_access_key** — 吊销 Access Key
- **list_access_keys** — 查看 Access Key 列表
- **update_access_key_scope** — 改项目范围
- **rotate_access_key** — 轮换密钥
- **set_access_key_mode** — 改审核模式

### 项目管理类
- **manage_system** — 建立并签发 system 域
- **manage_project_profile** — 直接写入项目画像
- **get_project_profile** — 查询项目画像
- **get_project_info** — 枚举/查询项目

### 数据质量类
- **reindex_project_vectors** — 异步重建项目向量
- **get_reindex_status** — 查询重嵌任务进度
- **list_knowledge** — 分页列出项目知识节点
- **query_audit_log** — 查询审计日志

### 共享类
- **get_role_skills** — 获取角色 Skills 文档
- **get_requirement_context** — 查询需求上下文

> **详细参数表和示例见 `admin/REFERENCE.md`**

## 关键要点

1. **默认用 review_auto_process**：不要跳过它直接 review_approve，否则会跳过冲突检测。

2. **Access Key 明文仅创建时返回一次**：丢失后只能吊销重建。

3. **review_reject 必须填 review_comment**：拒绝原因会写入审计日志。

4. **已审批批次不能再次审批**：对已审批批次调用会返回错误。

5. **manage_project_profile 是直接写入**：不走审批流，状态直接 approved。

6. **宽松模式生效需两者同时为真**：Key 标记 lax_mode=true 且全局开关 LAX_MODE_ENABLED=true。

## 冲突检测简述

| 层级 | 检测内容 | 不冲突条件 |
|------|---------|-----------|
| L0 | 关键标识字段精确匹配 | 相同 → 冲突（Requirement 无此层）|
| L1 | 项目 + 节点类型 | 不同项目或类型 → 通过 |
| L2 | 关键属性 | 不同 → 排除（Requirement 无此层）|
| L3 | 内容语义相似度 | < 0.85 → 通过 |

## 常见工作流

### 自动审批

```
1. review_pending_list(limit=50)
2. review_auto_process(batch_id=...)
   ├─ auto_approved → 告知人类，无需操作
   └─ needs_human_review → 向人类描述冲突，等待决策
       ├─ 人类"通过" → review_approve(...)
       └─ 人类"拒绝" → review_reject(...)
```

### 密钥管理

```
create_access_key(role="pm", project_scope=[...])
revoke_access_key(key_id="...")
rotate_access_key(key_id="...")
set_access_key_mode(lax_mode=true, key_ids=["..."])
```

## 参考文件

详细工具参数表、冲突检测机制和完整工作流示例见独立参考文件：

- **Admin**: 参考 `admin/REFERENCE.md`

按需加载方式：
1. 使用 `get_role_skills(role="admin")` 获取主文件
2. 如需详细工具参数或示例，加载 `admin/REFERENCE.md`
3. REFERENCE.md 仅在需要时加载，节省 context token
