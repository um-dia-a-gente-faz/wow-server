# Grafana provisioning

File-based dashboard provisioning — the only reliable way to manage dashboards
for this Grafana instance, since `admin:admin` was rotated after first boot and
the write API rejects basic auth (anonymous Viewer falls through and gets
`Access denied`).

## Structure

```
grafana/
  provisioning/
    dashboards.yaml        ← providers: Pandora (general) + WoW Server
```

The actual dashboard JSONs live in `monitoring/` in the repo and are deployed to
`/opt/pandora/grafana/dashboards-wow/` on the docker-stack VM.