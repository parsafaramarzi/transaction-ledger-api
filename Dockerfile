# syntax=docker/dockerfile:1

# ---- build stage: turn the project and its dependencies into wheels ----------
FROM python:3.13-slim AS build
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

# ---- runtime stage: small image, no build tooling, non-root user -------------
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LEDGER_DB_PATH=/data/ledger.db

RUN useradd --system --create-home --uid 10001 ledger \
    && mkdir /data \
    && chown ledger:ledger /data

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels ledger \
    && rm -rf /wheels

USER ledger
WORKDIR /home/ledger

# The SQLite file lives on a volume so data survives container restarts.
VOLUME /data
EXPOSE 8000

CMD ["uvicorn", "ledger.api:app", "--host", "0.0.0.0", "--port", "8000"]
