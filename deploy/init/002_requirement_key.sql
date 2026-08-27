-- 需求主键列 + 计数器表（v1.x）
-- 由服务端按 system 域分配可读序号（如 HIS-0001），替代原先调用方自生成的 requirement_id 属性。

-- 1. knowledge_node 增加 requirement_key 列
ALTER TABLE knowledge_node ADD COLUMN IF NOT EXISTS requirement_key VARCHAR(64);

-- 按 (system_id, requirement_key) 唯一（仅对非空 requirement_key 生效；其它类型该列为 NULL 不参与）
DROP INDEX IF EXISTS uq_node_system_requirement_key;
CREATE UNIQUE INDEX uq_node_system_requirement_key
    ON knowledge_node (system_id, requirement_key)
    WHERE requirement_key IS NOT NULL;

-- 2. system 增加 code 前缀编码列（用于需求主键前缀）
ALTER TABLE system ADD COLUMN IF NOT EXISTS code VARCHAR(32) UNIQUE;

-- 3. 需求序号计数器表
CREATE TABLE IF NOT EXISTS requirement_counter (
    system_id UUID PRIMARY KEY,
    last_value INTEGER NOT NULL DEFAULT 0
);
