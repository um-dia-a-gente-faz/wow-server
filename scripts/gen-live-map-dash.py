#!/usr/bin/env python3
"""Generate the WoW live-map Grafana dashboard."""
import sys

from genlib import dashboard_text, sync

DS = {"type": "prometheus", "uid": "efnf4d4t8vsw0a"}
panels, pid = [], 0


def panel(t, title, x, y, w, h, targets=None, **kw):
    global pid
    pid += 1
    p = {"id": pid, "type": t, "title": title, "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": DS}
    if targets:
        p["targets"] = targets
    p.update(kw)
    panels.append(p)
    return p


# --- link to the actual live map app
panel("text", "Mapa ao vivo", 0, 0, 24, 3, options={"mode": "markdown", "content": """
# [Abrir console ao vivo →](http://192.168.1.64:9400)

O console mostra, numa só página: mapa interativo com cada jogador online
(cor de classe, seletor de zona), lista de jogadores, chat público ao vivo
(aba Chat) e um painel de inspeção de personagem (clique num nome ou
marcador). Atualização a cada 5 segundos.

Abaixo, os mesmos dados como métricas no Grafana (sem o mapa de fundo —
para isso, use o link acima).
""".strip()})  # noqa: E501

# --- stats
for i, (q, label, unit) in enumerate([
    ("wow_players_online", "online", "short"),
    ("wow_accounts_online", "contas online", "short"),
    ("wow_players_in_instances", "em instância", "short"),
]):
    panel("stat", "Jogadores " + label, i * 8, 3, 8, 4, unit=unit,
          targets=[{"refId": "A", "expr": q, "datasource": DS}],
          options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                   "orientation": "auto", "textMode": "auto", "colorMode": "value", "graphMode": "area"},
          fieldConfig={"defaults": {"unit": unit, "thresholds": {"mode": "absolute",
                        "steps": [{"color": "green", "value": None}]}}, "overrides": []})

# --- XY scatter: positions (Grafana-native, no background map but correct coords)
panel("timeseries", "Posições dos jogadores (XY)", 0, 7, 24, 12,
      options={"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
               "tooltip": {"mode": "multi", "sort": "desc"}},
      targets=[
          {"refId": "X", "expr": 'wow_player_position_x', "legendFormat": "{{character}}", "datasource": DS},
          {"refId": "Y", "expr": 'wow_player_position_y', "legendFormat": "{{character}}", "datasource": DS},
      ],
      fieldConfig={"defaults": {"unit": "short",
                                "custom": {"drawStyle": "points", "pointSize": 8, "showPoints": "always"}},
                   "overrides": []})

dash = {"uid": "wow-live-map", "title": "WoW — Mapa ao Vivo",
        "tags": ["wow", "wow-server", "trinitycore", "homelab"],
        "timezone": "browser", "refresh": "15s", "editable": True,
        "time": {"from": "now-1h", "to": "now"},
        "schemaVersion": 39, "version": 1, "panels": panels}

sys.exit(sync("monitoring/grafana-dashboard-wow-live-map.json", dashboard_text(dash),
              "--check" in sys.argv, "scripts/gen-live-map-dash.py"))