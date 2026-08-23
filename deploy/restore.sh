#!/bin/bash
# MemLake 数据库恢复脚本
#
# 从 pg_dump 自定义格式备份文件恢复 PostgreSQL 数据库。
# 恢复前自动停止 mem-lake 应用（避免连接冲突），恢复后重启。
#
# 警告：恢复会覆盖当前数据库全部数据，不可逆。
#
# 用法：
#   ./restore.sh                              # 恢复最新备份（backups/latest.dump）
#   ./restore.sh backups/memlake_20260808.dmp # 恢复指定备份文件
#   ./restore.sh --no-restart <file>          # 恢复后不重启应用
#   ./restore.sh --yes <file>                 # 跳过确认（用于自动化）
#
# 备份见 backup.sh。

set -euo pipefail

# ========== 配置 ==========

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_NAME="${DB_NAME:-memlake}"
DB_USER="${DB_USER:-memlake}"
COMPOSE_DIR="$SCRIPT_DIR"

# 容器名：优先用环境变量，否则通过 docker compose 自动发现
if [ -z "${PG_CONTAINER:-}" ]; then
    PG_CONTAINER=$(cd "$SCRIPT_DIR" && docker compose ps -q postgres 2>/dev/null | xargs -r docker inspect --format='{{.Name}}' 2>/dev/null | sed 's|^/||')
    if [ -z "$PG_CONTAINER" ]; then
        PG_CONTAINER="deploy-postgres-1"
    fi
fi

# ========== 参数解析 ==========

RESTART_APP=true
SKIP_CONFIRM=false
BACKUP_FILE=""

for arg in "$@"; do
    case "$arg" in
        --no-restart) RESTART_APP=false ;;
        --yes) SKIP_CONFIRM=true ;;
        *) BACKUP_FILE="$arg" ;;
    esac
done

# 默认取最新备份
if [ -z "$BACKUP_FILE" ]; then
    BACKUP_FILE="$SCRIPT_DIR/backups/latest.dump"
fi

# ========== 前置检查 ==========

if [ ! -f "$BACKUP_FILE" ]; then
    echo "[ERROR] 备份文件不存在: $BACKUP_FILE"
    echo "  可用备份:"
    ls -1 "$SCRIPT_DIR/backups/"memlake_*.dump 2>/dev/null || echo "  (无)"
    exit 1
fi

if ! docker inspect --format='{{.State.Running}}' "$PG_CONTAINER" 2>/dev/null | grep -q true; then
    echo "[ERROR] PostgreSQL 容器 $PG_CONTAINER 未运行"
    exit 1
fi

echo "============================================"
echo "  MemLake 数据库恢复"
echo "============================================"
echo "  备份文件: $BACKUP_FILE"
echo "  目标数据库: $DB_NAME"
echo "  覆盖现有数据: 是（不可逆）"
echo "============================================"
echo ""
if [ "$SKIP_CONFIRM" = false ]; then
    read -p "确认恢复？(输入 YES 继续): " confirm
    if [ "$confirm" != "YES" ]; then
        echo "已取消"
        exit 0
    fi
fi

# ========== 执行恢复 ==========

echo ""
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 步骤 1/4: 停止 mem-lake 应用（避免连接占用）..."
cd "$COMPOSE_DIR"
docker compose stop mem-lake
cd "$SCRIPT_DIR"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 步骤 2/4: 删除并重建数据库..."
# WITH (FORCE) 断开残留连接（如外部 SQL 客户端），避免 DROP 因连接占用失败
docker exec "$PG_CONTAINER" psql -U "$DB_USER" -d postgres -c "DROP DATABASE IF EXISTS $DB_NAME WITH (FORCE);"
docker exec "$PG_CONTAINER" psql -U "$DB_USER" -d postgres -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 步骤 3/4: 恢复数据..."
# --exit-on-error：任一对象恢复失败立即中止（配合 set -e），避免半成品库
docker exec -i "$PG_CONTAINER" pg_restore \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    --no-owner \
    --no-privileges \
    --exit-on-error \
    < "$BACKUP_FILE"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 步骤 4/4: 重启 mem-lake 应用..."
if [ "$RESTART_APP" = true ]; then
    cd "$COMPOSE_DIR"
    docker compose start mem-lake
    cd "$SCRIPT_DIR"
    echo "  等待应用就绪（healthcheck 探测，最多 60s）..."
    waited=0
    until [ "$(docker inspect --format='{{.State.Health.Status}}' "$(docker compose -f "$COMPOSE_DIR/docker-compose.yml" ps -q mem-lake 2>/dev/null)" 2>/dev/null)" = "healthy" ]; do
        if [ "$waited" -ge 60 ]; then
            echo "  [WARN] 等待超时（60s），请自行确认容器状态：docker compose ps"
            break
        fi
        sleep 3
        waited=$((waited + 3))
    done
    echo "  应用就绪（约 ${waited}s）"
else
    echo "  --no-restart 已指定，跳过重启（需手动 docker compose start mem-lake）"
fi

echo ""
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 恢复完成"
echo ""
echo "验证建议:"
echo "  docker exec $PG_CONTAINER psql -U $DB_USER -d $DB_NAME -c \"SELECT count(*) FROM knowledge_node;\""
echo "  docker exec $PG_CONTAINER psql -U $DB_USER -d $DB_NAME -c \"SELECT count(*) FROM access_key WHERE status='active';\""
