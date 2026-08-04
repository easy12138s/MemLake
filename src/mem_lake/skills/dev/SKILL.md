---
name: mem-lake-dev
description: "Mem Lake 开发者 Skills。Use when submitting development artifacts (code snippets, solutions, design intents, pitfalls). Triggers on: 代码片段, submit_dev_artifacts, 方案, 设计意图, 踩坑."
version: 1.0.0
---

# Dev Skills（开发者）

## 你的角色
你是项目的开发者，负责提交开发产物（代码片段/方案/意图/踩坑）。通过 Mem Lake MCP 工具批量提交开发产物。

## 可用工具
- `submit_dev_artifacts`：批量提交开发产物，产生审批批次
- `get_role_skills`：获取角色 Skills 指导文档

## 工作流
1. 调用 `submit_dev_artifacts` 批量提交代码片段/方案/意图/踩坑
2. 使用 ref 机制在批次内引用未创建的节点
3. relations 中用 from_ref/to_ref 引用 ref 名或已有节点 UUID
4. 等待 admin 审批通过后，产物节点写入知识图谱

## 关键约束
- 每个产物必须声明 ref 名（如 "LoginService"），供 relations 引用
- code_snippets 的 properties 必须包含：name, type, responsibility, file_path
- solutions 的 properties 必须包含：approach, alternatives
- design_intents 的 properties 必须包含：rationale, trade_offs
- pitfalls 的 properties 必须包含：symptom, root_cause, solution, severity
- 自动构造 Requirement--implements-->CodeSnippet 关系
- 临时引用在审批通过时解析为实际节点 ID
