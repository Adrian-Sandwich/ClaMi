"""Escrituras sobre el tablero que no son tools MCP.

record_position concentra la lógica del voto de cast_position (row lock,
dedup por ronda, insert del message + position, advance del motor, cierre con
resultado) y start_decision la apertura de una decisión. Las usan tanto los
tools MCP (server.py) como la UI (magi_ui.py) y los turnos de asientos API:
una decisión es una decisión venga de donde venga.
"""

import uuid

from psycopg.types.json import Json

import decision
import heads


def start_decision(
    conn,
    title: str,
    artifact: str | None = None,
    protocol: str = "vote",
    created_by: str = "adrian",
    seats: list[str] | None = None,
) -> dict:
    """Abre una decisión MAGI. El llamador maneja la transacción."""
    if created_by != "adrian":
        raise ValueError("solo adrian abre decisiones")
    if protocol not in decision.PROTOCOLS:
        raise ValueError(f"protocol inválido: {protocol!r} (válidos: {list(decision.PROTOCOLS)})")
    registry = heads.load()
    known = heads.seat_names(registry)
    participating = list(seats) if seats is not None else heads.seat_names(heads.active_seats(registry))
    unknown = [s for s in participating if s not in known]
    if unknown:
        raise ValueError(f"asientos desconocidos: {unknown} (válidos: {known})")
    if not participating:
        raise ValueError("no hay asientos para decidir: configurá heads.json o DEBATE_HEADS")
    live = set(heads.seat_names(heads.active_seats(registry)))
    degraded = [s for s in participating if s not in live]

    # thread provisional único: la columna es UNIQUE y el nombre final
    # (d<id>) recién se conoce tras el INSERT.
    provisional = f"_opening-{uuid.uuid4().hex[:12]}"
    row = conn.execute(
        """
        INSERT INTO decisions (title, artifact, protocol, thread, heads, created_by)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (title, artifact, protocol, provisional, Json(participating), created_by),
    ).fetchone()
    did = row["id"]
    thread = f"d{did}"
    msg = conn.execute(
        """
        INSERT INTO messages (thread, author, kind, body, artifact)
        VALUES (%s, %s, 'analisis', %s, %s)
        RETURNING id
        """,
        (thread, created_by, title, artifact),
    ).fetchone()
    conn.execute(
        "UPDATE decisions SET thread = %s, anchor_id = %s WHERE id = %s",
        (thread, msg["id"], did),
    )
    return {"decision_id": did, "thread": thread, "seats": participating, "degraded": degraded}


def record_position(
    conn,
    decision_id: int,
    author: str,
    position: str,
    body: str,
    conditions: list[str] | None = None,
) -> tuple[dict, int]:
    """Registra el voto de una cabeza y aplica la orden del motor.

    Devuelve (orden, message_id). El llamador maneja la transacción y las
    validaciones de author/position (necesitan el registry); acá se vuelve a
    chequear estado y dedup porque entre la validación y el INSERT puede
    haber pasado otra cabeza.
    """
    d = conn.execute(
        "SELECT * FROM decisions WHERE id = %s FOR UPDATE", (decision_id,)
    ).fetchone()
    if d is None:
        raise ValueError(f"decisión {decision_id} no existe")
    if d["status"] != "open":
        raise ValueError(f"decisión {decision_id} está '{d['status']}'")
    ya = conn.execute(
        """
        SELECT 1 FROM positions
        WHERE decision_id = %s AND head = %s AND round = %s
        """,
        (decision_id, author, d["round"]),
    ).fetchone()
    if ya:
        raise ValueError(f"{author} ya votó en la ronda {d['round']}")

    msg = conn.execute(
        """
        INSERT INTO messages (thread, author, kind, body, artifact)
        VALUES (%s, %s, 'posicion', %s, NULL)
        RETURNING id
        """,
        (d["thread"], author, body),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO positions (decision_id, head, round, position, conditions, message_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (decision_id, author, d["round"], position,
         Json(conditions) if conditions else None, msg["id"]),
    )
    positions = conn.execute(
        """
        SELECT head, round, position, conditions
        FROM positions WHERE decision_id = %s
        """,
        (decision_id,),
    ).fetchall()
    act = decision.advance(d, positions)
    if act["action"] == "close":
        act["mind_changes"] = decision.mind_changes(positions)
        conn.execute(
            """
            UPDATE decisions
            SET status = 'closed', ruling = %s, confidence = %s,
                minority_report = %s, closed_at = now()
            WHERE id = %s
            """,
            (act["ruling"], act["confidence"],
             Json({
                 "minority": act["minority"],
                 "degraded": act.get("degraded", False),
                 "mind_changes": act["mind_changes"],
             }),
             decision_id),
        )
    elif act["action"] == "next_round":
        conn.execute(
            "UPDATE decisions SET round = %s WHERE id = %s",
            (d["round"] + 1, decision_id),
        )
    elif act["action"] == "split":
        conn.execute(
            """
            UPDATE decisions SET status = 'split', minority_report = %s
            WHERE id = %s
            """,
            (Json({"minority": act["minority"]}), decision_id),
        )
    if act["action"] in ("close", "split"):
        conn.execute(
            """
            INSERT INTO messages (thread, author, kind, body, artifact)
            VALUES (%s, 'magi', 'resultado', %s, NULL)
            """,
            (d["thread"], decision.resultado_text(d, act)),
        )
    return act, msg["id"]


def human_message(conn, thread: str, body: str) -> dict:
    """Mensaje del operador humano desde la UI: un solo campo de texto, y el
    kind lo decide el estado del thread — cero protocolo que memorizar.

    - decisión 'split'   → 'arbitraje': cierra la decisión, el ruling humano
      queda en el body (misma lógica que el tool post_message);
    - decisión 'open'    → 'contexto': las cabezas lo leen en el journal de
      su próximo turno (prompt desde el anchor, lo ven completo);
    - thread libre       → 'analisis': abre ronda y el relay dispara a la
      primera cabeza (los mensajes de adrian con otros kinds no disparan).
    """
    d = conn.execute(
        "SELECT id, status FROM decisions WHERE thread = %s", (thread,)
    ).fetchone()
    if d is not None and d["status"] == "split":
        kind = "arbitraje"
    elif d is not None:
        kind = "contexto"
    else:
        kind = "analisis"
    row = conn.execute(
        """
        INSERT INTO messages (thread, author, kind, body, artifact)
        VALUES (%s, 'adrian', %s, %s, NULL)
        RETURNING id
        """,
        (thread, kind, body),
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
    return {"id": row["id"], "kind": kind, "arbitrated_decision": arbitrated}
