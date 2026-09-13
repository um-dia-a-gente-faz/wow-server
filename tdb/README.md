# tdb/ — TrinityCore base world dump

This directory holds the **TDB full world SQL** that populates the `world` database.
It is **not committed** (280 MB) — fetch it when setting up.

## Why the exact version matters

The container image downloads the *newest* TDB release from the TrinityCore GitHub
releases, but the `worldserver` binary bundled in the image is compiled expecting
**one specific filename**. If they differ, bootstrap fails:

```
>> File "TDB_full_world_335.25101_2025_10_21.sql" is missing, download it from
   "https://github.com/TrinityCore/TrinityCore/releases" ...
Could not populate the World database, see log for details.
```

## How to fetch it

The exact filename the binary wants is printed in that error message. To see it
without guessing, run the binary by hand (see docs/DEPLOYMENT.md → "Debugging the
bootstrap"). For the `3.3.5` image tag, the current expected version is **25101**:

```bash
mkdir -p tdb && cd tdb
curl -sL -o tdb.7z \
  https://github.com/TrinityCore/TrinityCore/releases/download/TDB335.25101/TDB_full_world_335.25101_2025_10_21.7z

docker run --rm -v "$PWD:/tdb" --entrypoint /bin/sh \
  danielsilvestre37/trinitycore-docker:3.3.5 -c 'cd /tdb && 7z x tdb.7z -y'

ls -lh TDB_full_world_335.*.sql
```

The resulting `.sql` is bind-mounted into the container at
`/app/server/bin/<exact-filename>` by `docker-compose.yml`.

## Updating

If you change the image tag (e.g. a newer `3.3.5` build), the expected TDB version
may change. Re-run the manual `worldserver -u` trick to read the new expected
filename, download that release, and update both the filename and the bind mount in
`docker-compose.yml`.
