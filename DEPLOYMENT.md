# 部署与运维

## 环境要求

- Linux（Ubuntu 22.04+ / CentOS 8+），Docker 24+ 及 Compose v2+
- 2GB+ 内存，5GB+ 磁盘

## 部署

### 1. 配置

```bash
cp .env.example .env
```

生产环境需关注的配置项：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `DATABASE_POOL_SIZE` | `10` | 连接池大小，按并发量调整 |
| `MCP_RATE_LIMIT_QPS` | `100` | 每 Access Key 限流 |
| `EMBEDDING_DEVICE` | `cpu` | 有 GPU 改 `cuda` |
| `CONFLICT_SIMILARITY_THRESHOLD` | `0.85` | 冲突检测相似度阈值 |

`DATABASE_URL` 在 Compose 环境下由 `docker-compose.yml` 覆盖，无需手动改。

### 2. 构建启动

```bash
cd deploy
docker compose up -d --build
```

首次构建需编译 AGE/pgvector/zhparser 扩展 + 下载 Embedding 模型，约 15-20 分钟。

### 2.1 复用本地模型（可选，跳过下载）

若宿主机已下载过 bge 模型（默认位于仓库 `models/models/AI-ModelScope--bge-large-zh-v1.5/snapshots/master`），可避免重复下载：叠加 `docker-compose.local.yml` 将本地模型挂载进容器，并通过 `DOWNLOAD_MODEL=false` 跳过构建期下载。

```bash
cd deploy
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
```

> 注意：该文件不会被 `docker compose up` 自动加载，必须显式 `-f` 指定。宿主机不存在该模型目录时**不要**使用，否则 embedding 容器会因加载不到模型而启动失败。普通首次部署直接 `docker compose up -d --build` 即可。

### 3. 验证

```bash
docker compose ps                                                    # 三容器均为 healthy/running
docker compose logs mem-lake | grep "初始化完成"                      # DB 初始化成功
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2026-07-28","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

### 4. 创建首个 Admin Access Key

```bash
docker exec -it deploy-mem-lake-1 memlake-bootstrap-admin
```

明文 Key 仅显示一次，丢失只能重新创建。admin 接入后通过 `manage_access_key` 工具为 PM 和 Dev 创建 Access Key。

### 5. 配置 MCP 客户端

```json
{
  "mcpServers": {
    "mem-lake": {
      "url": "http://<服务器IP>:8000/mcp",
      "headers": { "X-MCP-Key": "ak_xxx.xxx" }
    }
  }
}
```

## 容器架构

| 容器 | 端口 | 职责 |
|------|------|------|
| postgres | 5432 | PostgreSQL 17 + AGE + pgvector + zhparser，数据持久化在 `pg_data` 卷 |
| embedding | 8001 | bge-large-zh-v1.5 向量化服务，mem-lake 通过 HTTP 调用 |
| mem-lake | 8000 | MCP 网关，18 个工具 + RBAC + 限流 |

启动顺序：postgres healthy → embedding healthy → mem-lake。生产环境仅放行 8000 端口。

## 数据备份与恢复

### 手动备份

```bash
cd deploy
./backup.sh                       # 备份到 backups/
./backup.sh /path/to/dir          # 指定目录
RETENTION_DAYS=14 ./backup.sh     # 保留 14 天（默认 30）
```

`pg_dump -F c` 自定义格式，覆盖关系表、AGE 图数据、pgvector 向量、ag_catalog 元数据。

### 恢复

```bash
cd deploy
./restore.sh                              # 恢复最新备份
./restore.sh backups/memlake_20260808.dump # 恢复指定文件
```

自动执行：停止 mem-lake → 重建数据库 → 恢复数据 → 重启。需输入 `YES` 确认。

### 定时备份

```bash
sudo cp deploy/memlake-backup.cron /etc/cron.d/memlake-backup
sudo chmod 644 /etc/cron.d/memlake-backup
```

每日 2:00 执行，保留 30 天，日志输出到 `deploy/backups/backup.log`。

## 运维操作

### 日志

```bash
docker compose logs -f mem-lake          # 实时应用日志
docker compose logs --tail 100 mem-lake  # 最近 100 行
```

### 重启

```bash
docker compose restart mem-lake   # 单容器
docker compose restart            # 全部
```

### 更新版本

```bash
git pull
docker compose up -d --build
```

Schema 变更由应用启动时 `init_knowledge_schema()` 幂等处理，无需手动迁移。

### 数据库状态查询

```bash
# 节点数
docker exec deploy-postgres-1 psql -U memlake -d memlake -c \
  "SELECT status, count(*) FROM knowledge_node GROUP BY status;"

# 待审批批次
docker exec deploy-postgres-1 psql -U memlake -d memlake -c \
  "SELECT count(*) FROM approval_batch WHERE status='pending_review';"

# Access Key
docker exec deploy-postgres-1 psql -U memlake -d memlake -c \
  "SELECT role, status, count(*) FROM access_key GROUP BY role, status;"
```

## 故障排查

### 容器启动失败

```bash
docker compose ps
docker compose logs mem-lake
docker compose logs postgres
docker compose logs embedding
```

- postgres 首次构建慢：扩展编译需 10-15 分钟
- embedding 健康检查失败：检查 `models/bge-large-zh-v1.5/` 是否存在
- mem-lake 连接数据库失败：检查 postgres healthcheck 状态

### 认证失败

- 请求头包含 `X-MCP-Key: ak_xxx.xxx`
- Key 状态为 active：`docker exec deploy-postgres-1 psql -U memlake -d memlake -c "SELECT key_id, role, status FROM access_key;"`
- 明文丢失只能重新创建

### 连接池耗尽

日志出现 `QueuePool limit reached`，调大 `.env` 中 `DATABASE_POOL_SIZE` 和 `DATABASE_MAX_OVERFLOW`，重启 mem-lake。

### 数据恢复

```bash
cd deploy
./restore.sh --yes backups/latest.dump
```
