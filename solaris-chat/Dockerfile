FROM docker.io/python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY src /app/src

RUN pip install --no-cache-dir /app

# The Solaris release version, injected at build time (the git tag/ref, see
# build-images.yml). Surfaced in the sidebar footer; falls back to the package
# version when unset (local/dev builds) so the badge always shows something.
ARG SOLARIS_VERSION=""

ENV PYTHONUNBUFFERED=1 \
    SOLARIS_COMPONENT=chat \
    SOLARIS_VERSION=${SOLARIS_VERSION}

# Loopback-only chat proxy; NPM (hostNetwork) proxies the public
# chat.<publicDomain> down to this port behind Authelia forward-auth.
EXPOSE 8787

ENTRYPOINT ["python", "-m", "solaris_chat"]
