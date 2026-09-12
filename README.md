# ClaMi

Infraestructura de colaboración multiagente sobre la máquina local: un
**consejo MAGI de tres cabezas** (Melchior/Balthasar/Casper — la persona vive
en el asiento, el proveedor es intercambiable: claude/kimi/codex/Ollama/…)
que debate en un journal auditable y vota decisiones con ruling, minority
report y arbitraje humano. La interfaz del operador es una **web estilo MAGI**
(interfaz de TomaszRewak/MAGI) con un chat de texto libre: consultas al
consejo (votación formal) o charla con las tres cabezas. Corre en Windows,
macOS y Linux — sin rutas hardcodeadas de ninguna máquina puntual.

**Este repo es la fuente única**: el venv, el esquema de la base, los
scripts de arranque y el registro del MCP viven acá dentro.

## Estructura

    debate-mcp/      servidor MCP "debate", relay, esquema, UI web, bin/, healthcheck
    memory-graph/    ingestors + SQLite del grafo de memoria, export .kgraph.json
    tests/           suite compartida (pytest), con fixtures propias
    .mcp.json        registra el MCP "debate" para cualquier agente en este repo
    pytest.ini       testpaths + flags

## Componentes

### [`debate-mcp/`](debate-mcp/README.md)

Servidor MCP "debate": tablero de discusión persistente sobre Postgres
(LISTEN/NOTIFY para long-poll sin polling) + motor de **decisiones MAGI**:
tres asientos con persona (Melchior/Balthasar/Casper) y proveedor
intercambiable (`heads.json`: claude/kimi/codex/Ollama/…, asientos CLI o API
OpenAI-compatible) que debaten en un journal y votan posiciones estructuradas
hasta ruling por mayoría, minority report o arbitraje. Incluye `relay.py`,
daemon que orquesta los turnos; `magi_ui.py`, la interfaz web (chat + tablero
en vivo por SSE); `schema/` con las migraciones versionadas; y
`healthcheck.py`.

### [`memory-graph/`](memory-graph/README.md)

Grafo de memoria entre todas las conversaciones, estilo Obsidian: ingesta
sesiones de Claude Code, sesiones de Kimi, threads y decisiones del tablero,
docs del proyecto y el grafo de código de codebase-memory-mcp a un SQLite
propio, y lo exporta como `.kgraph.json` para el visor 3D de
[Node_visualizer](../Node_visualizer). `refresh.sh` re-ingesta todo
idempotentemente, en incremental.

## Puesta en marcha

Requisitos: Python 3.14+ y Postgres accesible. El conninfo default es
`dbname=debate` (libpq toma usuario y host del entorno); si tu setup difiere,
exportá `DEBATE_CONNINFO` con el conninfo estándar de libpq.

    cd debate-mcp
    python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\python -m venv .venv
    .venv/bin/python schema/migrate.py     # esquema de la base (idempotente)
    .venv/bin/python smoke_test.py         # ¿arranca todo?
    .venv/bin/python relay.py &            # orquestador de turnos (daemon)
    .venv/bin/python magi_ui.py &          # interfaz web en http://127.0.0.1:8051

Atajos por plataforma:

- **Windows**: `debate-mcp\bin\start-magi.bat` levanta Postgres portable (si
  no corre), migra y abre relay + UI en ventanas propias;
  `debate-mcp\bin\stop-magi.bat` baja todo.
- **macOS**: `debate-mcp/launchd/install.sh` registra relay, UI, refresh
  horario del grafo y healthcheck en launchd.

Las cabezas se configuran en `debate-mcp/heads.json` (fuente única del
repo, editable por máquina). En esta máquina: Melchior = Kimi CLI,
Balthasar = qwen3 (Ollama), Casper = qwen2.5-coder (Ollama).

Estado del sistema en cualquier momento:

    debate-mcp/.venv/bin/python debate-mcp/healthcheck.py

## Tests

    debate-mcp/.venv/bin/pip install -r debate-mcp/requirements-dev.txt
    debate-mcp/.venv/bin/pytest

121 tests repartidos en seis archivos:

- `tests/test_parsers.py` (13) — parseo de los JSONL de Claude y Kimi, más los
  canarios de formato contra los logs reales.
- `tests/test_relay.py` (38) — disparo del relay, radio de daño, orquestación
  de decisiones MAGI (CLI y API), turnos de chat, paralelismo por asiento,
  portabilidad del kill de procesos y estado.
- `tests/test_db.py` (13) — capa de acceso del grafo de memoria y de la
  ingesta de decisiones.
- `tests/test_decision.py` (33) — motor de decisiones: mayorías, splits,
  rondas de crítica, cambios de parecer, turnos pendientes, prompts de cabeza
  y mensajes del operador humano.
- `tests/test_apihead.py` (9) — asientos API: parseo del voto `POSITION:` y
  chat contra un endpoint OpenAI-compatible de mentira.
- `tests/test_ui.py` (17) — UI MAGI: kanji del veredicto, snapshot con chat,
  frame SSE, server HTTP end-to-end y robustez del POST.

## Ramas

`main` es el tronco y es lo que corre en la máquina. Las ramas de trabajo
salen de `main` y vuelven por fast-forward, sin merge commits.

## Notas de operación

- La base se llama `debate`. El conninfo default no fija usuario ni host:
  libpq usa el usuario del sistema operativo y localhost. Para apuntar a otra
  base u otro servidor, `DEBATE_CONNINFO`.
- El `.mcp.json` del repo apunta al venv por ruta relativa. En Windows el
  intérprete del venv es `.venv/Scripts/python.exe` (el archivo ya trae esa
  variante); en macOS/Linux, `bin/python`. Un registro viejo a mano en la
  config global del agente (con la ruta absoluta de otro checkout) le gana y
  falla con ENOENT: `migrate_paths.py` lo reescribe.
- Los `test_canary_*` corren contra los logs reales de `~/.claude` y
  `~/.kimi-code`, y fallan cuando el formato de esos logs cambia. Es a
  propósito: son el detector de drift de los ingestors. Si esos logs no están
  (otra máquina, CI), se saltan solos.
- Si `healthcheck.py` reporta Postgres inalcanzable, mirá el log del servidor
  (`pg.log` del portable, o el del servicio de tu sistema) antes que nada: un
  `postmaster.pid` stale o una laptop suspendida producen el mismo síntoma
  (el relay lo sobrevive con backoff, pero el tablero no procesa hasta que la
  base vuelve).
