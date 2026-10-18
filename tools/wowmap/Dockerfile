# Build context is tools/ (see monitoring/docker-compose.yml) so the image can
# include the shared DBC reader (tools/dbc) and tools/common next to the wowmap sources.
# Stage 1: the SvelteKit app (#324). Only web/build leaves this stage: the final image has no node.
# Build context is tools/, so the app reads ../openapi.json from wowmap/.
FROM node:22.22.1-slim@sha256:4f77a690f2f8946ab16fe1e791a3ac0667ae1c3575c3e4d0d4589e9ed5bfaf3d AS web
WORKDIR /web
COPY wowmap/web/package.json wowmap/web/package-lock.json ./
RUN npm ci
# Not `COPY wowmap/web/`: a local node_modules or build must not reach the image.
COPY wowmap/web/*.js wowmap/web/*.ts wowmap/web/tsconfig.json ./
COPY wowmap/web/src/ ./src/
COPY wowmap/web/static/ ./static/
RUN npm run build

FROM python:3.12.15-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

WORKDIR /app
COPY wowmap/requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# tools/common (ADR 0014): the .py files only, not its tests.
COPY common/*.py ./common/
COPY dbc/__init__.py dbc/wdbc.py dbc/names.py dbc/spelltext.py ./dbc/
# The whole runtime directory (modules, repo/, static/, calibration.json). The offline
# pipeline/ is not shipped (#392); a new module needs no edit here.
COPY wowmap/wowmap/ ./
# The built SvelteKit app, served at /v2 (api.WEB_BUILD).
COPY --from=web /web/build/ ./web/
# Import smoke test: a module missing from the image fails the build, not the deploy.
RUN python -c "import app, routes, api"

EXPOSE 9400
CMD ["python", "-u", "app.py"]
