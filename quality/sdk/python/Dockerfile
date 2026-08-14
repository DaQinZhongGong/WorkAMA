FROM python:3.12-slim

RUN pip install --no-cache-dir openai==2.45.0
COPY quality/sdk/python/smoke.py /test/smoke.py

CMD ["python", "/test/smoke.py"]
