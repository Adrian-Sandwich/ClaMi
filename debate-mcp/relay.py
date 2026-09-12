#!/usr/bin/env python3
"""Daemon relay: orquesta los turnos del tablero 'debate'.

Dos responsabilidades sobre la misma base:

1. Decisiones MAGI: escucha el canal 'decision_all' (apertura/cambio de
   ronda de una decisión) y el clásico 'debate_all' (messages). Para cada
   decisión abierta, el motor puro (decision.py) dice qué asientos faltan
   actuar en la ronda actual; el relay los dispara. Cada asiento tiene su
   candado in-flight propio (thread::seat): las tres cabezas de una decisión
   corren EN PARALELO, sin pisarse entre sí.

2. Threads libres (compatibilidad): cuando un autor postea, dispara al
   siguiente asiento del registry para que responda, hasta que el thread
   cierra con veredictos cruzados o arbitraje.

Tipos de asiento (heads.json):
- CLI: un proceso con herramientas (claude/kimi/codex/...). El prompt le
  apunta al MCP: lee el journal, investiga el artefacto y vota con
  cast_position.
- API (apihead.py): un POST a un endpoint OpenAI-compatible (Ollama, LM
  Studio, llama.cpp). No tiene herramientas: el relay le inlinea el journal
  en el prompt y el modelo responde con un tag POSITION que se registra como
  voto con la misma lógica que cast_position (board.record_position). La
  decisión no distingue de dónde salió el voto.

Lo que aprendimos de la versión anterior, en el debate 'clami-mejoras' y
viéndolo fallar en vivo el 2026-08-26:

- Hacía `time.sleep(20)` contra una base que ya empuja eventos por
  LISTEN/NOTIFY. Ahora escucha los canales de notify y despierta al instante,
  con un wake ocioso cada IDLE_WAKE_SECS que además refresca el heartbeat.
- Disparaba un proceso POR CADA fila nueva. Si dos agentes posteaban casi a
  la vez (o si el relay volvía de estar caído con backlog), salían N procesos
  concurrentes sobre el mismo thread y el debate se duplicaba en cada ronda:
  el thread 'clami-mejoras' generó 15 mensajes de más así. Ahora se colapsa a
  un disparo por turno pendiente, con candado in-flight por asiento.
- No tenía tope de rondas: sin tope, dos analistas que nunca posteen
  'veredicto' lo hacían disparar para siempre. Y como los agentes corren con
  permiso de escritura en un cwd real, eso no es sólo gasto de tokens: es
  radio de daño. Ahora hay topes por thread y por decisión.
- Hacía Popen y se olvidaba: procesos zombie, y un agente colgado quedaba
  colgado para siempre. Ahora se espera con timeout, se mata el grupo de
  procesos si se pasa, y se registra exit code y duración.
- Avanzaba el watermark antes de saber si el disparo había salido. Si el
  binario no estaba, ese turno se perdía sin reintento. Ahora lo que no
  llegó a arrancar queda en `pending` y se reintenta.
- Corría todo con cwd fijo en un solo proyecto. Ahora el cwd sale del campo
  `artifact` del thread.
- El "otro agente" era un flip binario kimi/claude hardcodeado. Ahora los
  asientos viven en heads.json: el modelo que ocupa un asiento es
  intercambiable (CLI o API) sin tocar este archivo.
"""

import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg  # noqa: E402

import apihead  # noqa: E402
import board  # noqa: E402
import memory_ctx  # noqa: E402
import decision  # noqa: E402
import heads  # noqa: E402
import personas  # noqa: E402

from config import connect  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "relay_state.json"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
HEARTBEAT_PATH = LOG_DIR / "relay_heartbeat.json"
EVENTS_PATH = LOG_DIR / "trigger_events.jsonl"

CHANNEL_ALL = "debate_all"
CHANNEL_DECISIONS = "decision_all"

# Sin notify, despertamos igual cada tanto: refresca el heartbeat y reintenta
# lo que haya quedado en `pending`.
IDLE_WAKE_SECS = 300
MAX_BACKOFF_SECS = 300

# Tope de disparos entre un 'analisis' y el siguiente (threads libres) o por
# decisión. Un debate de 3 rondas gasta ~6; 12 deja margen para idas y
# vueltas sin dejar que un loop corra indefinidamente.
MAX_TRIGGERS_PER_THREAD = 12
MAX_TRIGGERS_PER_DECISION = 12

# Un agente que no terminó en 15 minutos está colgado.
AGENT_TIMEOUT_SECS = 900

# Techo global de turnos concurrentes, sumando CLI y API.
MAX_CONCURRENT_TRIGGERS = 4

# Último recurso cuando el thread no dice sobre qué proyecto opina.
DEFAULT_CWD = os.environ.get("DEBATE_DEFAULT_CWD", str(BASE_DIR.parent))

