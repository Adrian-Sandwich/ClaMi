"""Nodos debate_thread y decision desde la base `debate` de Postgres.

- Threads: un nodo por thread de messages, con conteos y autores en props.
  Los edges de sesiones (posted_to) NO se crean acá: los crean
  ingest_claude/ingest_kimi referenciando debate_thread:<name> por natural
  key (no importa el orden de ingestión, finalize() en export_kgraph.py tira
  los edges que queden colgando).
- Decisiones: un nodo por fila de decisions (dominio `decision`) con
  ruling/confidence/minority/mind_changes en props, y edge journal_of desde
  el debate_thread de su journal. Así el grafo sabe no sólo QUÉ se debatió
  sino qué se resolvió, con qué confianza y quién discrepó.
"""

from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

import db
import settings

CONNINFO = settings.CONNINFO
SOURCE = "ingest_debate"


def node_id(thread: str) -> str:
    return f"debate_thread:{thread}"


def decision_node_id(decision_id) -> str:
    return f"decision:{decision_id}"


def _iso(ts) -> str | None:
    return ts.isoformat() if ts else None


def write_decision(conn, r: dict, now: str) -> None:
    """Una decisión como nodo del grafo + edge journal_of desde su thread.

    Es función propia (y no inline en main) porque es la parte testeable sin
    Postgres: recibe una fila ya leída.
    """
    minority = r["minority_report"] or {}
    props = {
        "title": r["title"],
        "protocol": r["protocol"],
        "status": r["status"],
        "ruling": r["ruling"],
        "confidence": r["confidence"],
        "round": r["round"],
        "thread": r["thread"],
        "created_by": r["created_by"],
        "minority": minority.get("minority"),
        "mind_changes": minority.get("mind_changes") or [],
        "degraded": minority.get("degraded", False),
        "first_at": _iso(r["created_at"]),
        "last_at": _iso(r["closed_at"] or r["created_at"]),
    }
    db.upsert_node(
        conn,
        id=decision_node_id(r["id"]),
        domain="decision",
        source=SOURCE,
        updated_at=now,
        label=f"#{r['id']} {r['title']}",
        tag="Decision",
        size=10,
        tooltip=f"{r['status']} · ruling={r['ruling']} · {r['protocol']} · ronda {r['round']}",
        props=props,
    )
    db.upsert_edge(
        conn,
        node_id(r["thread"]),
        decision_node_id(r["id"]),
        "journal_of",
        source=SOURCE,
        updated_at=now,
        label_forward="journal de",
    )


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = db.connect()
    # connect_timeout acotado: sin él, un Postgres caído cuelga refresh.sh
    # ~2 minutos por corrida (mismo criterio que debate-mcp/config.py).
    with psycopg.connect(CONNINFO, row_factory=dict_row, connect_timeout=10) as pg:
        rows = pg.execute(
            """
            WITH kc AS (
                SELECT thread, kind, count(*) AS kind_count
                FROM messages GROUP BY thread, kind
            )
            SELECT m.thread,
                   count(*) AS n_messages,
                   min(m.created_at) AS first_at,
                   max(m.created_at) AS last_at,
                   array_agg(DISTINCT m.author) AS authors,
                   (SELECT json_object_agg(kind, kind_count) FROM kc WHERE kc.thread = m.thread) AS kind_counts
            FROM messages m
            GROUP BY m.thread
            """
        ).fetchall()
        decisions = pg.execute(
            """
            SELECT id, title, protocol, status, ruling, confidence,
                   minority_report, thread, round, created_by,
                   created_at, closed_at
            FROM decisions
            ORDER BY id
            """
        ).fetchall()

    seen: set[str] = set()
    n = 0
    for r in rows:
        seen.add(node_id(r["thread"]))
        db.upsert_node(
            conn,
            id=node_id(r["thread"]),
            domain="debate_thread",
            source=SOURCE,
            updated_at=now,
            label=r["thread"],
            tag="DebateThread",
            size=min(30, max(8, 6 + r["n_messages"])),
            tooltip=f"{r['n_messages']} mensajes, {r['first_at'].isoformat()} - {r['last_at'].isoformat()}",
            props={
                "n_messages": r["n_messages"],
                "authors": r["authors"],
                "kind_counts": r["kind_counts"],
                "first_at": r["first_at"].isoformat(),
                "last_at": r["last_at"].isoformat(),
            },
        )
        n += 1

    seen_decisions: set[str] = set()
    for r in decisions:
        seen_decisions.add(decision_node_id(r["id"]))
        write_decision(conn, r, now)

    n_swept = db.sweep_domain(conn, "debate_thread", seen)
    n_swept_decisions = db.sweep_domain(conn, "decision", seen_decisions)
    conn.commit()
    conn.close()
    print(
        f"[ingest_debate] {n} threads ({n_swept} borrados), "
        f"{len(decisions)} decisiones ({n_swept_decisions} borradas)"
    )


if __name__ == "__main__":
    main()
