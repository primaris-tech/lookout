# syntax=docker/dockerfile:1

FROM python:3.12-slim

# Non-root user. UID 1000 by default so bind-mounted ./data permissions line up
# with most host users without needing --user overrides.
RUN useradd --create-home --shell /bin/bash --uid 1000 lookout

WORKDIR /app

COPY pyproject.toml /app/
COPY src/ /app/src/

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir . \
    && mkdir -p /app/data \
    && chown -R lookout:lookout /app

USER lookout

# Mount points: config.yml (read-only) and data/ (writable for SQLite state).
VOLUME ["/app/data"]

ENTRYPOINT ["lookout"]
CMD ["--config", "/app/config.yml", "--db", "/app/data/lookout.db"]
