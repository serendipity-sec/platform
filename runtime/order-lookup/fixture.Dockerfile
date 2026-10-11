FROM python:3.13.15-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b
WORKDIR /app
RUN pip install --no-cache-dir uv==0.9.30
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra baseline
COPY src/flowdelta /app/src/flowdelta
ENV PYTHONPATH=/app/src PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 1000:1000
CMD ["/app/.venv/bin/uvicorn", "flowdelta.baseline.fixture:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
