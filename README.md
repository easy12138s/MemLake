# Mem Lake

## 解决什么问题

团队里每个人都在用 AI 编程工具，但知识是割裂的。

PM 调研了半天需求，AI 帮忙梳理了 PRD、分析了影响范围、检测了冲突——这些上下文只留在 PM 自己的会话里。开发接手时，AI 对新需求一无所知，开发者得重新把需求文档贴进去，或者再跑一遍检索。

更糟的是，老张写的模块只有老张的 AI 知道。老张休假了，小李接手，AI 完全不知道这个模块的设计意图、为什么当时选了方案 A 而不是方案 B、踩过什么坑。小李只能翻代码猜，或者打电话。

传统知识库解决不了这个问题。Confluence 上贴的文档是一篇篇独立的，需求是需求、方案是方案、踩坑是踩坑，没有关联。AI 不知道这篇文档和那篇文档是什么关系，也不知道当前需求应该在哪些文档里找上下文。

我想做的是：**让团队里所有 AI 共享一份知识记忆**。不需要专门找一个人维护知识库，也不需要单独部署一个 Agent 来做这件事。团队里每个人本来就在用 AI 干活——PM 的 AI 在梳理需求，开发者的 AI 在写代码——这些 AI 产出的东西自动沉淀到共享图谱里，每个人既是贡献者也是受益者。分布式地建，集中式地用。

## 部署与使用

```bash
cp .env.example .env
docker compose -f deploy/docker-compose.yml up -d --build
```

三个容器：PostgreSQL（pgvector + Apache AGE + zhparser）、Embedding 服务（Qwen3-Embedding-0.6B）、Mem Lake 应用。MCP 网关在 `http://localhost:8000/mcp`。

MCP 客户端配置：

```json
{
  "mcpServers": {
    "mem-lake": {
      "url": "http://localhost:8000/mcp",
      "headers": { "X-MCP-Key": "ak_xxx.xxx" }
    }
  }
}
```

接入后 AI 自动获得 29 个工具，按角色分配权限：pm 负责需求发布与关系治理，dev 负责代码与方案提交、知识检索，admin 负责审批、密钥与项目画像。写操作走审批流，读操作直接返回。

第一次用需要 admin 创建 Access Key 分配给团队成员。之后所有 Agent 交互自动走 Mem Lake，不需要额外操作。

## 详细文档

部署、配置、备份恢复与日常运维操作见 [DEPLOYMENT.md](DEPLOYMENT.md)。