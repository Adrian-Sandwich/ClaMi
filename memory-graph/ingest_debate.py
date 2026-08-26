"""Nodos debate_thread desde la base `debate` de Postgres. No crea edges — las
crean ingest_claude/ingest_kimi/ingest_docs referenciando debate_thread:<name>
por natural key (no importa el orden de ingestión, finalize() en
export_kgraph.py tira los edges que queden colgando)."""

from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

import db
import settings

CONNINFO = settings.CONNINFO
SOURCE = "ingest_debate"


def node_id(thread: str) -> str:
    return f"debate_thread:{thread}"


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = db.connect()
    with psycopg.connect(CONNINFO, row_factory=dict_row) as pg:
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

    n_swept = db.sweep_domain(conn, "debate_thread", seen)
    conn.commit()
    conn.close()
    print(f"[ingest_debate] {n} threads, {n_swept} borrados")


if __name__ == "__main__":
    main()
