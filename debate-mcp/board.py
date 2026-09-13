"""Escrituras sobre el tablero que no son tools MCP.

record_position concentra la lógica del voto de cast_position (row lock,
dedup por ronda, insert del message + position, advance del motor, cierre con
resultado) y start_decision la apertura de una decisión. Las usan tanto los
tools MCP (server.py) como la UI (magi_ui.py) y los turnos de asientos API:
una decisión es una decisión venga de donde venga.
"""

import re
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
    production: bool = False,
) -> dict:
    """Abre una decisión MAGI. El llamador maneja la transacción.

    production=true la marca como decisión de plan con ejecución: si el
    consejo la aprueba (ruling yes/conditional), pasa a 'executing' y el
    relay lanza al ejecutor (modo producción) en vez de cerrarla.
    """
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
        INSERT INTO decisions (title, artifact, protocol, thread, heads, created_by, production)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (title, artifact, protocol, provisional, Json(participating), created_by, production),
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
        if decision.debe_ejecutar(d, act):
            # modo producción: no es un cierre, es el pase a ejecución. El
            # relay detecta 'executing', lanza al ejecutor en la rama
            # magi/d<id> y al terminar abre la revisión del diff.
            conn.execute(
                """
                UPDATE decisions
                SET status = 'executing', ruling = %s, confidence = %s,
                    minority_report = %s
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
        else:
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
        if decision.debe_ejecutar(d, act):
            body = decision.resultado_ejecucion_texto(d, act)
        else:
            body = decision.resultado_text(d, act)
        conn.execute(
            """
            INSERT INTO messages (thread, author, kind, body, artifact)
            VALUES (%s, 'magi', 'resultado', %s, NULL)
            """,
            (d["thread"], body),
        )
    if act["action"] == "split":
        # Destrabe: el split no es un callejón sin salida. El consejo le
        # pide al operador una decisión concreta, con las dos vías: ruling
        # humano (arbitraje) o "seguí" para otra ronda con su contexto
        # (human_message reabre la decisión). Sin esto, el stalemate era
        # invisible hasta que el operador se diera cuenta solo.
        conn.execute(
            """
            INSERT INTO messages (thread, author, kind, body, artifact)
            VALUES (%s, 'magi', 'consulta', %s, NULL)
            """,
            (d["thread"], consulta_destrabe_texto(d["round"], act["minority"])),
        )
    return act, msg["id"]


# Palabras que reabren una decisión en STALEMATE en vez de arbitrarla.
# El resto del texto va como contexto para la ronda nueva.
_RE_SEGUI = re.compile(r"^\s*(segu[ií]|continu[aá]|seguimos|retry|reintent[aá]|otra ronda)\b", re.IGNORECASE)


def consulta_destrabe_texto(round_: int, posiciones: list[dict]) -> str:
    """El mensaje que le pide al operador una decisión concreta cuando el
    consejo no se puso de acuerdo: qué dijo cada cabeza y las dos vías."""
    pos = "; ".join(
        f"{m['head']}={m['position']}"
        + (f" ({', '.join(m['conditions'])})" if m.get("conditions") else "")
        for m in posiciones
    )
    return (
        f"CONSULTA AL OPERADOR — el consejo no se puso de acuerdo tras "
        f"{round_} ronda(s). Posiciones: {pos}.\n"
        f"Respondé con tu ruling y justificación para cerrar la decisión, "
        f"o escribí 'seguí' (opcionalmente con contexto nuevo) para abrir "
        f"otra ronda: las cabezas recastan teniéndolo en cuenta."
    )


def human_message(conn, thread: str, body: str) -> dict:
    """Mensaje del operador humano desde la UI: un solo campo de texto, y el
    kind lo decide el estado del thread — cero protocolo que memorizar.

    - decisión 'split'   → dos vías: si el texto empieza con "seguí"/"retry",
      REABRE la decisión (ronda siguiente, tu texto va como contexto y las
      cabezas recastan); si no, es 'arbitraje': cierra con tu ruling;
    - decisión 'open'    → 'contexto': las cabezas lo leen en el journal de
      su próximo turno;
    - thread libre       → 'analisis': abre ronda y el relay dispara a la
      primera cabeza (los mensajes de adrian con otros kinds no disparan).
    """
    d = conn.execute(
        "SELECT id, status, round FROM decisions WHERE thread = %s", (thread,)
    ).fetchone()
    if d is not None and d["status"] == "split" and not _RE_SEGUI.match(body):
        kind = "arbitraje"
    elif d is not None and d["status"] == "split":
        kind = "contexto"
    elif d is not None and d["status"] == "executing":
        # reintento de ejecución: "seguí" borra la marca de fallo y el
        # relay vuelve a lanzar al ejecutor en su próximo ciclo.
        kind = "contexto"
        conn.execute(
            """
            DELETE FROM messages
            WHERE thread = %s AND kind = 'resultado' AND body LIKE 'EJECUCIÓN FALLIDA%%'
            """,
            (thread,),
        )
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
    reopened = None
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
    elif kind == "contexto" and d is not None and d["status"] == "split":
        # destrabe: nueva ronda, las cabezas recastan con el contexto nuevo
        conn.execute(
            "UPDATE decisions SET status = 'open', round = %s WHERE id = %s",
            (d["round"] + 1, d["id"]),
        )
        reopened = d["id"]
    return {
        "id": row["id"], "kind": kind,
        "arbitrated_decision": arbitrated, "reopened_decision": reopened,
    }