# Override manual, gana sobre el artifact.
THREAD_CWD: dict[str, str] = {}

PROTOCOL = """Roles: los asientos del registry (heads.json) como analistas, adrian como arbitro humano.
Kinds: analisis (apertura), critica, respuesta, veredicto (cierre de cada analista), arbitraje (solo adrian). Regla de 3 rounds: analisis -> criticas cruzadas -> respuestas/veredicto. Si hay desacuerdo tras el veredicto, adrian arbitra."""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "relay.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("relay")

# turnos corriendo ahora mismo, identificados por token:
#   "thread"        → thread libre (un agente por thread)
#   "thread::seat"  → turno de una cabeza en una decisión (paralelo por asiento)
_inflight: set[str] = set()
_inflight_lock = threading.Lock()

_RE_LINE_SUFFIX = re.compile(r":\d+$")
# Los journals de decisiones viven en threads 'd<id>' (provisional al abrir,
# final tras el primer INSERT — ver board.start_decision). El patrón queda
# reservado: un thread libre con ese nombre sería secuestrado por el motor.
_RE_DECISION_THREAD = re.compile(r"^d\d+$")


def _token(thread: str, seat: str | None = None) -> str:
    return f"{thread}::{seat}" if seat else thread


# ---------------------------------------------------------------- estado

def load_state() -> dict:
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    else:
        state = {}
    state.setdefault("last_id", 0)
    state.setdefault("threads", {})
    state.setdefault("pending", [])
    return state


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_PATH)


def thread_state(state: dict, thread: str) -> dict:
    ts = state["threads"].setdefault(thread, {})
    ts.setdefault("triggers", 0)
    ts.setdefault("cwd", None)
    return ts


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def event(kind: str, **fields) -> None:
    """Una línea JSON por evento. Es lo que hace medible al relay: cuántos
    disparos, cuánto tardan, cuántos fallan. `healthcheck.py` lee esto."""
    rec = {"ts": now_iso(), "event": kind, **fields}
    with EVENTS_PATH.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def write_heartbeat(state: dict, pg_ok: bool) -> None:
    """Latido en disco. launchd ya reinicia el proceso si muere, pero no sabe
    distinguir 'vivo' de 'vivo y roto' — que fue exactamente el estado del
    relay mientras Postgres estuvo caído: proceso arriba, loop escupiendo
    OperationalError cada 20s, nadie enterado."""
    with _inflight_lock:
        inflight = sorted(_inflight)
    HEARTBEAT_PATH.write_text(json.dumps({
        "ts": now_iso(),
        "pid": os.getpid(),
        "pg_ok": pg_ok,
        "last_id": state["last_id"],
        "inflight": inflight,
        "pending": len(state["pending"]),
        "capped_threads": sorted(
            t for t, ts in state["threads"].items()
            if ts.get("triggers", 0) >= MAX_TRIGGERS_PER_THREAD
        ),
    }, indent=1))


# ---------------------------------------------------------------- cwd

def cwd_from_artifact(artifact: str | None) -> str | None:
    """El campo `artifact` dice sobre qué opina el thread ("/Users/x/repo" o
    "src/paper.rs:120"). Si es una ruta absoluta, buscamos hacia arriba la
    raíz de repo — ahí es donde tiene sentido correr al agente."""
    if not artifact:
        return None
    p = Path(_RE_LINE_SUFFIX.sub("", artifact.strip()))
    if not p.is_absolute():
        return None
    candidate = p if p.is_dir() else p.parent
    for parent in [candidate, *candidate.parents]:
        if (parent / ".git").exists():
            return str(parent)
    return str(candidate) if candidate.is_dir() else None


def resolve_cwd(conn, thread: str, ts: dict) -> str:
    if thread in THREAD_CWD:
        return THREAD_CWD[thread]
    if ts.get("cwd"):
        return ts["cwd"]
    rows = conn.execute(
        """
        SELECT artifact FROM messages
        WHERE thread = %s AND artifact IS NOT NULL
        ORDER BY id LIMIT 5
        """,
        (thread,),
    ).fetchall()
    for r in rows:
        resolved = cwd_from_artifact(r["artifact"])
        if resolved:
            ts["cwd"] = resolved
            log.info("thread %s -> cwd %s (desde artifact)", thread, resolved)
            return resolved
    ts["cwd"] = DEFAULT_CWD
    log.warning("thread %s sin artifact usable, cwd por defecto %s", thread, DEFAULT_CWD)
    return DEFAULT_CWD


# ---------------------------------------------------------------- threads libres

def thread_closed(conn, thread: str) -> bool:
    rows = conn.execute(
        """
        SELECT author, kind FROM messages
        WHERE thread = %s ORDER BY id DESC LIMIT 2
        """,
        (thread,),
    ).fetchall()
    if not rows:
        return False
    if rows[0]["kind"] == "arbitraje":
        return True
    if len(rows) == 2:
        a, b = rows
        if (
            a["kind"] == "veredicto"
            and b["kind"] == "veredicto"
            and a["author"] != b["author"]
        ):
            return True
    return False


