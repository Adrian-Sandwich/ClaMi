#!/bin/bash
# Instala (o reinstala) los tres agentes de launchd del monorepo.
#
# Antes de pisar nada guarda una copia de los plists que ya estuvieran
# instalados en launchd/backup/, para poder volver atrás con rollback.sh.
set -euo pipefail
cd "$(dirname "$0")"

AGENTS_DIR="$HOME/Library/LaunchAgents"
BACKUP_DIR="./backup"
GUI="gui/$(id -u)"

mkdir -p "$AGENTS_DIR" "$BACKUP_DIR"

for plist in com.adrianmedina.*.plist; do
    label="${plist%.plist}"
    target="$AGENTS_DIR/$plist"

    if [[ -f "$target" ]] && [[ ! -f "$BACKUP_DIR/$plist" ]]; then
        cp "$target" "$BACKUP_DIR/$plist"
        echo "backup: $plist -> $BACKUP_DIR/"
    fi

    # bootout de lo que hubiera corriendo con esa etiqueta; puede no existir
    launchctl bootout "$GUI/$label" 2>/dev/null || true

    cp "$plist" "$target"
    launchctl bootstrap "$GUI" "$target"
    echo "instalado: $label"
done

echo
launchctl list | grep -E 'debate-relay|clami-' || echo "(nada corriendo todavía)"
