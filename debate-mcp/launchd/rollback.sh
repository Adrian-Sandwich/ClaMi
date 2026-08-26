#!/bin/bash
# Vuelta atrás de install.sh: restaura los plists que había antes (los que
# quedaron en backup/) y descarga los agentes que este repo agregó.
#
# El plan de migración del monorepo era explícito en esto: archivar, no borrar,
# y tener el camino de vuelta escrito antes de tocar producción.
set -euo pipefail
cd "$(dirname "$0")"

AGENTS_DIR="$HOME/Library/LaunchAgents"
BACKUP_DIR="./backup"
GUI="gui/$(id -u)"

for plist in com.adrianmedina.*.plist; do
    label="${plist%.plist}"
    launchctl bootout "$GUI/$label" 2>/dev/null || true

    if [[ -f "$BACKUP_DIR/$plist" ]]; then
        cp "$BACKUP_DIR/$plist" "$AGENTS_DIR/$plist"
        launchctl bootstrap "$GUI" "$AGENTS_DIR/$plist"
        echo "restaurado el plist previo: $label"
    else
        rm -f "$AGENTS_DIR/$plist"
        echo "desinstalado (no existía antes): $label"
    fi
done

echo
echo "Falta revertir a mano, si querés volver del todo:"
echo "  - ~/.claude.json y ~/.kimi-code/mcp.json (hay .bak junto a cada uno)"
echo "  - los directorios archivados: ~/src/debate-mcp.old y ~/src/memory-graph.old"