def build_prompt(thread: str, since_id: int, last_author: str, seat_to_call: str) -> str:
    return (
        f"Continua el debate en el tablero MCP 'debate', thread '{thread}'. "
        f"Protocolo:\n{PROTOCOL}\n\n"
        f"Usa read_thread(thread='{thread}', since_id={since_id}) para ver los "
        f"mensajes nuevos de '{last_author}'. Respondé como '{seat_to_call}' "
        f"con post_message(thread='{thread}', author='{seat_to_call}', kind=..., body=...) "
        f"usando el kind que corresponda según el protocolo (critica, "
        f"respuesta o veredicto). Posteá UN SOLO mensaje. No uses "
        f"wait_messages, no hace falta: este disparo es automático. Al "
        f"terminar decime solo el id del mensaje que posteaste."
    )


def next_seat(author: str, seats: list[str]) -> str | None:
    """Quien responde en un thread libre: el siguiente asiento del registry
    (round-robin). Con dos asientos es el flip clásico del debate libre."""
    if not seats:
        return None
    if author not in seats:
        return seats[0]
    return seats[(seats.index(author) + 1) % len(seats)]


# ---------------------------------------------------------------- disparo CLI

def _agent_cmd(seat_name: str, prompt: str) -> list[str]:
    """El comando del asiento sale del registry: el modelo que lo ocupa es
    intercambiable sin tocar este archivo. ValueError si no tiene binario."""
    seat = heads.seat_by_name(seat_name)
    if seat is None or not seat.get("bin"):
        raise ValueError(f"asiento {seat_name!r} sin binario en el registry")
    return [seat["bin"], *seat.get("args", []), prompt]


def _kill_tree(proc: subprocess.Popen) -> None:
    """Mata al agente colgado y a los procesos que haya lanzado.

    POSIX: start_new_session hizo que el hijo sea líder de su grupo, y killpg
    se lleva el árbol entero. Windows: no hay grupos POSIX ni SIGKILL;
    taskkill /T /F recorre el árbol de hijos solo. Sin esto, un agente colgado
    en Windows crasheaba el hilo supervisor (os.killpg no existe acá).
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _supervise(proc: subprocess.Popen, meta: dict) -> None:
    """Espera al agente, lo mata si se cuelga, y deja el resultado medido."""
    start = time.monotonic()
    timed_out = False
    try:
        rc = proc.wait(timeout=AGENT_TIMEOUT_SECS)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        rc = proc.wait()
    duration = round(time.monotonic() - start, 1)

    if timed_out:
        log.error("agente %s en %s colgado tras %ss, matado", meta["author"], meta["thread"], AGENT_TIMEOUT_SECS)
    elif rc != 0:
        log.error("agente %s en %s salió con rc=%s (%.0fs)", meta["author"], meta["thread"], rc, duration)
    else:
        log.info("agente %s en %s ok (%.0fs)", meta["author"], meta["thread"], duration)

    event("trigger_done", rc=rc, timed_out=timed_out, duration_s=duration, **meta)

    with _inflight_lock:
        _inflight.discard(meta["token"])


def trigger(seat_name: str, thread: str, since_id: int, cwd: str, prompt: str, meta: dict | None = None) -> bool:
    """Lanza al asiento CLI. Devuelve False si no llegó a arrancar — el
    llamador lo reencola en `pending` en vez de perder el turno."""
    ts_label = time.strftime("%Y%m%dT%H%M%S")
    out_path = LOG_DIR / f"{thread}_{seat_name}_{ts_label}.log"
    meta = {
        "thread": thread, "author": seat_name, "since_id": since_id, "cwd": cwd,
        "token": _token(thread, seat_name) if (meta or {}).get("decision_id") else _token(thread),
        **(meta or {}),
    }

    try:
        f = out_path.open("w")
        # start_new_session es POSIX (grupo de procesos para el kill en el
        # timeout); en Windows taskkill /T ya recorre el árbol de hijos.
        popen_kw = {"start_new_session": True} if os.name == "posix" else {}
        proc = subprocess.Popen(
            _agent_cmd(seat_name, prompt),
            cwd=cwd,
            stdout=f,
            stderr=subprocess.STDOUT,
            **popen_kw,
        )
    except (OSError, ValueError) as exc:
        log.error("no pude lanzar %s en %s: %s", seat_name, thread, exc)
        event("trigger_spawn_failed", error=str(exc), **meta)
        return False

    with _inflight_lock:
        _inflight.add(meta["token"])
    log.info("disparo %s en thread=%s cwd=%s -> %s", seat_name, thread, cwd, out_path.name)
    event("trigger_spawned", pid=proc.pid, **meta)

    threading.Thread(target=_supervise, args=(proc, meta), daemon=True).start()
    return True


# ---------------------------------------------------------------- disparo API

def _run_api_turn(seat_info: dict, d: dict, memory: str | None = None) -> None:
    """Un turno de asiento API, síncrono: journal inline → chat → voto
    registrado con la misma lógica que cast_position. Si algo falla, el
    error queda en eventos: pending_turns sigue viendo al asiento sin votar
    y lo re-dispara en el próximo ciclo (con tope por decisión)."""
    start = time.monotonic()
    meta = {
        "thread": d["thread"], "author": seat_info["seat"], "token": _token(d["thread"], seat_info["seat"]),
        "decision_id": d["id"], "round": d["round"], "turn": "api",
    }
    try:
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT author, kind, body FROM messages
                WHERE thread = %s ORDER BY id DESC LIMIT %s
                """,
                (d["thread"], apihead.JOURNAL_LIMIT),
            ).fetchall()
            journal = [dict(m) for m in reversed(rows)]
            vote = apihead.run_turn(seat_info, d, journal, memory=memory)
            with conn.transaction():
                board.record_position(
                    conn, d["id"], seat_info["seat"],
                    vote["position"], vote["body"], vote["conditions"],
                )
        log.info(
            "asiento API %s votó %s en decisión %s (%.0fs)",
            seat_info["seat"], vote["position"], d["id"], time.monotonic() - start,
        )
        event("trigger_done", rc=0, timed_out=False,
              duration_s=round(time.monotonic() - start, 1), **meta)
    except Exception as exc:
        log.error("turno API de %s en %s falló: %s", seat_info["seat"], d["thread"], exc)
        event("trigger_done", rc=1, error=str(exc),
              duration_s=round(time.monotonic() - start, 1), **meta)


