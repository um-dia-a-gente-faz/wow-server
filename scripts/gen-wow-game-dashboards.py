#!/usr/bin/env python3
"""Generate the Grafana dashboards for the WoW server (game + realm health)."""
import json

DS = {"type": "prometheus", "uid": "efnf4d4t8vsw0a"}
GRID_W = 24


def base_targets(targets, instant=False):
    out = []
    for i, (expr, legend) in enumerate(targets):
        t = {"refId": chr(65 + i), "expr": expr, "legendFormat": legend, "datasource": DS}
        if instant:
            t["instant"] = True
            t["format"] = "table"
        out.append(t)
    return out


def panel(pid, ptype, title, x, y, w, h, targets, unit=None, desc="", instant=False, options=None):
    defaults = {}
    if unit:
        defaults["unit"] = unit
    p = {
        "id": pid, "type": ptype, "title": title, "description": desc,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "datasource": DS,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "targets": base_targets(targets, instant),
    }
    if options:
        p["options"] = options
    return p


def ts(pid, title, x, y, w, h, unit, targets, desc="", fill=True):
    d = {"unit": unit, "custom": {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 12 if fill else 0,
                                  "showPoints": "never"}}
    return panel(pid, "timeseries", title, x, y, w, h, targets, desc=desc,
                 options={"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                          "tooltip": {"mode": "multi", "sort": "desc"}}) | {
        "fieldConfig": {"defaults": d, "overrides": []}}


def stat(pid, title, x, y, w, h, unit, targets, desc="", thresholds=None):
    d = {"unit": unit}
    if thresholds:
        d["thresholds"] = thresholds
        d["color"] = {"mode": "thresholds"}
    return panel(pid, "stat", title, x, y, w, h, targets, desc=desc,
                 options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                          "orientation": "auto", "textMode": "auto", "colorMode": "value",
                          "graphMode": "area", "justifyMode": "auto"}) | {
        "fieldConfig": {"defaults": d, "overrides": []}}


def bargauge(pid, title, x, y, w, h, unit, targets, desc="", instant=True):
    return panel(pid, "bargauge", title, x, y, w, h, targets,
                 unit=unit, desc=desc, instant=instant,
                 options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                          "orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True})


def pie(pid, title, x, y, w, h, targets, desc=""):
    return panel(pid, "piechart", title, x, y, w, h, targets, desc=desc, instant=True,
                 options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                          "pieType": "pie",
                          "legend": {"displayMode": "list", "placement": "right", "showLegend": True,
                                     "values": ["value"]}})


def row(pid, title, y):
    return {"id": pid, "type": "row", "title": title, "collapsed": False,
            "gridPos": {"h": 1, "w": GRID_W, "x": 0, "y": y}, "panels": []}


def table(pid, title, x, y, w, h, targets, desc=""):
    return panel(pid, "table", title, x, y, w, h, targets, desc=desc, instant=True,
                 options={"showHeader": True, "cellHeight": "sm"})


# =====================================================================================
# Dashboard 1 — Players & Activity
# =====================================================================================
p, pid = [], 0

pid += 1
p.append(row(pid, "Agora", 0))

pid += 1
p.append(stat(pid, "Jogadores online", 0, 1, 5, 5, "short",
              [('wow_players_online', "jogadores")],
              "Personagens com online=1 agora.",
              {"mode": "absolute", "steps": [{"color": "red", "value": None},
                                             {"color": "yellow", "value": 1},
                                             {"color": "green", "value": 5}]}))

pid += 1
p.append(stat(pid, "Contas online", 5, 1, 5, 5, "short",
              [('wow_accounts_online', "contas")], "Contas com online=1 agora."))

pid += 1
p.append(stat(pid, "Pico (24h)", 10, 1, 5, 5, "short",
              [('max_over_time(wow_players_online[24h])', "pico 24h")],
              "Máximo de jogadores simultâneos nas últimas 24 horas."))

pid += 1
p.append(stat(pid, "Pico (histórico)", 15, 1, 5, 5, "short",
              [('wow_realm_peak_players_alltime', "pico")],
              "Maior número de jogadores simultâneos já registrado pelo realm."))

pid += 1
p.append(stat(pid, "Uptime do realm", 20, 1, 4, 5, "s",
              [('wow_realm_uptime_seconds', "uptime")],
              "Tempo desde o início da execução atual do worldserver."))

pid += 1
p.append(row(pid, "Atividade ao longo do tempo", 6))

