"""Servidor MCP "debate": tablero de discusión persistente sobre Postgres +
motor de decisiones MAGI.

Dos capas sobre la misma base:

1. Primitivas del tablero (list_threads/read_thread/post_message/
   wait_messages): un thread es un journal auditable. Cada INSERT dispara
   pg_notify('debate_<thread>', id), así que wait_messages despierta por
   LISTEN/NOTIFY sin polling.

2. Decisiones MAGI (start_decision/cast_position/get_decision): la unidad de
   trabajo es una Decisión — pregunta/artefacto sometido a las cabezas
   (registry en heads.json, personas en personas.py, motor puro en
   decision.py). Las cabezas debaten en el journal como messages normales
   (kind='posicion') y votan posiciones estructuradas: el voto es dato, el
   razonamiento es historia. El cierre lo decide el motor: mayoría 2/3,
   minority report, y si no hay acuerdo, arbitraje humano — el sistema no
   inventa consenso.
"""

import uuid

import psycopg
from psycopg import sql
from psycopg.types.json import Json
from mcp.server import MCPServer

import decision
import heads
import board

from config import connect

KINDS = {"analisis", "critica", "respuesta", "veredicto", "arbitraje", "posicion", "resultado", "contexto", "consulta"}

# tope del long-poll: no tiene sentido esperar más que esto en una sola llamada
MAX_WAIT_SECS = 300

# Postgres trunca identifiers y channel names de NOTIFY a 63 bytes (NAMEDATALEN-1).
# Un thread que empuje "debate_<thread>" sobre ese límite hace fallar el pg_notify
# del trigger (y con él el INSERT completo) con "channel name too long". Cortamos
# antes, con un mensaje claro, en vez de dejar que reviente adentro del trigger.
_CHANNEL_PREFIX = "debate_"
MAX_THREAD_LEN = 63 - len(_CHANNEL_PREFIX.encode())

mcp = MCPServer("debate")


def _authors() -> set[str]:
    """Autores válidos: los asientos del registry + adrian (superusuario
    humano: abre decisiones y arbitra). Se recalcula por llamada: el registry
    puede cambiar sin reiniciar el servidor."""
    return set(heads.seat_names()) | {"adrian"}


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

    Protocolo del debate: los analistas son los asientos del registry (ver
    heads.json / personas.py); adrian es el árbitro humano. Kinds:
    analisis (apertura), critica/respuesta (debate libre), veredicto,
    posicion (voto de una cabeza en una decisión), resultado (cierre de una
    decisión), arbitraje (solo adrian, cierra una decisión split).

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

    kind='arbitraje' (solo adrian) además cierra la decisión cuyo journal es
    este thread, si está 'split': el ruling humano queda en el body.
    """
    if author not in _authors():
        raise ValueError(f"author inválido: {author!r} (válidos: {sorted(_authors())})")
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
        arbitrated = None
        if kind == "arbitraje":
            closed = conn.execute(
                """
                UPDATE decisions SET status = 'closed', closed_at = now()
                WHERE thread = %s AND status = 'split'
                RETURNING id
                """,
                (thread,),
            ).fetchone()
            arbitrated = closed["id"] if closed else None
    return {"id": row["id"], "arbitrated_decision": arbitrated}


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


@mcp.tool()
def start_decision(
    title: str,
    artifact: str | None = None,
    protocol: str = "vote",
    created_by: str = "adrian",
    seats: list[str] | None = None,
) -> dict:
    """Abre una decisión MAGI: las cabezas la investigan, debaten en el
    journal y votan hasta ruling o arbitraje. Solo adrian abre decisiones.

    protocol: 'vote' (una ronda; split → arbitraje), 'critique' (rondas de
    crítica mutua hasta acuerdo o tope), 'adaptive' (vota; split 3-vías →
    critique automático).

    seats: asientos participantes (default: los activos del registry). Si un
    asiento elegido no tiene binario, la decisión corre degradada y queda
    anotado en 'degraded'.

    El INSERT dispara NOTIFY al relay, que dispara a las cabezas faltantes.
    """
    with connect() as conn:
        with conn.transaction():
            return board.start_decision(
                conn, title, artifact=artifact, protocol=protocol,
                created_by=created_by, seats=seats,
            )


@mcp.tool()
def cast_position(
    decision_id: int,
    author: str,
    position: str,
    body: str,
    conditions: list[str] | None = None,
) -> dict:
    """Voto estructurado de una cabeza: publica el razonamiento en el journal
    (kind='posicion') y registra la posición (yes/no/conditional/info) con
    sus condiciones. El voto es dato; el razonamiento es historia.

    Cuando la ronda queda completa, el motor cierra la decisión por mayoría,
    abre la ronda de crítica siguiente, o la declara split (a arbitraje de
    adrian). El UPDATE dispara NOTIFY y el relay redispára a las cabezas.
    """
    if author not in heads.seat_names():
        raise ValueError(f"author inválido: {author!r} (asientos: {heads.seat_names()})")
    if position not in decision.POSITIONS:
        raise ValueError(f"position inválida: {position!r} (válidas: {list(decision.POSITIONS)})")
    with connect() as conn:
        with conn.transaction():
            act, message_id = board.record_position(
                conn, decision_id, author, position, body, conditions
            )
    return {"message_id": message_id, "action": act["action"]}


@mcp.tool()
def get_decision(decision_id: int) -> dict:
    """El dossier de una decisión: estado, ronda, ruling, confidence,
    minority report y todas las posiciones por ronda."""
    with connect() as conn:
        d = conn.execute(
            "SELECT * FROM decisions WHERE id = %s", (decision_id,)
        ).fetchone()
        if d is None:
            raise ValueError(f"decisión {decision_id} no existe")
        positions = conn.execute(
            """
            SELECT head, round, position, conditions, message_id, created_at
            FROM positions WHERE decision_id = %s
            ORDER BY round, head
            """,
            (decision_id,),
        ).fetchall()
    out = dict(d)
    out["heads"] = list(d["heads"])
    for k in ("created_at", "closed_at"):
        if out.get(k):
            out[k] = out[k].isoformat()
    out["positions"] = [
        {
            **p,
            "conditions": list(p["conditions"]) if p["conditions"] else None,
            "created_at": p["created_at"].isoformat(),
        }
        for p in (dict(r) for r in positions)
    ]
    out["mind_changes"] = decision.mind_changes(out["positions"])
    return out


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
