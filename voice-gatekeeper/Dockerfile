FROM docker.io/python:3.12-slim

WORKDIR /app

# System deps kept minimal. Wyoming is pure Python; httpx wheel is pre-built.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md /app/
COPY src /app/src

# EXTRAS selects optional dependency groups from pyproject. The default
# image installs the base bridge only; the `solaris-gatekeeper-ml` image is
# built with EXTRAS="[speaker-id]" (SpeechBrain + torch) so Phase-2
# speaker-ID can load the ECAPA model. Kept one Dockerfile so the two
# images never drift.
ARG EXTRAS=""
RUN pip install --no-cache-dir "/app${EXTRAS}"

ENV PYTHONUNBUFFERED=1 \
    SOLARIS_COMPONENT=gatekeeper

# Wyoming pipeline endpoint that satellites (HA Voice PE, wyoming-satellite
# CLI, etc.) connect to. The Wyoming-internal whisper/piper services are
# reached via 127.0.0.1 inside the same pod.
EXPOSE 10700

ENTRYPOINT ["python", "-m", "gatekeeper"]