pid += 1
p.append(ts(pid, "Jogadores online", 0, 7, 12, 8, "short",
            [('wow_players_online', "jogadores online"),
             ('wow_accounts_online', "contas online")],
            "Série histórica de concorrência. É a métrica principal — deixe o exporter "
            "rodando para acumular histórico."))

pid += 1
p.append(ts(pid, "Contas ativas / criadas", 12, 7, 12, 8, "short",
            [('wow_accounts_active{window="24h"}', "ativas 24h"),
             ('wow_accounts_active{window="7d"}', "ativas 7d"),
             ('wow_accounts_created{window="24h"}', "criadas 24h"),
             ('wow_accounts_created{window="7d"}', "criadas 7d")],
            "Contas que logaram / foram criadas dentro da janela."))

pid += 1
p.append(row(pid, "População", 15))

pid += 1
p.append(bargauge(pid, "Personagens por nível", 0, 16, 8, 8, "short",
                  [('wow_characters_by_level', "{{level}}")],
                  "Distribuição de níveis. Buckets vazios somem sozinhos (o collector "
                  "recalcula a cada scrape)."))

pid += 1
p.append(pie(pid, "Personagens por classe", 8, 16, 8, 8,
             [('wow_characters_by_class', "{{class}}")], "Distribuição de classes."))

pid += 1
p.append(pie(pid, "Personagens por raça", 16, 16, 8, 8,
             [('wow_characters_by_race', "{{race}}")], "Distribuição de raças."))

pid += 1
p.append(ts(pid, "Jogadores online por zona", 0, 24, 12, 8, "short",
            [('wow_players_by_zone', "{{zone_name}}")],
            "Onde os jogadores online estão. IDs de zona sem nome conhecido caem pro "
            "número — o TDB em uso não tem tabela de áreas."))

pid += 1
p.append(bargauge(pid, "Guilds por membros", 12, 24, 12, 8, "short",
                  [('wow_guild_members', "{{guild}}")],
                  "Tamanho das guilds. Vazio enquanto não houver nenhuma guild."))

pid += 1
p.append(row(pid, "Engajamento e economia", 32))

pid += 1
p.append(bargauge(pid, "Personagens mais jogados", 0, 33, 12, 9, "s",
                  [('wow_character_playtime_seconds', "{{character}}")],
                  "Tempo de jogo acumulado por personagem (top 20)."))

pid += 1
p.append(bargauge(pid, "Nível dos mais jogados", 12, 33, 6, 9, "short",
                  [('wow_character_level', "{{character}}")],
                  "Nível dos personagens do painel ao lado (mesmo top 20 por tempo de jogo)."))

pid += 1
p.append(stat(pid, "Tempo de jogo total", 18, 33, 6, 4, "s",
              [('wow_playtime_seconds_total', "total")], "Soma do tempo de jogo de todos os personagens."))

pid += 1
p.append(stat(pid, "Ouro total", 18, 37, 6, 5, "short",
              [('wow_money_gold_total', "ouro")], "Soma do dinheiro de todos os personagens, em ouro."))

pid += 1
p.append(row(pid, "Totais", 42))

pid += 1
p.append(stat(pid, "Contas", 0, 43, 6, 4, "short",
              [('wow_accounts_total', "contas")], "Total de contas criadas."))
pid += 1
p.append(stat(pid, "Personagens", 6, 43, 6, 4, "short",
              [('wow_characters_total', "personagens")], "Total de personagens criados."))
pid += 1
p.append(stat(pid, "Guilds", 12, 43, 6, 4, "short",
              [('wow_guilds_total', "guilds")], "Total de guilds."))
pid += 1
p.append(stat(pid, "Já logaram", 18, 43, 6, 4, "short",
              [('wow_accounts_ever_logged_in', "contas")],
              "Contas que fizeram login pelo menos uma vez."))

dash_players = {
    "uid": "wow-players",
    "title": "WoW — Jogadores & Atividade",
    "tags": ["wow", "wow-server", "trinitycore", "homelab"],
    "timezone": "browser", "refresh": "30s", "editable": True,
    "time": {"from": "now-6h", "to": "now"},
    "schemaVersion": 39, "version": 1, "panels": p,
}

# =====================================================================================
# Dashboard 2 — Realm & Server Health
# =====================================================================================
q, pid = [], 0

pid += 1
q.append(row(pid, "Saúde do realm", 0))

pid += 1
q.append(stat(pid, "Exporter do jogo", 0, 1, 4, 5, "short",
              [('wow_exporter_up', "up")],
              "1 = o exporter conseguiu ler o MySQL no último scrape. 0 = falhou.",
              {"mode": "absolute", "steps": [{"color": "red", "value": None},
                                             {"color": "green", "value": 1}]}))

