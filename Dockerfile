# syntax=docker/dockerfile:1
FROM python:3.13-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# --- dependency layer -------------------------------------------------
# Install only the runtime dependencies declared in `[project.dependencies]`
# (never the `dev` extra) before any application code is copied in, so an
# app-code-only change never invalidates this layer and never re-triggers a
# PyPI download.
COPY pyproject.toml ./
RUN python -c "import tomllib; deps = tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']; open('/tmp/requirements.txt', 'w').write('\n'.join(deps))" \
    && pip install --no-cache-dir -r /tmp/requirements.txt

# --- application layer --------------------------------------------------
# Copy the whole package tree, not just *.py: tributary/sql/schema.sql and
# tributary/web/templates/*.html are loaded at runtime via
# `Path(__file__).parent / ...`, so they must physically exist next to the
# code that reads them. Installing with `-e` (editable) keeps that
# `__file__` pointing at this copied source tree instead of a wheel that
# (verified empirically) silently drops non-.py package data -- the exact
# "builds fine, 500s on first request" trap this Dockerfile must avoid.
COPY tributary ./tributary
RUN pip install --no-cache-dir --no-deps -e .

EXPOSE 8000

# Railway (and any PaaS) injects $PORT at runtime; a hardcoded port here
# would silently fail the platform's health check.
CMD ["sh", "-c", "uvicorn tributary.web.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
