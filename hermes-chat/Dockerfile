FROM docker.io/python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY src /app/src

RUN pip install --no-cache-dir /app

ENV PYTHONUNBUFFERED=1 \
    SOLILOS_COMPONENT=chat

# Loopback-only chat proxy; NPM (hostNetwork) proxies the public
# chat.<publicDomain> down to this port behind Authelia forward-auth.
EXPOSE 8787

ENTRYPOINT ["python", "-m", "solilos_chat"]