def _run_api_turn_bg(seat_info: dict, d: dict, memory: str | None = None) -> None:
    try:
        _run_api_turn(seat_info, d, memory)
    finally:
        with _inflight_lock:
            _inflight.discard(_token(d["thread"], seat_info["seat"]))


def fire_api_turn(seat_info: dict, d: dict, memory: str | None = None) -> bool:
    """Dispara el turno de un asiento API en una decisión, en un thread propio."""
    with _inflight_lock:
        _inflight.add(_token(d["thread"], seat_info["seat"]))
    log.info(
        "turno API %s (modelo %s) en decisión %s, ronda %s",
        seat_info["seat"], seat_info.get("model"), d["id"], d["round"],
    )
    event("trigger_spawned", pid=None, thread=d["thread"], author=seat_info["seat"],
          decision_id=d["id"], round=d["round"], turn="api")
    threading.Thread(target=_run_api_turn_bg, args=(seat_info, d, memory), daemon=True).start()
    return True


# ---------------------------------------------------------------- turnos API de chat libre

def _run_api_chat_turn(seat_info: dict, thread: str, since_id: int) -> None:
    """Turno de chat libre de un asiento API: journal inline → chat → UN
    mensaje kind='respuesta'. Mismo contrato que el disparo CLI (un mensaje
    por turno). Si algo falla el mensaje no se inserta: el thread sigue con
    el mismo último autor y el próximo ciclo re-dispara (reintento implícito).
    Las cabezas API no emiten 'veredicto' — sin decisión no tienen posición
    que votar: el humano cierra el chat con 'arbitraje' o el tope corta."""
    start = time.monotonic()
    meta = {"thread": thread, "author": seat_info["seat"], "token": _token(thread), "turn": "free-api"}
    try:
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT author, kind, body FROM messages
                WHERE thread = %s ORDER BY id DESC LIMIT %s
                """,
                (thread, apihead.JOURNAL_LIMIT),
            ).fetchall()
            journal = [dict(m) for m in reversed(rows)]
            text = apihead.run_chat_turn(seat_info, journal)
            conn.execute(
                """
                INSERT INTO messages (thread, author, kind, body, artifact)
                VALUES (%s, %s, 'respuesta', %s, NULL)
                """,
                (thread, seat_info["seat"], text),
            )
        log.info("asiento API %s respondió en %s (%.0fs)",
                 seat_info["seat"], thread, time.monotonic() - start)
        event("trigger_done", rc=0, timed_out=False,
              duration_s=round(time.monotonic() - start, 1), **meta)
    except Exception as exc:
        log.error("turno API de chat de %s en %s falló: %s", seat_info["seat"], thread, exc)
        event("trigger_done", rc=1, error=str(exc),
              duration_s=round(time.monotonic() - start, 1), **meta)


def _run_api_chat_turn_bg(seat_info: dict, thread: str, since_id: int) -> None:
    try:
        _run_api_chat_turn(seat_info, thread, since_id)
    finally:
        with _inflight_lock:
            _inflight.discard(_token(thread))


def fire_api_chat_turn(seat_info: dict, thread: str, since_id: int) -> bool:
    """Dispara el turno de chat de un asiento API, en un thread propio."""
    with _inflight_lock:
        _inflight.add(_token(thread))
    log.info("turno API de chat %s (modelo %s) en thread %s",
             seat_info["seat"], seat_info.get("model"), thread)
    event("trigger_spawned", pid=None, thread=thread, author=seat_info["seat"], turn="free-api")
    threading.Thread(target=_run_api_chat_turn_bg, args=(seat_info, thread, since_id), daemon=True).start()
    return True


# ------------------------------------------- cabezas CLI sin MCP (journal inline)

def _run_cli_inline(seat_info: dict, prompt: str, cwd: str, timeout: int) -> str:
    """Corre una cabeza CLI con el prompt por STDIN (archivo temporal) y
    devuelve su stdout completo. STDIN en vez de argv: los prompts de turno
    tienen comillas y tildes que el re-quoting de shims .cmd (codex.cmd)
    rompería; y `codex exec -` lee el prompt de stdin de todos modos."""
    with tempfile.TemporaryDirectory(prefix=f"magi-{seat_info['seat']}-") as tmp:
        pin = Path(tmp) / "prompt.txt"
        pout = Path(tmp) / "out.txt"
        pin.write_text(prompt, encoding="utf-8")
        with pin.open("rb") as fin, pout.open("wb") as fout:
            proc = subprocess.Popen(
                [seat_info["bin"], *seat_info.get("args", []), "-"],
                cwd=cwd, stdin=fin, stdout=fout, stderr=subprocess.STDOUT,
            )
            rc = proc.wait(timeout=timeout)
        text = pout.read_text(encoding="utf-8", errors="replace")
    if rc != 0:
        raise RuntimeError(f"{seat_info['seat']} salió rc={rc}: {text[-300:]}")
    return text


def _journal_inline(conn, thread: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT author, kind, body FROM messages
        WHERE thread = %s ORDER BY id DESC LIMIT %s
        """,
        (thread, apihead.JOURNAL_LIMIT),
    ).fetchall()
    return [dict(m) for m in reversed(rows)]


