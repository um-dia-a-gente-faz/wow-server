# Build context is tools/ (see monitoring/docker-compose.yml) so the image can
# include the shared DBC reader in tools/dbc next to the wowmap sources.
FROM python:3.12-slim

WORKDIR /app
COPY wowmap/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dbc/__init__.py dbc/wdbc.py dbc/names.py dbc/spelltext.py ./dbc/
COPY wowmap/transform.py wowmap/overlays.py wowmap/item_icons.py wowmap/item_tooltip.py wowmap/activity.py wowmap/fog.py wowmap/fleet.py wowmap/walk.py wowmap/app.py wowmap/state.py wowmap/webio.py wowmap/routes.py wowmap/pages.py \
     wowmap/players.py wowmap/character.py wowmap/inventory.py wowmap/areas.py wowmap/fogview.py \
     wowmap/agents.py wowmap/calibration.py wowmap/assets.py wowmap/calibration.json ./
COPY wowmap/repo ./repo
# Vendored Leaflet, served by app.py under /static/.
COPY wowmap/static ./static

EXPOSE 9400
CMD ["python", "-u", "app.py"]
