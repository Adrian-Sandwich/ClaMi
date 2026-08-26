# memory-graph

Grafo de memoria entre todas las conversaciones, estilo Obsidian. Ingesta a un
SQLite propio (`memory.db`) y exporta un `.kgraph.json` para el visor 3D de
[Node_visualizer](../../Node_visualizer).

## Qué entra al grafo

| Ingestor | Fuente | Dominio de nodo |
|---|---|---|
| `ingest_claude.py` | `~/.claude/projects/**/*.jsonl` | `claude_session` |
| `ingest_kimi.py` | `~/.kimi-code/sessions/wd_*/session_*/agents/*/wire.jsonl` | `kimi_session` |
| `ingest_debate.py` | tabla `messages` de Postgres | `debate_thread` |
| `ingest_docs.py` | globs de `.md` configurables | `doc` |
| `ingest_code.py` | SQLite de codebase-memory-mcp | `code` |

Los edges cruzan las fuentes: una sesión `touched` un archivo, `belongs_to` un
proyecto, `posted_to` un thread del tablero; un doc `documents` un thread y
`references` un archivo. Cuando el proyecto está indexado en
codebase-memory-mcp, el archivo tocado resuelve al MISMO nodo `code:` que creó
`ingest_code.py` (eso lo hace `code_lookup.py`), en vez de duplicar un nodo
`file:` genérico.

## Uso

    ./refresh.sh          # re-ingesta todo y exporta

Es idempotente: correrlo dos veces seguidas no cambia el contenido. Corre solo
cada hora vía launchd (`../debate-mcp/launchd/com.adrianmedina.clami-refresh.plist`).

## Configuración

Todo sale de `settings.py` y se puede pisar por entorno:

| Variable | Default |
|---|---|
| `DEBATE_CONNINFO` | `dbname=debate user=adrianmedina host=localhost` |
| `MEMORY_GRAPH_DB` | `./memory.db` |
| `NODE_VISUALIZER_DIR` | `../../Node_visualizer` |
| `MEMORY_GRAPH_OUT` | `$NODE_VISUALIZER_DIR/graphs/memory.kgraph.json` |
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects` |
| `KIMI_HOME` | `~/.kimi-code` |
| `CODEBASE_MEMORY_CACHE` | `~/.cache/codebase-memory-mcp` |
| `MEMORY_GRAPH_DOCS` | lista JSON o separada por `:` de globs de `.md` |
| `MEMORY_GRAPH_DOC_ROOTS` | raíces de repo, separadas por `:`, para resolver las referencias de cada doc |
| `MEMORY_GRAPH_PYTHON` | intérprete que usa `refresh.sh` |

## Cómo está armado

- `db.py` — nodos y edges en SQLite con upsert por natural key. `props` se
  mergea en vez de pisarse, para que dos ingestors que tocan el mismo nodo no
  se borren los datos del otro.
- `jsonl_facts.py` — la parte mecánica de leer un log de sesión (líneas rotas,
  timestamps, archivos tocados, threads). Los dialectos de Claude y Kimi viven
  en sus ingestors; esto es lo que comparten.
- `code_lookup.py` — resuelve un path absoluto al nodo `code:` correspondiente,
  leyendo las SQLite de codebase-memory-mcp directo.
- `export_kgraph.py` — vuelca a `.kgraph.json` pasando por el `finalize()` del
  contrato de Node_visualizer (dedupe, descarta edges colgantes).

### Ingesta incremental

`db.file_cache` guarda los hechos ya extraídos de cada log con su `(mtime,
size)`. Los transcripts son append-only, así que un archivo con el mismo mtime
y tamaño tiene el mismo contenido y no se vuelve a parsear. Una corrida sin
cambios cuesta unos segundos contra los ~6s de una completa, y la diferencia
crece con el histórico.

### Recolección

El grafo antes sólo sabía crecer: las sesiones o docs borrados quedaban para
siempre. Ahora cada ingestor declara qué nodos vio y `db.sweep_domain()` borra
del dominio lo que ya no aparece, junto con sus edges. Se barre por *dominio* y
no por *source* a propósito: un nodo `project:` lo escriben varios ingestors y
el último gana la columna `source`, así que barrer por source borraría cosas
vivas.

## Tests

    ../debate-mcp/.venv/bin/pytest ../tests

Los `test_canary_*` corren contra los logs REALES del disco y fallan si el
formato de Claude o Kimi cambia. Existen porque un comentario en el código
afirmó durante semanas que kimi-code había dejado de emitir eventos
`tool.call`; al medirlo, era falso — y nadie lo había revalidado. Un test que
mira el log de ayer no envejece igual que una nota en prosa.
