# 批量解析导入需求文档 — 设计规格

日期：2026-08-31
状态：已批准（build mode）

## 1. 目标

提供 CLI `memlake-import-requirements`：扫描文件夹内的 HTML 需求文档（每个文件 = 一个需求），用 **markitdown** 解析为 Markdown，抽取 title/content，直接写入 Requirement 节点（带向量、不进审批），并按**原始相对路径**做幂等，支持 `--force` 更新。

## 2. 入口与调用

- 新增 console script `memlake-import-requirements`，复用 `src/mem_lake/cli/` 目录与 `asyncio.run` 模式，容器内运行（连 Postgres/AGE/embedding）。

```bash
docker exec -it deploy-mem-lake-1 memlake-import-requirements /data/reqs \
  --system-code HIS --project <project-id> \
  [--priority P3] [--module 导入] [--force] [--dry-run]
```

- 参数：
  - 位置参数 `<folder>`：要扫描的根目录（递归）。
  - `--system-code`：目标 System.code。
  - `--project`：目标 Project（id 或名称）。
  - `--priority`：默认 `P3`，用于未识别到优先级的需求。
  - `--module`：默认 `导入`，用于未识别到模块的需求。
  - `--force`：`source_doc` 已存在时用 update_node 覆盖更新。
  - `--dry-run`：只解析与打印待导入清单，不连 embedding、不写库。

## 3. 模块划分（均在 `src/mem_lake/cli/`）

- **`import_requirements.py`**：argparse 入口 → 构造 `AsyncSessionLocal` + `GraphStore` + `EmbeddingClient` → 依次驱动 extract→ingest → 打印汇总（成功/跳过/失败/耗时）。有失败则非零退出码。
- **`extractor.py`**：`extract_directory(root) -> list[ParsedRequirement]`。递归扫 `*.html`；每个文件用 `markitdown.MarkItDown().convert(file).text_content` 转 markdown；输出 `ParsedRequirement(title, content, rel_path)`。
- **`ingest.py`**：按 `source_doc` 查重 → `create_node`（新增）/ `update_node`（`--force`）/ skip → 事务提交。

## 4. 数据流与字段映射

```
folder1/folder2/a.html
  → markitdown convert() → markdown
  → ParsedRequirement(
      title    = "folder1/folder2/a.html",   # 完整相对路径(/)连接，非文件名非#标题
      content  = markdown 正文(剥掉 # 标题行后的剩余文本),
      rel_path = "folder1/folder2/a.html",
    )
  → Requirement:
      title            = 完整相对路径
      content          = markdown 正文
      properties       = { priority, module, source_doc=rel_path }
      source           = {"kind":"file","path":rel_path,"importer":"cli"}
      system_id        = 由 --system-code 解析
      project_id       = 由 --project 解析
      generate_vector  = True   # title(含路径)入向量 → 检索可定位源文件
```

- `source_doc` 写入 `properties`（封闭契约 Requirement 可选字段白名单已有 `source_doc`），同时 `source` provenance 存一份溯源。

## 5. 幂等与错误处理

- 查重：在 `project_id + system_id` 范围内匹配 `properties.source_doc == rel_path`。
  - 命中且无 `--force` → **skip**。
  - 命中且 `--force` → **update_node**（`regenerate_vector=True`，保留原 requirement_key；`update_node` 需重建 properties 全集）。
  - 未命中 → **create_node**（服务端分配新 `requirement_key`，如 `HIS-0001`）。
- `--dry-run`：仅解析 + 打印待导入清单，不连 embedding、不写库。
- 单文件失败隔离：捕获每文件异常记入失败清单，其余继续；末尾汇总，有失败则非零退出码。
- `--system-code` / `--project` 未命中 → 启动即 fail-fast 报错退出。

## 6. 依赖

- 新增 `markitdown`（基础包即可；HTML 为内置 `HtmlConverter`，无需 `[all]` extras）。
- CLI 框架用 stdlib `argparse`，不引新框架。
- 容器需 rebuild 以安装 markitdown。

## 7. 测试（`tests/unit/`）

1. **extractor**：临时 HTML（`<h1>`/`<title>`/多段/嵌套目录/非 html 文件）→ 断言 title=完整相对路径(`/` 连接)、content 为 markdown 正文、递归扫描正确、非 html 忽略。
2. **ingest**：fake/mock `create_node`/`update_node` → 断言 source_doc 幂等（新增/跳过/--force 更新）、properties 与 source 映射正确、单文件异常隔离不中断其余。
3. **CLI 冒烟**：`--dry-run` 不写库；`--system-code` 未命中 fail-fast。

## 8. 范围外（本次不做）

- 关系边建立。
- 从 HTML 自动抽取 `acceptance_criteria`/`version`/`external_id` 等（`extra_props` 仅预留扩展点）。
- docx/xlsx 等其它格式（markitdown 已支持，后续按需加 extras）。
