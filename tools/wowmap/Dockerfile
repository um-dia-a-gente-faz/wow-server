# Build context is tools/ (see monitoring/docker-compose.yml) so the image can
# include the shared DBC reader (tools/dbc) and tools/common next to the wowmap sources.
FROM python:3.12.15-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

WORKDIR /app
COPY wowmap/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# tools/common (ADR 0014): the .py files only, not its tests.
COPY common/*.py ./common/
COPY dbc/__init__.py dbc/wdbc.py dbc/names.py dbc/spelltext.py ./dbc/
# The whole runtime directory (modules, repo/, static/, calibration.json). The offline
# pipeline/ is not shipped (#392); a new module needs no edit here.
COPY wowmap/wowmap/ ./
# Import smoke test: a module missing from the image fails the build, not the deploy.
RUN python -c "import app, routes"

EXPOSE 9400
CMD ["python", "-u", "app.py"]
