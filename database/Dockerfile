# Migration sidecar for the solbay ServiceBay template.
# Runs `alembic upgrade head` against /var/lib/solilos/solilos.db on every
# pod start. Idempotent.
#
# Image: ghcr.io/mdopp/solilos-schema-init

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SOLILOS_DB_URL=sqlite:////var/lib/solilos/solilos.db

WORKDIR /opt/solilos-schema

# Install runtime deps. No application package — we only need Alembic +
# SQLAlchemy to drive op.execute() against SQLite. The migration files
# themselves are baked in below.
COPY pyproject.toml ./
RUN pip install --no-cache-dir alembic>=1.13 sqlalchemy>=2.0

COPY alembic.ini ./
COPY migrations ./migrations

# Default entrypoint runs the upgrade. The pod manifest may override
# with `args: ["downgrade", "base"]` etc.; default is idempotent upgrade.
ENTRYPOINT ["alembic"]
CMD ["upgrade", "head"]