pid += 1
q.append(stat(pid, "Uptime do realm", 4, 1, 5, 5, "s",
              [('wow_realm_uptime_seconds', "uptime")], "Tempo desde o início do worldserver atual."))

pid += 1
q.append(stat(pid, "Início do realm", 9, 1, 5, 5, "dateTimeAsIso",
              [('wow_realm_start_time_seconds * 1000', "início")],
              "Quando o worldserver atual subiu."))

pid += 1
q.append(stat(pid, "Conexões MySQL", 14, 1, 5, 5, "short",
              [('wow_mysql_connections', "conexões")], "Conexões ativas no banco."))

pid += 1
q.append(stat(pid, "Duração do scrape", 19, 1, 5, 5, "s",
              [('wow_exporter_scrape_duration_seconds', "duração")],
              "Quanto o exporter leva para consultar o banco. Se subir muito, o banco está lento."))

pid += 1
q.append(row(pid, "Dados do jogo", 6))

pid += 1
q.append(ts(pid, "Tamanho dos bancos", 0, 7, 12, 8, "bytes",
            [('wow_db_size_bytes', "{{database}}")],
            "auth + characters são pequenos; world carrega o TDB inteiro (~460 MB)."))

pid += 1
q.append(ts(pid, "Personagens e contas", 12, 7, 12, 8, "short",
            [('wow_characters_total', "personagens"),
             ('wow_accounts_total', "contas"),
             ('wow_accounts_ever_logged_in', "contas que logaram")],
            "Crescimento da base. Escala devagar — use um range longo (30d)."))

pid += 1
q.append(row(pid, "Segurança das contas", 15))

pid += 1
q.append(stat(pid, "Contas travadas", 0, 16, 6, 5, "short",
              [('wow_accounts_locked', "travadas")],
              "Contas bloqueadas por excesso de tentativas.",
              {"mode": "absolute", "steps": [{"color": "green", "value": None},
                                             {"color": "yellow", "value": 1},
                                             {"color": "red", "value": 3}]}))

pid += 1
q.append(stat(pid, "Contas com falha de login", 6, 16, 6, 5, "short",
              [('wow_accounts_with_failed_logins', "contas")],
              "Contas com pelo menos uma falha de login registrada."))

pid += 1
q.append(ts(pid, "Tentativas de login falhas", 12, 16, 12, 5, "short",
            [('wow_failed_logins_total', "falhas acumuladas")],
            "Soma do contador de falhas. Um salto = alguém martelando uma senha."))

pid += 1
q.append(row(pid, "Containers do jogo", 21))

pid += 1
q.append(ts(pid, "CPU dos containers do jogo", 0, 22, 8, 8, "percent",
            [('sum(rate(container_cpu_usage_seconds_total{host="wow-server",name=~"trinitycore-.*"}[5m])) by (name) * 100',
              "{{name}}")], "CPU do worldserver e do MySQL."))

pid += 1
q.append(ts(pid, "Memória dos containers do jogo", 8, 22, 8, 8, "bytes",
            [('container_memory_working_set_bytes{host="wow-server",name=~"trinitycore-.*"}', "{{name}}")],
            "Memória dos containers do jogo. O compose limita worldserver a 5g e MySQL a 1g."))

pid += 1
q.append(ts(pid, "Rede dos containers do jogo", 16, 22, 8, 8, "Bps",
            [('sum(rate(container_network_receive_bytes_total{host="wow-server",name=~"trinitycore-.*"}[5m])) by (name)',
              "rx {{name}}"),
             ('sum(rate(container_network_transmit_bytes_total{host="wow-server",name=~"trinitycore-.*"}[5m])) by (name)',
              "tx {{name}}")],
            "Tráfego de rede do worldserver — cresce junto com o número de jogadores conectados."))

dash_realm = {
    "uid": "wow-realm-health",
    "title": "WoW — Saúde do Realm",
    "tags": ["wow", "wow-server", "trinitycore", "homelab"],
    "timezone": "browser", "refresh": "30s", "editable": True,
    "time": {"from": "now-6h", "to": "now"},
    "schemaVersion": 39, "version": 1, "panels": q,
}

for d in (dash_players, dash_realm):
    fn = f"/root/{d['uid']}.json"
    with open(fn, "w") as f:
        json.dump(d, f, indent=2)
    n = len([x for x in d["panels"] if x["type"] != "row"])
    r = len([x for x in d["panels"] if x["type"] == "row"])
    print(f"{fn}: {d['title']} — {n} painéis, {r} seções")
