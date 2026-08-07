"""Store compartido del grafo de memoria: nodos/edges en SQLite, natural-key
upsert para que correr los ingestors dos veces sea un no-op sobre el contenido.
"""

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "memory.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    tag TEXT NOT NULL DEFAULT 'Unknown',
    domain TEXT NOT NULL,
    size REAL,
    tooltip TEXT,
    props TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_domain ON nodes(domain);
CREATE INDEX IF NOT EXISTS idx_nodes_source ON nodes(source);

CREATE TABLE IF NOT EXISTS edges (
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    type TEXT NOT NULL,
    label_forward TEXT,
    label_backward TEXT,
    weight REAL NOT NULL DEFAULT 1.0,
    source TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (from_id, to_id, type)
);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_id);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def upsert_node(
    conn: sqlite3.Connection,
    id: str,
    domain: str,
    source: str,
    updated_at: str,
    label: str | None = None,
    tag: str = "Unknown",
    size: float | None = None,
    tooltip: str | None = None,
    props: dict | None = None,
) -> None:
    """Merge semantics: label/tag/tooltip/size se pisan, props se mergea
    (no se reemplaza) para que dos ingestors distintos tocando el mismo nodo
    (ej. un `file` citado por un doc y tocado por una sesión) no se borren
    los props del otro."""
    label = label if label is not None else id
    props = props or {}
    row = conn.execute("SELECT props FROM nodes WHERE id = ?", (id,)).fetchone()
    if row:
        merged = json.loads(row[0])
        merged.update(props)
        props = merged
    conn.execute(
        """
        INSERT INTO nodes (id, label, tag, domain, size, tooltip, props, source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            label=excluded.label, tag=excluded.tag, domain=excluded.domain,
            size=excluded.size, tooltip=excluded.tooltip, props=excluded.props,
            source=excluded.source, updated_at=excluded.updated_at
        """,
        (id, label, tag, domain, size, tooltip, json.dumps(props), source, updated_at),
    )


def upsert_edge(
    conn: sqlite3.Connection,
    from_id: str,
    to_id: str,
    type: str,
    source: str,
    updated_at: str,
    label_forward: str | None = None,
    label_backward: str | None = None,
    weight: float = 1.0,
) -> None:
    conn.execute(
        """
        INSERT INTO edges (from_id, to_id, type, label_forward, label_backward, weight, source, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(from_id, to_id, type) DO UPDATE SET
            label_forward=excluded.label_forward, label_backward=excluded.label_backward,
            weight=excluded.weight, source=excluded.source, updated_at=excluded.updated_at
        """,
        (from_id, to_id, type, label_forward, label_backward, weight, source, updated_at),
    )


def reset_source_edges(conn: sqlite3.Connection, source: str, from_id: str) -> None:
    """Antes de reinsertar los edges salientes de una entidad, borra los
    viejos de ese mismo source/from_id — evita doble-conteo de weight al
    re-correr el ingestor sobre la misma sesión/doc."""
    conn.execute("DELETE FROM edges WHERE source = ? AND from_id = ?", (source, from_id))
