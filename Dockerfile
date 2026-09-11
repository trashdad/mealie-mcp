FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 MCP_DATA_DIR=/data
WORKDIR /app

COPY pyproject.toml ./
COPY mealie_mcp ./mealie_mcp
RUN pip install --no-cache-dir . && mkdir -p /data

VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=8).status==200 else 1)"

CMD ["mealie-mcp"]
