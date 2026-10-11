# Build context is tools/ (see monitoring/docker-compose.yml) so the image can
# include the shared DBC reader in tools/dbc next to the wowmap sources.
FROM python:3.12-slim

WORKDIR /app
COPY wowmap/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dbc/__init__.py dbc/wdbc.py dbc/names.py dbc/spelltext.py ./dbc/
# The whole runtime directory (modules, repo/, static/, calibration.json). The offline
# pipeline/ is not shipped (#392); a new module needs no edit here.
COPY wowmap/wowmap/ ./
# Import smoke test: a module missing from the image fails the build, not the deploy.
RUN python -c "import app, routes"

EXPOSE 9400
CMD ["python", "-u", "app.py"]
