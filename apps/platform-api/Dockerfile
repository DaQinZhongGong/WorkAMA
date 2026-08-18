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
# 性能优化（candidate, pending GA）：Granian(Rust ASGI server) 替换 uvicorn。
# 跨容器压测结论：worst-case 尾尖 427ms→55ms、steady p99 33→32ms；worker/thread 经环境变量配置。
# 使用 shell form 以支持 ${GRANIAN_WORKERS:-4} / ${GRANIAN_RUNTIME_THREADS:-8} 环境变量展开。
CMD granian --interface asgi --host 0.0.0.0 --port 8000 \
    --workers ${GRANIAN_WORKERS:-4} --runtime-threads ${GRANIAN_RUNTIME_THREADS:-8} \
    workama_platform.main:app
