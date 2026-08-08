#!/bin/bash
# MemLake 数据库备份脚本
#
# 使用 pg_dump 自定义格式（-F c）完整备份 PostgreSQL 数据库，
# 含关系表（knowledge_node/access_key/audit_log/approval_*）+
# AGE 图数据（mem_lake_graph schema）+ pgvector 向量 + ag_catalog 元数据。
#
# 经实测验证：pg_dump -F c 完整备份后 pg_restore 恢复，
# Cypher 查询正常，图节点/边数据零丢失。
#
# 用法：
#   ./backup.sh                    # 备份到默认目录 ./backups/
#   ./backup.sh /path/to/dir       # 备份到指定目录
#   RETENTION_DAYS=14 ./backup.sh  # 保留 14 天（默认 30 天）
#
# 定时任务（crontab 每日凌晨 2 点）：
#   0 2 * * * cd /path/to/deploy && ./backup.sh >> logs/backup.log 2>&1
#
# 恢复见 restore.sh。

set -euo pipefail

# ========== 配置 ==========

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="${1:-$SCRIPT_DIR/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
DB_NAME="${DB_NAME:-memlake}"
DB_USER="${DB_USER:-memlake}"

# 容器名：优先用环境变量，否则通过 docker compose 自动发现
if [ -z "${PG_CONTAINER:-}" ]; then
    PG_CONTAINER=$(cd "$SCRIPT_DIR" && docker compose ps -q postgres 2>/dev/null | xargs -r docker inspect --format='{{.Name}}' 2>/dev/null | sed 's|^/||')
    # 回退到默认命名（基于 deploy 目录）
    if [ -z "$PG_CONTAINER" ]; then
        PG_CONTAINER="deploy-postgres-1"
    fi
fi

# ========== 主逻辑 ==========

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="$BACKUP_DIR/memlake_${TIMESTAMP}.dump"
LATEST_LINK="$BACKUP_DIR/latest.dump"

mkdir -p "$BACKUP_DIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始备份..."
echo "  数据库: $DB_NAME"
echo "  容器:   $PG_CONTAINER"
echo "  目标:   $BACKUP_FILE"

# 检查容器运行状态
if ! docker inspect --format='{{.State.Running}}' "$PG_CONTAINER" 2>/dev/null | grep -q true; then
    echo "[ERROR] PostgreSQL 容器 $PG_CONTAINER 未运行"
    exit 1
fi

# 执行备份（自定义格式，支持并行恢复 + 压缩）
docker exec "$PG_CONTAINER" pg_dump \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    -F c \
    --no-owner \
    --no-privileges \
    > "$BACKUP_FILE"

BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "  大小:   $BACKUP_SIZE"

# 更新 latest 软链接（方便快速取最新备份）
ln -sf "memlake_${TIMESTAMP}.dump" "$LATEST_LINK"

# 清理过期备份
DELETED=$(find "$BACKUP_DIR" -name "memlake_*.dump" -mtime +"$RETENTION_DAYS" -print -delete | wc -l)
if [ "$DELETED" -gt 0 ]; then
    echo "  清理:   删除 $DELETED 个超过 ${RETENTION_DAYS} 天的旧备份"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 备份完成: $BACKUP_FILE"
