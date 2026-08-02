# Mem Lake 应用镜像
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml /app/
COPY src/ /app/src/

RUN pip install --no-cache-dir /app

EXPOSE 8000
CMD ["python", "-m", "mem_lake.main"]
