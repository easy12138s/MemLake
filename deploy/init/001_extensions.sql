-- Mem Lake 数据库初始化脚本
-- 在 PostgreSQL 容器首次启动时自动执行

-- 安装扩展
CREATE EXTENSION IF NOT EXISTS age;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS zhparser;

-- 配置中文全文检索
CREATE TEXT SEARCH CONFIGURATION chinese (PARSER = zhparser);
ALTER TEXT SEARCH CONFIGURATION chinese ADD MAPPING FOR n,v,a,i,e,l WITH simple;

-- 创建 AGE 图
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
SELECT create_graph('mem_lake_graph');

-- 行级安全（RLS）基础设置
-- 各表的 RLS 策略在应用层通过迁移脚本创建
