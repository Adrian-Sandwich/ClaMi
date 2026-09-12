#!/bin/bash
# Re-ingesta todo el grafo de memoria y lo exporta al visor.
#
# Idempotente: correrlo dos veces seguidas no cambia el contenido. Con el cache
# de archivos (db.file_cache) la segunda corrida además es barata, porque sólo
# re-parsea los logs que cambiaron.
#
# Se agenda con launchd (ver ../debate-mcp/launchd/). Estaba pensado para
# correrse a mano, y el resultado fue previsible: el 2026-08-26 la memory.db
# tenía 19 días, con 15 sesiones de Kimi cargadas contra 75 archivos en disco.
set -euo pipefail
cd "$(dirname "$0")"

# Intérprete portable: el venv de Windows vive en .venv/Scripts, el de
# macOS/Linux en .venv/bin. Override puntual con MEMORY_GRAPH_PYTHON.
ROOT="$(cd .. && pwd)"
PY="${MEMORY_GRAPH_PYTHON:-}"
if [[ -z "$PY" ]]; then
    if [[ -x "$ROOT/debate-mcp/.venv/Scripts/python.exe" ]]; then
        PY="$ROOT/debate-mcp/.venv/Scripts/python.exe"
    else
        PY="$ROOT/debate-mcp/.venv/bin/python"
    fi
fi
if [[ ! -x "$PY" ]]; then
    echo "refresh.sh: no encuentro el intérprete en $PY" >&2
    echo "  creá el venv:  python3.14 -m venv ../debate-mcp/.venv && ../debate-mcp/.venv/bin/pip install -r ../debate-mcp/requirements.txt" >&2
    exit 1
fi

echo "== refresh $(date -Iseconds) =="
"$PY" ingest_claude.py
"$PY" ingest_kimi.py
"$PY" ingest_debate.py
"$PY" ingest_docs.py
"$PY" ingest_code.py
"$PY" export_kgraph.py
echo "== listo $(date -Iseconds) =="
