# Build context is tools/ (docker-compose.yml) so the image can include tools/common
# next to app.py (ADR 0014).
FROM python:3.12.15-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

WORKDIR /app
COPY common/*.py ./common/
COPY chat-feed/app.py .
# Import smoke test: a missing shared module fails the build, not the deploy.
RUN python -c "import app"

EXPOSE 9500
CMD ["python", "-u", "app.py"]
