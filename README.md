# ClaMi

Infraestructura de colaboración entre agentes CLI (Claude Code + Kimi) sobre la
máquina local. **Este repo es la fuente única**: el venv, el esquema de la base,
los plists de launchd y el registro del MCP viven acá dentro.

## Estructura

    debate-mcp/      servidor MCP "debate", relay, esquema, launchd, healthcheck
    memory-graph/    ingestors + SQLite del grafo de memoria, export .kgraph.json
    tests/           suite compartida (pytest), con fixtures propias
    .mcp.json        registra el MCP "debate" para cualquier agente en este repo
    pytest.ini       testpaths + flags

## Componentes

### [`debate-mcp/`](debate-mcp/README.md)

Servidor MCP "debate": tablero de discusión persistente sobre Postgres
(LISTEN/NOTIFY para long-poll sin polling) donde Kimi y Claude opinan como
analistas, con Adrian de árbitro. Incluye `relay.py`, daemon que cierra el loop
disparando el turno del otro agente cuando uno postea; `schema/` con las
migraciones versionadas de la base; y `healthcheck.py`.

### [`memory-graph/`](memory-graph/README.md)

Grafo de memoria entre todas las conversaciones, estilo Obsidian: ingesta
sesiones de Claude Code, sesiones de Kimi, threads del tablero, docs del
proyecto y el grafo de código de codebase-memory-mcp a un SQLite propio, y lo
exporta como `.kgraph.json` para el visor 3D de
[Node_visualizer](../Node_visualizer). `refresh.sh` re-ingesta todo
idempotentemente, en incremental.

## Puesta en marcha

    cd debate-mcp
    python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt
    .venv/bin/python schema/migrate.py     # esquema de la base
    .venv/bin/python smoke_test.py         # ¿arranca todo?
    .venv/bin/python migrate_paths.py      # registra el MCP en Claude y Kimi
    launchd/install.sh                     # relay + refresh horario + healthcheck

Estado del sistema en cualquier momento:

    debate-mcp/.venv/bin/python debate-mcp/healthcheck.py

## Tests

    debate-mcp/.venv/bin/pip install -r debate-mcp/requirements-dev.txt
    debate-mcp/.venv/bin/pytest

47 tests repartidos en tres archivos:

- `tests/test_parsers.py` (12) — parseo de los JSONL de Claude y Kimi, más los
  canarios de formato contra los logs reales.
- `tests/test_relay.py` (23) — disparo del relay, radio de daño y estado.
- `tests/test_db.py` (12) — capa de acceso del grafo de memoria.

## Ramas

`main` es el tronco y es lo que corre en la máquina. Las ramas de trabajo
salen de `main` y vuelven por fast-forward, sin merge commits. `consolidar-monorepo`
quedó en el mismo commit que `main` tras el merge de agosto de 2026: no tiene
trabajo pendiente y se puede borrar.

## Notas de operación

- La base se llama `debate`. Hasta agosto de 2026 se llamaba `trade_debate`,
  de cuando el tablero arrancó para el proyecto de trading; hoy es de propósito
  general y el nombre viejo confundía. Si tenés un checkout que todavía apunta
  al anterior, pisá el default con `DEBATE_CONNINFO`.
- El `.mcp.json` del repo apunta al venv por ruta relativa, así que el MCP
  resuelve desde cualquier clon. Un registro viejo a mano en la config global
  del agente (con la ruta absoluta de otro checkout) le gana y falla con
  ENOENT: `migrate_paths.py` lo reescribe.
- Los `test_canary_*` corren contra los logs reales de `~/.claude` y
  `~/.kimi-code`, y fallan cuando el formato de esos logs cambia. Es a
  propósito: son el detector de drift de los ingestors. Si esos logs no están
  (otra máquina, CI), se saltan solos.
- Si `healthcheck.py` reporta Postgres inalcanzable, mirá primero
  `/opt/homebrew/var/postgresql@14/postmaster.pid`: un PID reciclado por otro
  proceso deja a launchd reintentando en loop con "lock file already exists".