def _run_cli_inline_turn(seat_info: dict, d: dict, cwd: str, memory: str | None = None) -> None:
    """Turno de decisión de una cabeza CLI que NO carga el MCP del tablero
    (p.ej. codex exec: en modo no interactivo no expone tools de servers
    externos — verificado 2026-09-12 con su propio debug log). El relay le
    inlinea el journal en el prompt (mismo builder que las cabezas API) y
    parsea el tag POSITION: de su salida; la salida completa queda como
    body del voto. El proceso puede investigar el repo con sus propias
    herramientas de lectura aunque no pueda votar por MCP."""
    start = time.monotonic()
    meta = {"thread": d["thread"], "author": seat_info["seat"],
            "token": _token(d["thread"], seat_info["seat"]),
            "decision_id": d["id"], "round": d["round"], "turn": "cli-inline"}
    try:
        with connect() as conn:
            journal = _journal_inline(conn, d["thread"])
        system, user = apihead.build_api_prompt(seat_info["seat"], d, journal, memory=memory)
        text = _run_cli_inline(
            seat_info, f"{system}\n\n{user}", cwd,
            seat_info.get("timeout_secs", AGENT_TIMEOUT_SECS),
        )
        vote = apihead.parse_vote(text)
        with connect() as conn:
            with conn.transaction():
                board.record_position(
                    conn, d["id"], seat_info["seat"],
                    vote["position"], vote["body"], vote["conditions"],
                )
        log.info("cabeza inline %s votó %s en decisión %s (%.0fs)",
                 seat_info["seat"], vote["position"], d["id"], time.monotonic() - start)
        event("trigger_done", rc=0, timed_out=False,
              duration_s=round(time.monotonic() - start, 1), **meta)
    except Exception as exc:
        log.error("turno inline de %s en %s falló: %s", seat_info["seat"], d["thread"], exc)
        event("trigger_done", rc=1, error=str(exc),
              duration_s=round(time.monotonic() - start, 1), **meta)


def _run_cli_inline_turn_bg(seat_info: dict, d: dict, cwd: str, memory: str | None = None) -> None:
    try:
        _run_cli_inline_turn(seat_info, d, cwd, memory)
    finally:
        with _inflight_lock:
            _inflight.discard(_token(d["thread"], seat_info["seat"]))


