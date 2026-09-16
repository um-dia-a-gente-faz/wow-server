# ─── WoW AI Agent Container ────────────────────────────────
# Pure Python 3.12+, zero pip dependencies.
# Single process per agent; scale horizontally with compose.

FROM python:3.12-slim

WORKDIR /app

# Copy only the agent package
COPY agent/ ./agent/

# Python flags
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Default entrypoint: run the agent loop
ENTRYPOINT ["python3", "-m", "agent"]