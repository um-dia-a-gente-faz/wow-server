#!/bin/bash
set -e
SRC="/root/wow-client/World of Warcraft 3.3.5a"
DEST="/root/wow-clients"

mkdir -p "$DEST"

echo "[1/3] Copiando base (warmane) -> $DEST/wow-3.3.5a-warmane"
rm -rf "$DEST/wow-3.3.5a-warmane"
cp -a "$SRC" "$DEST/wow-3.3.5a-warmane"
echo "     OK: $(du -sh "$DEST/wow-3.3.5a-warmane" | cut -f1)"

echo "[2/3] Copiando -> $DEST/wow-3.3.5a-pandora"
rm -rf "$DEST/wow-3.3.5a-pandora"
cp -a "$SRC" "$DEST/wow-3.3.5a-pandora"

echo "[3/3] Apontando realmlist para o server local (192.168.1.64)"
for loc in enUS ptBR ptPT; do
  d="$DEST/wow-3.3.5a-pandora/Data/$loc"
  [ -d "$d" ] || continue
  printf 'set realmlist 192.168.1.64\nset patchlist 192.168.1.64\n' > "$d/realmlist.wtf"
  echo "     $loc -> $(cat "$d/realmlist.wtf" | tr '\n' ' ')"
done

echo "DONE"
ls -la "$DEST"
