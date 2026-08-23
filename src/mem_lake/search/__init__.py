"""三引擎检索模块：向量 + 全文 + 图遍历，RRF 融合排序。

对齐 PDD 3.3/5.4。三引擎通过 asyncio.gather 并行执行，统一 FilterSpec 编译过滤条件。
向量与全文结果通过 RRF 算法融合排序，图遍历作为独立检索路径不参与融合。
"""

from mem_lake.search.filters import FilterSpec, compile_sqlalchemy
from mem_lake.search.fulltext import FullTextSearcher
from mem_lake.search.fusion import SearchResult, hybrid_search, rrf_fuse
from mem_lake.search.graph import GraphSearcher
from mem_lake.search.vector import VectorSearcher

__all__ = [
    "FilterSpec",
    "compile_sqlalchemy",
    "SearchResult",
    "hybrid_search",
    "rrf_fuse",
    "FullTextSearcher",
    "GraphSearcher",
    "VectorSearcher",
]
