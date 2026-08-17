# Mem Lake 应用镜像
# 基于 python:3.11-slim，安装项目依赖并启动 FastMCP HTTP 服务
FROM python:3.11-slim

# 替换为国内 Debian 镜像源（加速 apt 下载）
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null \
    || sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list 2>/dev/null \
    || true

# 安装 libpq5（psycopg3 binary 依赖）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# 使用清华 PyPI 镜像加速（fastmcp 等包体积较大）
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app
COPY pyproject.toml /app/
COPY src/ /app/src/

# 安装项目（不含 sentence-transformers 可选依赖，app 容器通过 HTTP 调用 embedding 服务）
RUN pip install --no-cache-dir /app

EXPOSE 8000
# python -m mem_lake.main 触发 mcp.run(transport="http")，监听 0.0.0.0:8000
CMD ["python", "-m", "mem_lake.main"]