def fire_cli_inline_turn(seat_info: dict, d: dict, cwd: str, memory: str | None = None) -> bool:
    """Dispara el turno de decisión de una cabeza journal-inline."""
    with _inflight_lock:
        _inflight.add(_token(d["thread"], seat_info["seat"]))
    log.info("turno inline %s (%s) en decisión %s, ronda %s",
             seat_info["seat"], seat_info.get("name"), d["id"], d["round"])
    event("trigger_spawned", pid=None, thread=d["thread"], author=seat_info["seat"],
          decision_id=d["id"], round=d["round"], turn="cli-inline")
    threading.Thread(target=_run_cli_inline_turn_bg, args=(seat_info, d, cwd, memory), daemon=True).start()
    return True


def _run_cli_inline_chat_turn(seat_info: dict, thread: str, cwd: str) -> None:
    """Turno de chat libre de una cabeza journal-inline: prompt de charla,
    stdout completo posteado como UN mensaje 'respuesta' (igual contrato
    que el turno API de chat)."""
    start = time.monotonic()
    meta = {"thread": thread, "author": seat_info["seat"], "token": _token(thread),
            "turn": "free-inline"}
    try:
        with connect() as conn:
            journal = _journal_inline(conn, thread)
        system, user = apihead.build_chat_prompt(seat_info["seat"], journal)
        text = _run_cli_inline(
            seat_info, f"{system}\n\n{user}", cwd,
            seat_info.get("timeout_secs", AGENT_TIMEOUT_SECS),
        ).strip()
        with connect() as conn:
            conn.execute(
                """
                INSERT INTO messages (thread, author, kind, body, artifact)
                VALUES (%s, %s, 'respuesta', %s, NULL)
                """,
                (thread, seat_info["seat"], text),
            )
        log.info("cabeza inline %s respondió en %s (%.0fs)",
                 seat_info["seat"], thread, time.monotonic() - start)
        event("trigger_done", rc=0, timed_out=False,
              duration_s=round(time.monotonic() - start, 1), **meta)
    except Exception as exc:
        log.error("turno inline de chat de %s en %s falló: %s", seat_info["seat"], thread, exc)
        event("trigger_done", rc=1, error=str(exc),
              duration_s=round(time.monotonic() - start, 1), **meta)


def _run_cli_inline_chat_turn_bg(seat_info: dict, thread: str, cwd: str) -> None:
    try:
        _run_cli_inline_chat_turn(seat_info, thread, cwd)
    finally:
        with _inflight_lock:
            _inflight.discard(_token(thread))


def fire_cli_inline_chat_turn(seat_info: dict, thread: str, cwd: str) -> bool:
    """Dispara el turno de chat de una cabeza journal-inline."""
    with _inflight_lock:
        _inflight.add(_token(thread))
    log.info("turno inline de chat %s (%s) en thread %s",
             seat_info["seat"], seat_info.get("name"), thread)
    event("trigger_spawned", pid=None, thread=thread, author=seat_info["seat"], turn="free-inline")
    threading.Thread(target=_run_cli_inline_chat_turn_bg, args=(seat_info, thread, cwd), daemon=True).start()
    return True


# ---------------------------------------------------------------- decisiones

def fetch_open_decisions(conn) -> list[dict]:
    """Decisiones abiertas con sus posiciones, para que el motor diga qué
    turnos faltan. Una consulta barata: lo normal es cero o una abiertas."""
    rows = conn.execute(
        """
        SELECT id, title, artifact, protocol, status, round, thread, heads, anchor_id
        FROM decisions
        WHERE status = 'open'
        ORDER BY id
        """
    ).fetchall()
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    positions = conn.execute(
        """
        SELECT decision_id, head, round, position, conditions, message_id
        FROM positions
        WHERE decision_id = ANY(%s)
        ORDER BY decision_id, round, head
        """,
        (ids,),
    ).fetchall()
    by_decision: dict[int, list[dict]] = {}
    for p in positions:
        by_decision.setdefault(p["decision_id"], []).append(p)
    for r in rows:
        r["positions"] = by_decision.get(r["id"], [])
    return rows


def _persona_text(seat: str) -> str:
    if seat in personas.PERSONAS:
        return personas.system_prompt(seat)
    # asientos custom del registry sin persona dedicada: identidad genérica.
    return f"Sos el asiento '{seat}' del sistema MAGI."


