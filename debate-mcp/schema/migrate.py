#!/usr/bin/env python3
"""Aplica las migraciones .sql de este directorio, en orden, una sola vez.

La base se había creado a mano: el DDL no vivía en ningún archivo, así que
el tablero no era reproducible desde el código. Esto lo
arregla sin traer Alembic para una tabla — archivos numerados y una tabla
`schema_version` que registra cuáles ya corrieron.

Cada migración corre en su propia transacción junto con el INSERT a
schema_version: o se aplica entera y queda registrada, o no pasa nada.

Uso:
    python schema/migrate.py           # aplica lo pendiente
    python schema/migrate.py --status  # sólo lista qué falta
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from config import CONNINFO  # noqa: E402

SCHEMA_DIR = Path(__file__).resolve().parent
RE_MIGRATION = re.compile(r"^(\d+)_(.+)\.sql$")

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    integer PRIMARY KEY,
    name       text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def discover() -> list[tuple[int, str, Path]]:
    found = []
    for path in SCHEMA_DIR.glob("*.sql"):
        m = RE_MIGRATION.match(path.name)
        if not m:
            print(f"[migrate] ignoro {path.name}: no matchea NNN_nombre.sql", file=sys.stderr)
            continue
        found.append((int(m.group(1)), m.group(2), path))
    found.sort()
    dupes = {v for v, _, _ in found if [x for x, _, _ in found].count(v) > 1}
    if dupes:
        raise SystemExit(f"[migrate] versiones duplicadas: {sorted(dupes)}")
    return found


def applied(conn) -> set[int]:
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    return {r[0] for r in rows}


def main() -> int:
    status_only = "--status" in sys.argv
    migrations = discover()

    with psycopg.connect(CONNINFO) as conn:
        conn.execute(BOOTSTRAP)
        conn.commit()
        done = applied(conn)
        pending = [m for m in migrations if m[0] not in done]

        if status_only:
            for version, name, _ in migrations:
                mark = "ok  " if version in done else "PEND"
                print(f"[migrate] {mark} {version:03d}_{name}")
            return 0

        if not pending:
            print(f"[migrate] nada pendiente ({len(done)} aplicadas)")
            return 0

        for version, name, path in pending:
            sql_text = path.read_text()
            with conn.transaction():
                conn.execute(sql_text)
                conn.execute(
                    "INSERT INTO schema_version (version, name) VALUES (%s, %s)",
                    (version, name),
                )
            print(f"[migrate] aplicada {version:03d}_{name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
