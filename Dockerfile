# ─── WoW AI Agent Container ────────────────────────────────
# Pure Python 3.12+, zero pip dependencies.
# Single process per agent; scale horizontally with compose.

FROM python:3.12.15-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

WORKDIR /app

# Copy only the agent package
COPY agent/ ./agent/

# Python flags
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Default entrypoint: run the agent loop
ENTRYPOINT ["python3", "-m", "agent"]