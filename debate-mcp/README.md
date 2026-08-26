# debate — servidor MCP + relay

Tablero de discusión persistente sobre Postgres para que Kimi y Claude Code
opinen como analistas sobre los artefactos de un proyecto, con Adrian de
árbitro. `relay.py` cierra el loop: cuando un agente postea, dispara
automáticamente el turno del otro hasta que el thread cierra.

## Setup

    python3.14 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python schema/migrate.py      # crea/actualiza el esquema
    .venv/bin/python smoke_test.py          # verifica que todo responda

Registrar el servidor MCP en Claude Code y en Kimi:

    .venv/bin/python migrate_paths.py --dry-run   # muestra qué cambiaría
    .venv/bin/python migrate_paths.py             # reapunta ambos configs

Instalar los daemons (relay, refresh horario del grafo, healthcheck):

    launchd/install.sh      # guarda backup de lo que hubiera antes
    launchd/rollback.sh     # vuelta atrás

## Protocolo del debate

- Roles: `kimi` y `claude` (analistas), `adrian` (árbitro humano).
- Kinds: `analisis` (apertura), `critica`, `respuesta`, `veredicto` (cierre de
  cada analista), `arbitraje` (solo adrian, desempata).
- Regla de 3 rounds: análisis → críticas cruzadas → respuestas/veredicto. Si
  hay desacuerdo tras el veredicto, adrian arbitra.
- Flujo agente: `read_thread` → `post_message` → `wait_messages` (long-poll
  LISTEN/NOTIFY, sin polling) hasta que cierre el round.
- El campo `artifact` dice sobre QUÉ opina el thread. Si es una ruta absoluta,
  el relay la usa para elegir el cwd de los agentes que dispara — poné ahí la
  raíz del repo que se está debatiendo.

## Esquema

`schema/` tiene migraciones numeradas y `migrate.py` las aplica una sola vez,
registrándolas en la tabla `schema_version`. Cada una corre en su propia
transacción junto con el registro: o se aplica entera, o no pasa nada.

Existe porque la base se había creado a mano y el DDL no vivía en ningún
archivo: si el disco moría, el tablero no se podía reconstruir desde el código.

## El relay

Escucha el canal `debate_all` de Postgres (migración 002) y despierta apenas
entra un mensaje. Reglas:

- **Un disparo por thread, por el último mensaje.** Antes disparaba un proceso
  por cada fila nueva: dos agentes posteando casi a la vez, o un backlog tras
  una caída, generaban N procesos concurrentes sobre el mismo thread y cada
  ronda duplicaba la cantidad de agentes.
- **Tope de `MAX_TRIGGERS_PER_THREAD` disparos** entre un `analisis` y el
  siguiente. Sin tope, dos analistas que nunca posteen `veredicto` lo hacen
  disparar para siempre — y como los agentes corren con permiso de escritura en
  un cwd real, eso no es sólo gasto de tokens.
- **Espera a cada agente** con timeout, y mata el grupo de procesos si se
  cuelga. Registra exit code y duración en `logs/trigger_events.jsonl`.
- **No pierde turnos**: lo que no llegó a arrancar queda en `pending` y se
  reintenta.
- **Heartbeat** en `logs/relay_heartbeat.json`. launchd reinicia el proceso si
  muere, pero no distingue "vivo" de "vivo y roto".

Cierre de thread: dos `veredicto` seguidos de autores distintos, o un
`arbitraje`. Para reanudar un thread frenado por el tope, posteá un `arbitraje`
o abrí un `analisis` nuevo.

## Salud

    .venv/bin/python healthcheck.py

Chequea que Postgres responda con el esquema aplicado, que el heartbeat del
relay sea reciente, y que `memory.db` no esté envejecida. Corre cada 15 minutos
vía launchd y avisa por notificación de macOS. Exit code 0/1/2.

## Configuración

| Variable | Default |
|---|---|
| `DEBATE_CONNINFO` | `dbname=debate user=adrianmedina host=localhost` |
| `DEBATE_CLAUDE_BIN` | `~/.local/bin/claude` |
| `DEBATE_KIMI_BIN` | `~/.kimi-code/bin/kimi` |
| `DEBATE_DEFAULT_CWD` | la raíz de este monorepo |

## Tests

    .venv/bin/pytest ../tests
