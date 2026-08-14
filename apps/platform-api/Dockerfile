FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*

COPY apps/platform-api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/platform-api/src ./src
COPY packages/py-common/workama_observability ./src/workama_observability
COPY apps/platform-api/tests ./tests
COPY apps/platform-api/pyproject.toml ./pyproject.toml
COPY apps/platform-api/workama_version.py ./workama_version.py
ENV PYTHONPATH=/app/src:/app

EXPOSE 8000
# 性能优化：多 worker 以利用多核，worker 数通过 UVICORN_WORKERS 环境变量配置（默认 2）。
# 使用 shell form 以支持 ${UVICORN_WORKERS:-2} 环境变量展开。
CMD uvicorn workama_platform.main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-2}
