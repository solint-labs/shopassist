# ── Build stage: resolve and install dependencies into a virtualenv ───────────
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"
WORKDIR /src
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .

# ── Runtime stage: small image, non-root user ─────────────────────────────────
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/venv/bin:$PATH"
RUN useradd --create-home --uid 10001 shopassist && mkdir -p /models /app/data && chown shopassist /models /app/data
COPY --from=build /venv /venv
WORKDIR /app
COPY app ./app
COPY config ./config
COPY scripts ./scripts
COPY knowledge ./knowledge
USER shopassist
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"
# --proxy-headers: trust X-Forwarded-* from your load balancer / ingress
CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--workers", "2"]
