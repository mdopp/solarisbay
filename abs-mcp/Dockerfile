FROM docker.io/python:3.12-slim

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md /app/
COPY src /app/src

RUN pip install --no-cache-dir /app

ENV PYTHONUNBUFFERED=1 \
    SOLILOS_COMPONENT=abs-mcp

# Streamable-HTTP MCP endpoint Hermes registers (loopback by default).
EXPOSE 10770

ENTRYPOINT ["python", "-m", "abs_mcp"]
