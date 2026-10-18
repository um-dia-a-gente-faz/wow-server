# Build context is tools/ (see monitoring/docker-compose.yml). Stdlib only: no requirements file.
FROM python:3.12.15-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

WORKDIR /app
COPY pathfinder/*.py ./pathfinder/

# Import smoke test: a module missing from the image fails the build, not the deploy.
RUN python -c "import pathfinder.server"

ENV MMAPS_DIR=/server-data/mmaps \
    PATHFINDER_PORT=8770
EXPOSE 8770
# Answers 200 only when the server is up; urlopen raises on any HTTP error or refused connection.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request as u; u.urlopen('http://127.0.0.1:%s/healthz' % os.environ['PATHFINDER_PORT'], timeout=2)"
CMD ["python", "-u", "-m", "pathfinder.server"]
