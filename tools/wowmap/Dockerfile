# Build context is tools/ (see monitoring/docker-compose.yml) so the image can
# include the shared DBC reader in tools/dbc next to the wowmap sources.
FROM python:3.12-slim

WORKDIR /app
COPY wowmap/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dbc/__init__.py dbc/wdbc.py dbc/names.py ./dbc/
COPY wowmap/transform.py wowmap/overlays.py wowmap/item_icons.py wowmap/item_tooltip.py wowmap/activity.py wowmap/app.py wowmap/calibration.json ./

EXPOSE 9400
CMD ["python", "-u", "app.py"]