def fire_decision_turns(conn, state: dict, d: dict) -> None:
    """Dispara los asientos que el motor dice que faltan en la ronda actual.

    Cada asiento tiene su candado (thread::seat): las cabezas de una misma
    decisión investigan y votan EN PARALELO."""
    ts = thread_state(state, d["thread"])
    if ts["triggers"] >= MAX_TRIGGERS_PER_DECISION:
        if not ts.get("capped_notified"):
            log.error(
                "decisión %s (%s) alcanzó el tope de %s disparos sin cerrar: corto el loop. "
                "Posteá un 'arbitraje' para cerrarla.",
                d["id"], d["thread"], MAX_TRIGGERS_PER_DECISION,
            )
            event("decision_capped", decision_id=d["id"], thread=d["thread"], triggers=ts["triggers"])
            ts["capped_notified"] = True
        return

    turns = decision.pending_turns(d, d["positions"])
    if not turns:
        return
    cwd = resolve_cwd(conn, d["thread"], ts)
    # since_id fijo en el anchor: la cabeza siempre lee el journal completo
    # desde el título (sabe lo que dijeron las otras en rondas previas).
    since_id = max((d.get("anchor_id") or 1) - 1, 0)
    # Memoria del consejo: una sola consulta al grafo por tanda de turnos,
    # misma para las tres cabezas (es contexto compartido, no una ventaja).
    # Si el grafo no existe o falla, memoria queda vacío: nada cambia.
    memoria = memory_ctx.memoria_para(d["title"], d.get("artifact"))

    for turn in turns:
        token = _token(d["thread"], turn["seat"])
        with _inflight_lock:
            busy = token in _inflight
            total = len(_inflight)
        if busy:
            log.info("turno de %s en %s ya está corriendo", turn["seat"], d["thread"])
            continue
        if total >= MAX_CONCURRENT_TRIGGERS:
            log.warning("techo de %s turnos concurrentes, encolo %s", MAX_CONCURRENT_TRIGGERS, turn["seat"])
            state["pending"].append({"id": state["last_id"], "thread": d["thread"], "author": turn["seat"]})
            continue
        seat_info = heads.seat_by_name(turn["seat"])
        if seat_info is None:
            log.error("asiento %s ya no está en el registry, no lo puedo disparar", turn["seat"])
            event("trigger_spawn_failed", error="asiento fuera del registry", thread=d["thread"], author=turn["seat"])
            continue
        if seat_info.get("journal") == "inline":
            # cabeza CLI sin MCP (codex exec): prompt con journal inlineado
            # por stdin y voto parseado del stdout.
            if fire_cli_inline_turn(seat_info, d, cwd, memoria):
                ts["triggers"] += 1
            continue
        if seat_info.get("type") == "api":
            if fire_api_turn(seat_info, d, memoria):
                ts["triggers"] += 1
            continue
        if not seat_info.get("bin"):
            log.error("asiento %s no tiene binario (decisión degradada), no lo puedo disparar", turn["seat"])
            event("trigger_spawn_failed", error="asiento sin binario",
                  thread=d["thread"], author=turn["seat"], decision_id=d["id"])
            continue
        prompt = decision.build_head_prompt(turn["seat"], _persona_text(turn["seat"]), d, since_id, memory=memoria)
        meta = {"decision_id": d["id"], "round": turn["round"], "turn": turn["kind"]}
        if trigger(turn["seat"], d["thread"], since_id, cwd, prompt, meta):
            ts["triggers"] += 1
        else:
            state["pending"].append({"id": state["last_id"], "thread": d["thread"], "author": turn["seat"]})


# ---------------------------------------------------------------- ciclo

