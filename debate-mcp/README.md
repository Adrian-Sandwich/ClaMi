# debate — servidor MCP + relay

Tablero de discusión persistente sobre Postgres para que Kimi y Claude Code
opinen como analistas sobre los artefactos de un proyecto, con Adrian de
árbitro. `relay.py` cierra el loop: cuando un agente postea, dispara
automáticamente el turno del otro hasta que el thread cierra.

## Setup

    python3.14 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    # ajustá heads.json con los binarios de tus asientos (defaults: claude + kimi)
    .venv/bin/python schema/migrate.py      # crea/actualiza el esquema
    .venv/bin/python smoke_test.py          # verifica que todo responda

Registrar el servidor MCP en Claude Code y en Kimi:

    .venv/bin/python migrate_paths.py --dry-run   # muestra qué cambiaría
    .venv/bin/python migrate_paths.py             # reapunta ambos configs

Instalar los daemons (relay, refresh horario del grafo, healthcheck):

    launchd/install.sh      # guarda backup de lo que hubiera antes
    launchd/rollback.sh     # vuelta atrás

## Protocolo del debate

- Roles: los asientos del registry `heads.json` (analistas), `adrian` (árbitro
  humano, superusuario: abre decisiones y arbitra).
- Kinds: `analisis` (apertura), `critica`, `respuesta`, `veredicto` (cierre de
  cada analista), `posicion` (voto de una cabeza en una decisión), `resultado`
  (cierre de una decisión, lo postea el sistema), `arbitraje` (solo adrian,
  desempata y cierra una decisión `split`).
- Regla de 3 rounds: análisis → críticas cruzadas → respuestas/veredicto. Si
  hay desacuerdo tras el veredicto, adrian arbitra.
- Flujo agente: `read_thread` → `post_message` → `wait_messages` (long-poll
  LISTEN/NOTIFY, sin polling) hasta que cierre el round.
- El campo `artifact` dice sobre QUÉ opina el thread. Si es una ruta absoluta,
  el relay la usa para elegir el cwd de los agentes que dispara — poné ahí la
  raíz del repo que se está debatiendo.

## Decisiones MAGI

La unidad de trabajo es la **Decisión**: una pregunta/artefacto sometido a las
cabezas. El debate ocurre en el journal (un thread común y corriente, así
memory-graph sigue ingiriendo de la misma fuente); el voto es dato
estructurado, el razonamiento es historia.

- Asientos: Melchior (verdad técnica), Balthasar (cuidado), Casper (deseos
  reales) — personas en `personas.py`. El proveedor que ocupa cada asiento
  (claude/kimi/codex/...) es intercambiable vía `heads.json`, sin tocar código.
- Tools: `start_decision` (solo adrian) → el relay dispara a las cabezas →
  cada cabeza investiga el artefacto y `cast_position(yes|no|conditional|info)`
  con su razonamiento → el motor cierra por mayoría 2/3 con minority report,
  abre rondas de crítica (`critique`/`adaptive`), o declara `split` y pide
  arbitraje. `get_decision` devuelve el dossier completo, incluidos los
  cambios de parecer entre rondas (`mind_changes`): un consenso alcanzado
  después de un cambio vale distinto que uno sostenido desde la primera ronda.
- Protocolos: `vote` (una ronda; split → arbitraje), `critique` (rondas de
  crítica mutua hasta acuerdo o tope), `adaptive` (vota; split 3-vías →
  critique automático).

## Esquema

`schema/` tiene migraciones numeradas y `migrate.py` las aplica una sola vez,
registrándolas en la tabla `schema_version`. Cada una corre en su propia
transacción junto con el registro: o se aplica entera, o no pasa nada.

Existe porque la base se había creado a mano y el DDL no vivía en ningún
archivo: si el disco moría, el tablero no se podía reconstruir desde el código.

## El relay

Escucha los canales `debate_all` (messages, migración 002) y `decision_all`
(apertura/cambio de ronda de una decisión, migración 003) y despierta apenas
entra un evento. Reglas:

- **Un disparo por thread, por el último mensaje.** Antes disparaba un proceso
  por cada fila nueva: dos agentes posteando casi a la vez, o un backlog tras
  una caída, generaban N procesos concurrentes sobre el mismo thread y cada
  ronda duplicaba la cantidad de agentes.
