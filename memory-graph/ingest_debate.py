"""Nodos debate_thread desde Postgres trade_debate. No crea edges — las
crean ingest_claude/ingest_kimi/ingest_docs referenciando debate_thread:<name>
por natural key (no importa el orden de ingestión, finalize() en
export_kgraph.py tira los edges que queden colgando)."""

from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

import db

CONNINFO = "dbname=trade_debate user=adrianmedina host=localhost"
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

    n = 0
    for r in rows:
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

    conn.commit()
    conn.close()
    print(f"[ingest_debate] {n} threads")


if __name__ == "__main__":
    main()
