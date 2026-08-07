"""Servidor MCP "debate": tablero de discusión persistente sobre Postgres.

Dos agentes CLI (Kimi y Claude Code) opinan como analistas sobre los artefactos
del proyecto de trading. La persistencia y el fan-out en tiempo real los da
Postgres: cada INSERT dispara pg_notify('debate_<thread>', id), así que
`wait_messages` puede hacer LISTEN y despertar apenas llega un mensaje nuevo.
"""

import psycopg
from mcp.server import MCPServer
from psycopg import sql
from psycopg.rows import dict_row

CONNINFO = "dbname=trade_debate user=adrianmedina host=localhost"

AUTHORS = {"kimi", "claude", "adrian"}
KINDS = {"analisis", "critica", "respuesta", "veredicto", "arbitraje"}

# tope del long-poll: no tiene sentido esperar más que esto en una sola llamada
MAX_WAIT_SECS = 300

mcp = MCPServer("debate")


def connect() -> psycopg.Connection:
    # autocommit: LISTEN no puede ir dentro de una transacción
    return psycopg.connect(CONNINFO, autocommit=True, row_factory=dict_row)


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
def read_thread(thread: str, since_id: int = 0, limit: int = 50) -> list[dict]:
    """Lee mensajes de un thread con id > since_id, ordenados por id."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, thread, author, kind, body, artifact, created_at
            FROM messages
            WHERE thread = %s AND id > %s
            ORDER BY id
            LIMIT %s
            """,
            (thread, since_id, limit),
        ).fetchall()
    return [_serialize(r) for r in rows]


@mcp.tool()
def post_message(
    thread: str, author: str, kind: str, body: str, artifact: str | None = None
) -> dict:
    """Publica un mensaje en el thread y devuelve su id.

    El trigger de la tabla se encarga del NOTIFY a los listeners.
    """
    if author not in AUTHORS:
        raise ValueError(f"author inválido: {author!r} (válidos: {sorted(AUTHORS)})")
    if kind not in KINDS:
        raise ValueError(f"kind inválido: {kind!r} (válidos: {sorted(KINDS)})")
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
    timeout_secs = max(1, min(int(timeout_secs), MAX_WAIT_SECS))
    with connect() as conn:
        conn.execute(
            sql.SQL("LISTEN {}").format(sql.Identifier(f"debate_{thread}"))
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
