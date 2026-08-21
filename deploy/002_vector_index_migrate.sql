-- 向量索引迁移：cosine ops -> inner product (vector_ip_ops) + HNSW 参数调优
--
-- 背景：
--  1. 自建表（新库）的 HNSW 索引由 SQLAlchemy create_all 按 models.py 的 __table_args__ 生成
--     （vector_ip_ops, m=32, ef_construction=400），无需本脚本。
--  2. 本脚本仅用于【已有存量库】——此前用过 vector_cosine_ops + m=16/ef_64 建过
--     idx_node_vector 的库——需重建索引，使索引 opclass 与搜索层 search/vector.py 的
--     inner_product 调用一致。
--
-- 前提：content_vector 均来自归一化的 embedding 服务（/embed 固定 normalize_embeddings=True），
--        内积 <#> 与余弦等价。若存在手工/历史非归一化向量，请先归一化，否则排序语义会偏差。
--
-- 执行方式（手动，勿放入 init/ 自动执行目录以避免表未创建时报错）：
--  - psql -U memlake -d memlake -f deploy/002_vector_index_migrate.sql
--  - 或在业务低峰用 CREATE INDEX CONCURRENTLY 在线重建，避免阻塞写入。
--
-- 注：HNSW 索引不支持直接 ALTER；需 DROP 后重建。

DROP INDEX IF EXISTS idx_node_vector;

CREATE INDEX idx_node_vector
  ON knowledge_node
  USING hnsw (content_vector vector_ip_ops)
  WITH (m = 32, ef_construction = 400);

-- 查询期召回/延迟旋钮（可选，按需放开；值越大召回越高、延迟越高）
-- SET hnsw.ef_search = 100;