#!/usr/bin/env python3
"""Generate the Grafana dashboard JSON for the wow-server VM (host + containers)."""
import json

DS = {"type": "prometheus", "uid": "efnf4d4t8vsw0a"}
HOST = 'host="wow-server"'
CID = 'host="wow-server",name!="",name!~"POD"'


def ts(pid, title, x, y, w, h, unit, targets, desc="", extra_defaults=None):
    defaults = {"unit": unit}
    if extra_defaults:
        defaults.update(extra_defaults)
    return {
        "id": pid, "type": "timeseries", "title": title, "description": desc,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "datasource": DS,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "targets": [{"refId": chr(65 + i), "expr": e, "legendFormat": l, "datasource": DS}
                    for i, (e, l) in enumerate(targets)],
    }


def row(pid, title, y):
    return {"id": pid, "type": "row", "title": title, "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []}


panels = []
pid = 0

# --- Host row ---
pid += 1
panels.append(row(pid, "Host — wow-server (192.168.1.64)", 0))

pid += 1
panels.append(ts(pid, "CPU usage", 0, 1, 8, 8, "percent",
    [(f'100 - (avg(rate(node_cpu_seconds_total{{mode="idle",{HOST}}}[5m])) * 100)', "CPU %")],
    "Percent of CPU time not idle (5m rate). During map extraction this pegs ~100%."))

pid += 1
panels.append(ts(pid, "Load average", 8, 1, 8, 8, "short",
    [(f"node_load1{{{HOST}}}", "1m"), (f"node_load5{{{HOST}}}", "5m"), (f"node_load15{{{HOST}}}", "15m")],
    "Load average. For 4 vCPU, sustained >4 means saturation."))

pid += 1
panels.append(ts(pid, "Uptime", 16, 1, 8, 8, "s",
    [(f"node_time_seconds{{{HOST}}} - node_boot_time_seconds{{{HOST}}}", "uptime")],
    "Time since last boot.", {"custom": {"drawStyle": "line", "fillOpacity": 10}}))

pid += 1
panels.append(ts(pid, "Memory", 0, 9, 12, 8, "bytes",
    [(f"node_memory_MemTotal_bytes{{{HOST}}} - node_memory_MemAvailable_bytes{{{HOST}}}", "used"),
     (f"node_memory_MemAvailable_bytes{{{HOST}}}", "available"),
     (f"node_memory_Cached_bytes{{{HOST}}}", "cached")],
    "Used / available / cached RAM. VM has 6 GB total."))

pid += 1
panels.append(ts(pid, "Disk usage — /", 12, 9, 12, 8, "bytes",
    [(f'node_filesystem_size_bytes{{{HOST},mountpoint="/"}} - node_filesystem_avail_bytes{{{HOST},mountpoint="/"}}', "used"),
     (f'node_filesystem_avail_bytes{{{HOST},mountpoint="/"}}', "free")],
    "Root filesystem. 50 GB volume: client 17 GB + extracted maps + DB."))

pid += 1
panels.append(ts(pid, "Disk I/O", 0, 17, 12, 8, "Bps",
    [(f'rate(node_disk_read_bytes_total{{{HOST},device=~"sda|vda"}}[5m])', "read"),
     (f'rate(node_disk_written_bytes_total{{{HOST},device=~"sda|vda"}}[5m])', "write")],
    "Disk throughput."))

pid += 1
panels.append(ts(pid, "Network — eth0", 12, 17, 12, 8, "Bps",
    [(f'rate(node_network_receive_bytes_total{{{HOST},device="eth0"}}[5m])', "rx"),
     (f'rate(node_network_transmit_bytes_total{{{HOST},device="eth0"}}[5m])', "tx")],
    "Network throughput. A connected WoW client shows steady traffic on 8085/3724."))

# --- Containers row ---
pid += 1
panels.append(row(pid, "Containers — cadvisor", 25))

pid += 1
panels.append(ts(pid, "Container CPU", 0, 26, 12, 8, "percent",
    [(f'sum(rate(container_cpu_usage_seconds_total{{{CID}}}[5m])) by (name) * 100', "{{name}}")],
    "Per-container CPU. trinitycore-wowserver drives map extraction and world ticks."))

pid += 1
panels.append(ts(pid, "Container memory", 12, 26, 12, 8, "bytes",
    [(f'container_memory_working_set_bytes{{{CID}}}', "{{name}}")],
    "Per-container working set. Compose caps wowserver at 5g, MySQL at 1g."))

pid += 1
panels.append(ts(pid, "Container network RX", 0, 34, 12, 8, "Bps",
    [(f'sum(rate(container_network_receive_bytes_total{{{CID}}}[5m])) by (name)', "{{name}}")],
    "Per-container receive throughput."))

pid += 1
panels.append(ts(pid, "Container network TX", 12, 34, 12, 8, "Bps",
    [(f'sum(rate(container_network_transmit_bytes_total{{{CID}}}[5m])) by (name)', "{{name}}")],
    "Per-container transmit throughput."))

dash = {
    "uid": "wow-server-host",
    "title": "Wow Server — Host & Containers",
    "tags": ["wow-server", "homelab", "trinitycore"],
    "timezone": "browser",
    "refresh": "30s",
    "editable": True,
    "time": {"from": "now-6h", "to": "now"},
    "schemaVersion": 39,
    "version": 1,
    "panels": panels,
}

with open("/root/wow-dashboard.json", "w") as f:
    json.dump({"dashboard": dash, "overwrite": True, "message": "wow-server host dashboard"}, f)
print("panels:", len([p for p in panels if p["type"] != "row"]))
