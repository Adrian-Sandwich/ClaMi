"""Servidor MCP "debate": tablero de discusión persistente sobre Postgres.

Dos agentes CLI (Kimi y Claude Code) opinan como analistas sobre los artefactos
del proyecto de trading. La persistencia y el fan-out en tiempo real los da
Postgres: cada INSERT dispara pg_notify('debate_<thread>', id), así que
`wait_messages` puede hacer LISTEN y despertar apenas llega un mensaje nuevo.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server import MCPServer  # noqa: E402
from psycopg import sql  # noqa: E402

from config import connect  # noqa: E402

AUTHORS = {"kimi", "claude", "adrian"}
KINDS = {"analisis", "critica", "respuesta", "veredicto", "arbitraje"}

# tope del long-poll: no tiene sentido esperar más que esto en una sola llamada
MAX_WAIT_SECS = 300

# Postgres trunca identifiers y channel names de NOTIFY a 63 bytes (NAMEDATALEN-1).
# Un thread que empuje "debate_<thread>" sobre ese límite hace fallar el pg_notify
# del trigger (y con él el INSERT completo) con "channel name too long". Cortamos
# antes, con un mensaje claro, en vez de dejar que reviente adentro del trigger.
_CHANNEL_PREFIX = "debate_"
MAX_THREAD_LEN = 63 - len(_CHANNEL_PREFIX.encode())

mcp = MCPServer("debate")


def _validate_thread(thread: str) -> None:
    n = len(thread.encode())
    if n > MAX_THREAD_LEN:
        raise ValueError(
            f"thread {thread!r} tiene {n} bytes, máximo {MAX_THREAD_LEN} "
            f"(el canal '{_CHANNEL_PREFIX}{thread}' se trunca en Postgres a 63 bytes)"
        )


@mcp.tool()
def list_threads() -> list[dict]:
    """Lista los threads con cantidad de mensajes y timestamp del último."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT thread, count(*) AS n_messages, max(created_at) AS last_at
            FROM messages
            GROUP BY thread
            ORDER BY last_at DESC
            """
        ).fetchall()
    return [
        {"thread": r["thread"], "n_messages": r["n_messages"], "last_at": r["last_at"].isoformat()}
        for r in rows
    ]


@mcp.tool()
def read_thread(thread: str, since_id: int = 0, limit: int = 50) -> dict:
    """Lee mensajes de un thread con id > since_id, ordenados por id.

    Protocolo del debate: roles kimi/claude (analistas) y adrian (árbitro
    humano). kinds en orden — analisis (apertura) -> critica -> respuesta ->
    veredicto (cierre de cada analista); arbitraje solo lo postea adrian si
    hay desacuerdo tras los veredictos.

    Devuelve {"messages": [...], "has_more": bool, "max_id": int | None}.
    Si has_more es true, llamá de nuevo con since_id=messages[-1]["id"] —
    puede haber más mensajes de los que entraron en `limit`.
    """
    # El LEFT JOIN LATERAL trae la página y el max_id del thread en un solo
    # roundtrip. Es LEFT y no un CTE con join normal a propósito: cuando no
    # hay mensajes nuevos igual queremos devolver el max_id real del thread,
    # y un join interno no devolvería ninguna fila.
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT m.max_id, t.id, t.thread, t.author, t.kind, t.body,
                   t.artifact, t.created_at
            FROM (SELECT max(id) AS max_id FROM messages WHERE thread = %s) m
            LEFT JOIN LATERAL (
                SELECT id, thread, author, kind, body, artifact, created_at
                FROM messages
                WHERE thread = %s AND id > %s
                ORDER BY id
                LIMIT %s
            ) t ON true
            ORDER BY t.id
            """,
            (thread, thread, since_id, limit + 1),
        ).fetchall()

    max_id = rows[0]["max_id"] if rows else None
    found = [r for r in rows if r["id"] is not None]
    has_more = len(found) > limit
    messages = [_serialize(r) for r in found[:limit]]
    return {"messages": messages, "has_more": has_more, "max_id": max_id}


@mcp.tool()
def post_message(
    thread: str, author: str, kind: str, body: str, artifact: str | None = None
) -> dict:
    """Publica un mensaje en el thread y devuelve su id.

    El trigger de la tabla se encarga del NOTIFY a los listeners.

    artifact es una referencia opcional al archivo/artefacto sobre el que
    opina el mensaje (ej. "src/paper.rs:120" o "experiments/plan.md") — no
    el contenido en sí, eso va en body.
    """
    if author not in AUTHORS:
        raise ValueError(f"author inválido: {author!r} (válidos: {sorted(AUTHORS)})")
    if kind not in KINDS:
        raise ValueError(f"kind inválido: {kind!r} (válidos: {sorted(KINDS)})")
    _validate_thread(thread)
    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO messages (thread, author, kind, body, artifact)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (thread, author, kind, body, artifact),
        ).fetchone()
    return {"id": row["id"]}


@mcp.tool()
def wait_messages(thread: str, since_id: int, timeout_secs: int = 60) -> list[dict]:
    """Espera mensajes nuevos (id > since_id) hasta timeout_secs.

    Long-poll vía LISTEN/NOTIFY: se bloquea hasta que llega un INSERT al
    thread o vence el timeout (capeado a 300s). Devuelve lista vacía si no
    llegó nada.
    """
    _validate_thread(thread)
    timeout_secs = max(1, min(int(timeout_secs), MAX_WAIT_SECS))
    with connect() as conn:
        conn.execute(
            sql.SQL("LISTEN {}").format(sql.Identifier(f"{_CHANNEL_PREFIX}{thread}"))
        )
        # drenar lo que ya exista antes de bloquear (evita perder mensajes
        # que entraron entre el read_thread anterior y el LISTEN)
        pending = _new_messages(conn, thread, since_id)
        if pending:
            return pending
        for _notify in conn.notifies(timeout=timeout_secs, stop_after=1):
            break
        return _new_messages(conn, thread, since_id)


def _new_messages(conn: psycopg.Connection, thread: str, since_id: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, thread, author, kind, body, artifact, created_at
        FROM messages
        WHERE thread = %s AND id > %s
        ORDER BY id
        """,
        (thread, since_id),
    ).fetchall()
    return [_serialize(r) for r in rows]


def _serialize(r: dict) -> dict:
    return {
        "id": r["id"],
        "thread": r["thread"],
        "author": r["author"],
        "kind": r["kind"],
        "body": r["body"],
        "artifact": r["artifact"],
        "created_at": r["created_at"].isoformat(),
    }


if __name__ == "__main__":
    mcp.run()