- **Decisiones MAGI**: para cada decisión abierta, el motor puro (`decision.py`)
  dice qué asientos faltan en la ronda actual y el relay los dispara con su
  persona y su prompt de turno (answer o recast). El cierre no es suyo: lo
  aplica `server.py` cuando entra la última posición de la ronda.
- **Tope de `MAX_TRIGGERS_PER_THREAD` / `MAX_TRIGGERS_PER_DECISION` disparos**
  entre un `analisis` y el siguiente. Sin tope, dos analistas que nunca posteen
  `veredicto` lo hacen disparar para siempre — y como los agentes corren con
  permiso de escritura en un cwd real, eso no es sólo gasto de tokens.
- **Espera a cada agente** con timeout, y mata el grupo de procesos si se
  cuelga. Registra exit code y duración en `logs/trigger_events.jsonl`.
- **No pierde turnos**: lo que no llegó a arrancar queda en `pending` y se
  reintenta.
- **Heartbeat** en `logs/relay_heartbeat.json`. launchd reinicia el proceso si
  muere, pero no distingue "vivo" de "vivo y roto".

Cierre de thread libre: dos `veredicto` seguidos de autores distintos, o un
`arbitraje`. Cierre de decisión: mayoría de posiciones, o `arbitraje` de adrian
sobre una decisión `split`. Para reanudar un thread frenado por el tope, posteá
un `arbitraje` o abrí un `analisis` nuevo.

## Salud

    .venv/bin/python healthcheck.py

Chequea que Postgres responda con el esquema aplicado, que el heartbeat del
relay sea reciente, que no haya decisiones MAGI trabadas abiertas, y que
`memory.db` no esté envejecida. Corre cada 15 minutos vía launchd y avisa por
notificación de macOS. Exit code 0/1/2.

## UI MAGI

    .venv/bin/python magi_ui.py        # http://127.0.0.1:8051

Interfaz del sistema de decisiones: el triángulo MAGI con las tres cabezas,
alimentado en vivo por SSE desde LISTEN/NOTIFY — cada voto recolorea su
polígono al instante, con parpadeo mientras la cabeza piensa. El kanji del
veredicto cae al centro (承認/否絶/状態/情報/誤差/膠着) y un clic en cada
cabeza abre su razonamiento completo. Desde el formulario se abren
decisiones nuevas: POST /start usa el mismo `board.start_decision` que el
tool MCP. Es un port a vanilla JS de los componentes de TomaszRewak/MAGI
(MIT © 2023 — el CSS se conserva verbatim, ver el aviso en `ui/style.css`);
el server es stdlib puro, sin build step ni dependencias. También corre vía
launchd (`com.adrianmedina.magi-ui.plist`).

## Configuración

| Variable | Default |
|---|---|
| `DEBATE_CONNINFO` | `dbname=debate user=adrianmedina host=localhost` |
| `DEBATE_HEADS` | JSON inline de asientos; pisa a `heads.json` |
| `DEBATE_DEFAULT_CWD` | la raíz de este monorepo |

Los binarios de los asientos viven en `heads.json` (seat → name/bin/args), no
en env vars: el modelo que ocupa un asiento se cambia editando ese archivo. Un
asiento sin binario existente queda inactivo y las decisiones corren degradadas
(queda anotado en el dossier).

**Asientos API** (para probar con modelos locales, sin claude/kimi):
`type: "api"` con `model` + `base_url` apuntando a cualquier endpoint
OpenAI-compatible — Ollama (`http://localhost:11434/v1`), LM Studio,
llama.cpp. El relay le inlinea el journal en el prompt y el modelo responde
con un tag `POSITION:` que se registra como voto. Los tres asientos pueden
compartir el mismo modelo cargado una sola vez (Ollama atiende en paralelo);
la persona diferencia las cabezas. Ejemplo:

```json
{"seat": "melchior", "name": "qwen3", "type": "api",
 "model": "qwen3", "base_url": "http://localhost:11434/v1"}
```

## Tests

    .venv/bin/pytest ../tests
