FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY apps/sandbox-fleet/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/sandbox-fleet/src ./src
COPY apps/sandbox-fleet/tests ./tests
COPY apps/sandbox-fleet/pytest.ini ./pytest.ini
ENV PYTHONPATH=/app/src
EXPOSE 8002
CMD ["uvicorn", "workama_sandbox.main:app", "--host", "0.0.0.0", "--port", "8002"]