def process_cycle(conn, state: dict) -> None:
    rows = conn.execute(
        """
        SELECT id, thread, author, kind FROM messages
        WHERE id > %s ORDER BY id
        """,
        (state["last_id"],),
    ).fetchall()

    # Un solo candidato por thread: el último mensaje. Los intermedios sólo
    # sirven para la contabilidad (resetear el cap con cada 'analisis').
    candidates: dict[str, dict] = {}
    for r in rows:
        ts = thread_state(state, r["thread"])
        if r["kind"] == "analisis":
            ts["triggers"] = 0
            ts["capped_notified"] = False
        candidates[r["thread"]] = {"id": r["id"], "thread": r["thread"], "author": r["author"], "kind": r["kind"]}
        state["last_id"] = r["id"]

    # Lo que quedó sin disparar en ciclos anteriores, si no lo pisó algo nuevo.
    for p in state["pending"]:
        candidates.setdefault(p["thread"], p)
    state["pending"] = []

    open_decisions = fetch_open_decisions(conn)
    journal_threads = {d["thread"] for d in open_decisions}

    # --- threads libres: la lógica clásica, ahora sobre los asientos del registry
    seats = heads.seat_names()
    for thread, cand in candidates.items():
        if thread in journal_threads:
            continue  # journal de una decisión abierta: lo maneja el motor abajo
        if _RE_DECISION_THREAD.match(thread):
            # Journal de una decisión ya cerrada (o de otra corrida): sus
            # mensajes de cierre son ruido para el flip de threads libres.
            # Sin este filtro, al cerrarse una decisión los 'posicion' del
            # journal disparaban al asiento siguiente sobre el journal ya
            # cerrado — lo vio la prueba manual del 2026-09-12: un trigger
            # 'free' spawneado a los 5s de cerrada la decisión.
            continue

        ts = thread_state(state, thread)

        # adrian gobierna pero no opina: sus mensajes no disparan respuesta,
        # SALVO que abra ronda explícitamente (kind='analisis') — es la
        # señal de "quiero que las cabezas empiecen/continúen con esto",
        # la que usa el chat de la UI. next_seat resuelve al primer asiento
        # para autores fuera del registry.
        if cand["author"] == "adrian" and cand.get("kind") != "analisis":
            log.info("mensaje de adrian en %s (id=%s), no disparo", thread, cand["id"])
            continue

        if thread_closed(conn, thread):
            log.info("thread %s cerrado tras id=%s, no disparo", thread, cand["id"])
            continue

        if ts["triggers"] >= MAX_TRIGGERS_PER_THREAD:
            if not ts.get("capped_notified"):
                log.error(
                    "thread %s alcanzó el tope de %s disparos sin cerrar: "
                    "corto el loop. Posteá un 'arbitraje' o un 'analisis' nuevo para reanudar.",
                    thread, MAX_TRIGGERS_PER_THREAD,
                )
                event("thread_capped", thread=thread, triggers=ts["triggers"])
                ts["capped_notified"] = True
            continue

        with _inflight_lock:
            busy = _token(thread) in _inflight
            total = len(_inflight)
        if busy:
            log.info("thread %s ya tiene un agente corriendo, encolo id=%s", thread, cand["id"])
            state["pending"].append(cand)
            continue
        if total >= MAX_CONCURRENT_TRIGGERS:
            log.warning("techo de %s disparos concurrentes, encolo %s", MAX_CONCURRENT_TRIGGERS, thread)
            state["pending"].append(cand)
            continue

        other = next_seat(cand["author"], seats)
        if other is None:
            log.error("thread %s sin asientos en el registry, no puedo disparar", thread)
            continue
        seat_info = heads.seat_by_name(other)
        if seat_info is not None and seat_info.get("journal") == "inline":
            # chat libre de una cabeza sin MCP: el stdout se postea como
            # respuesta (igual contrato que el turno API de chat).
            cwd = resolve_cwd(conn, thread, ts)
            if fire_cli_inline_chat_turn(seat_info, thread, cwd):
                ts["triggers"] += 1
            else:
                state["pending"].append(cand)
            continue
        if seat_info is not None and seat_info.get("type") == "api":
            # el round-robin también sirve para cabezas API (Ollama y
            # compatibles): sin esto, el chat se colgaba cada vez que tocaba
            # un asiento sin binario — exigía un CLI que no existe.
            if fire_api_chat_turn(seat_info, thread, cand["id"] - 1):
                ts["triggers"] += 1
            else:
                state["pending"].append(cand)
            continue
        cwd = resolve_cwd(conn, thread, ts)
        # since_id-1: read_thread devuelve id > since_id, y cand["id"] es
        # justo el mensaje que disparó este trigger
        prompt = build_prompt(thread, cand["id"] - 1, cand["author"], other)
        if trigger(other, thread, cand["id"] - 1, cwd, prompt, meta={"turn": "free"}):
            ts["triggers"] += 1
        else:
            state["pending"].append(cand)

    # --- decisiones MAGI: el motor dice qué turnos faltan; nosotros disparamos
    for d in open_decisions:
        fire_decision_turns(conn, state, d)

    save_state(state)


def main() -> None:
    state = load_state()
    log.info("relay arrancando, last_id=%s", state["last_id"])
    backoff = 1

    while True:
        try:
            with connect() as conn:
                conn.execute(f"LISTEN {CHANNEL_ALL}")
                conn.execute(f"LISTEN {CHANNEL_DECISIONS}")
                log.info("escuchando %s y %s (last_id=%s)", CHANNEL_ALL, CHANNEL_DECISIONS, state["last_id"])
                backoff = 1
                while True:
                    process_cycle(conn, state)
                    write_heartbeat(state, pg_ok=True)
                    # bloquea hasta que entre un notify o venza el wake ocioso.
                    # Con pendientes o agentes en vuelo el wake es corto: el
                    # candado in-flight se libera en otro hilo y el turno
                    # encolado se reintenta en segundos, no en IDLE_WAKE_SECS
                    # (con agentes instantáneos el notify del mensaje de A
                    # llega mientras A todavía figura corriendo: su turno
                    # queda en pending y sin esto el debate se congelaba
                    # hasta el wake ocioso). Ocioso de verdad = wake largo.
                    idle = not state["pending"] and not _inflight
                    wake = 2 if not idle else IDLE_WAKE_SECS
                    for _notify in conn.notifies(timeout=wake, stop_after=1):
                        break
        except Exception:
            log.exception("relay: ciclo caído, reintento en %ss", backoff)
            try:
                write_heartbeat(state, pg_ok=False)
            except OSError:
                pass
            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECS)


if __name__ == "__main__":
    main()
