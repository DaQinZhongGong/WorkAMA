FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY apps/agent-server/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/agent-server/src ./src
COPY packages/py-common/workama_observability ./src/workama_observability
COPY apps/agent-server/tests ./tests
ENV PYTHONPATH=/app/src

EXPOSE 8001
CMD ["uvicorn", "workama_agent.main:app", "--host", "0.0.0.0", "--port", "8001"]
