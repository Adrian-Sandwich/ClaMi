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
import hashlib
import json

import psycopg
from psycopg.rows import dict_row

import db
import settings
from explicit_memory import extract

CONNINFO = settings.CONNINFO
SOURCE = "ingest_debate"


def node_id(thread: str) -> str:
    return f"debate_thread:{thread}"


def decision_node_id(decision_id) -> str:
    return f"decision:{decision_id}"


def _iso(ts) -> str | None:
    return ts.isoformat() if ts else None


def changed(conn, identifier, row):
    """Checkpoint and node writes commit together; failed runs remain retryable."""
    digest = hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
    previous = conn.execute('SELECT digest FROM debate_checkpoints WHERE id=?', (identifier,)).fetchone()
    exists = conn.execute('SELECT 1 FROM nodes WHERE id=?', (identifier,)).fetchone()
    if exists and previous == (digest,):
        return False
    conn.execute('INSERT OR REPLACE INTO debate_checkpoints VALUES (?,?)', (identifier, digest))
    return True


def write_decision(conn, r: dict, now: str) -> None:
    """Una decisión como nodo del grafo + edge journal_of desde su thread.

    Es función propia (y no inline en main) porque es la parte testeable sin
    Postgres: recibe una fila ya leída.
    """
    minority = r["minority_report"] or {}
    props = {
        "title": r["title"],
        "objective": r["title"],
        "artifact": r.get("artifact"),
        "evidence": r.get("evidence") or [],
        "explicit_memory": extract(r.get("human_messages") or []),
        "approved_conditions": minority.get("approved_conditions") or [],
        "pending": "Awaiting human input" if r["status"] == "split" else
                   "Implementation in progress" if r["status"] == "executing" else
                   "Deliberation in progress" if r["status"] == "open" else None,
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
        "last_at": _iso(r.get("last_message_at") or r["closed_at"] or r["created_at"]),
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
                   (SELECT artifact FROM decisions WHERE thread=m.thread ORDER BY id DESC LIMIT 1) AS artifact,
                   count(*) AS n_messages,
                   min(m.created_at) AS first_at,
                   max(m.created_at) AS last_at,
                   array_agg(DISTINCT m.author) AS authors,
                   COALESCE((SELECT jsonb_agg(jsonb_build_object(
                       'id',h.id,'author',h.author,'body',h.body,'created_at',h.created_at) ORDER BY h.id)
                       FROM messages h WHERE h.thread=m.thread AND h.author='adrian'), '[]'::jsonb) AS human_messages,
                   (SELECT json_object_agg(kind, kind_count) FROM kc WHERE kc.thread = m.thread) AS kind_counts
            FROM messages m
            GROUP BY m.thread
            """
        ).fetchall()
        decisions = pg.execute(
            """
            SELECT d.*,
                   COALESCE((SELECT jsonb_agg(jsonb_build_object(
                       'id',id,'author',author,'body',body,'created_at',created_at) ORDER BY id)
                       FROM messages WHERE thread=d.thread AND author='adrian'), '[]'::jsonb) AS human_messages,
                   (SELECT max(created_at) FROM messages WHERE thread=d.thread) AS last_message_at,
                   COALESCE((SELECT jsonb_agg(to_jsonb(e) ORDER BY e.id) FROM (
                       SELECT DISTINCT ON (author) id,author,kind,left(body,1800) AS body,created_at
                       FROM messages WHERE thread=d.thread
                         AND kind IN ('analisis','contexto','arbitraje','posicion','consulta','resultado')
                       ORDER BY author,id DESC
                   ) e), '[]'::jsonb) AS evidence
            FROM decisions d
            ORDER BY d.id
            """
        ).fetchall()

    seen: set[str] = set()
    conn.execute('CREATE TABLE IF NOT EXISTS debate_checkpoints (id TEXT PRIMARY KEY, digest TEXT NOT NULL)')
    n = 0
    for r in rows:
        seen.add(node_id(r["thread"]))
        n += 1
        if not changed(conn, node_id(r['thread']), r):
            continue
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
                "thread": r['thread'],
                "artifact": r['artifact'],
                "explicit_memory": extract(r['human_messages']),
                "evidence": [dict(m, kind='human_context', body=m['body'][:1800]) for m in r['human_messages'][-3:]],
                "n_messages": r["n_messages"],
                "authors": r["authors"],
                "kind_counts": r["kind_counts"],
                "first_at": r["first_at"].isoformat(),
                "last_at": r["last_at"].isoformat(),
            },
        )

    seen_decisions: set[str] = set()
    for r in decisions:
        seen_decisions.add(decision_node_id(r["id"]))
        identifier = decision_node_id(r['id'])
        if changed(conn, identifier, r):
            write_decision(conn, r, now)

    n_swept = db.sweep_domain(conn, "debate_thread", seen)
    n_swept_decisions = db.sweep_domain(conn, "decision", seen_decisions)
    conn.execute('DELETE FROM debate_checkpoints WHERE id NOT IN (SELECT id FROM nodes)')
    conn.commit()
    conn.close()
    print(
        f"[ingest_debate] {n} threads ({n_swept} borrados), "
        f"{len(decisions)} decisiones ({n_swept_decisions} borradas)"
    )


if __name__ == "__main__":
    main()
