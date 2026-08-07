#!/usr/bin/env python3
"""Daemon relay: cierra el loop de comunicación entre Kimi y Claude en el
tablero 'debate'. Antes, alguien tenía que disparar a mano un `claude -p` o
`kimi -p` por cada ronda. Este proceso hace polling de la tabla `messages` y,
cuando un autor postea, dispara automáticamente al otro para que responda.

Regla de cierre: si los dos últimos mensajes de un thread son 'veredicto' de
autores distintos, o el último es 'arbitraje', el thread queda cerrado (no se
dispara más) hasta que aparezca un nuevo 'analisis'.
"""

import json
import logging
import subprocess
import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

CONNINFO = "dbname=trade_debate user=adrianmedina host=localhost"
POLL_SECS = 20
STATE_PATH = Path(__file__).parent / "relay_state.json"
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

CLAUDE_BIN = "/Users/adrianmedina/.local/bin/claude"
KIMI_BIN = "/Users/adrianmedina/.kimi-code/bin/kimi"

# cwd por thread; default cubre el caso más común (debate sobre el proyecto trade)
DEFAULT_CWD = "/Users/adrianmedina/src/trade"
THREAD_CWD = {}

PROTOCOL = """Roles: kimi y claude (analistas), adrian (arbitro humano).
Kinds: analisis (apertura), critica, respuesta, veredicto (cierre de cada
analista), arbitraje (solo adrian). Regla de 3 rounds: analisis -> criticas
cruzadas -> respuestas/veredicto. Si hay desacuerdo tras el veredicto, adrian
arbitra."""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "relay.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("relay")


def connect():
    return psycopg.connect(CONNINFO, autocommit=True, row_factory=dict_row)


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"last_id": 0}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state))


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


def build_prompt(thread: str, since_id: int, other_author: str, author_to_call: str) -> str:
    return (
        f"Continua el debate en el tablero MCP 'debate', thread '{thread}'. "
        f"Protocolo:\n{PROTOCOL}\n\n"
        f"Usa read_thread(thread='{thread}', since_id={since_id}) para ver los "
        f"mensajes nuevos de '{other_author}'. Respondé como '{author_to_call}' "
        f"con post_message(thread='{thread}', author='{author_to_call}', kind=..., body=...) "
        f"usando el kind que corresponda según el protocolo (critica, "
        f"respuesta o veredicto). No uses wait_messages, no hace falta: este "
        f"disparo es automático. Al terminar decime solo el id del mensaje que "
        f"posteaste."
    )


def trigger(author_to_call: str, thread: str, since_id: int, other_author: str, cwd: str):
    prompt = build_prompt(thread, since_id, other_author, author_to_call)
    ts = time.strftime("%Y%m%dT%H%M%S")
    out_path = LOG_DIR / f"{thread}_{author_to_call}_{ts}.log"

    if author_to_call == "claude":
        cmd = [
            CLAUDE_BIN, "-p", prompt,
            "--allowedTools", "mcp__debate__*", "Read", "Grep", "Glob",
        ]
    else:
        # kimi -p ya corre no-interactivo por su cuenta; --auto/--yolo son
        # incompatibles con --prompt (kimi rechaza el combo con error)
        cmd = [KIMI_BIN, "-p", prompt]

    log.info("disparo %s en thread=%s cwd=%s -> %s", author_to_call, thread, cwd, out_path)
    with open(out_path, "w") as f:
        subprocess.Popen(cmd, cwd=cwd, stdout=f, stderr=subprocess.STDOUT)


def main():
    state = load_state()
    log.info("relay arrancando, last_id=%s", state["last_id"])

    while True:
        try:
            with connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, thread, author, kind FROM messages
                    WHERE id > %s ORDER BY id
                    """,
                    (state["last_id"],),
                ).fetchall()

                for r in rows:
                    thread, author, kind = r["thread"], r["author"], r["kind"]
                    state["last_id"] = r["id"]

                    if author == "adrian":
                        log.info("arbitraje/nota de adrian en %s (id=%s), no disparo nada", thread, r["id"])
                        save_state(state)
                        continue

                    if thread_closed(conn, thread):
                        log.info("thread %s cerrado tras id=%s, no disparo", thread, r["id"])
                        save_state(state)
                        continue

                    other = "claude" if author == "kimi" else "kimi"
                    cwd = THREAD_CWD.get(thread, DEFAULT_CWD)
                    # since_id-1: read_thread devuelve id > since_id, y r["id"]
                    # es justo el mensaje que disparó este trigger
                    trigger(other, thread, r["id"] - 1, author, cwd)
                    save_state(state)
        except Exception:
            log.exception("error en ciclo de polling")

        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
