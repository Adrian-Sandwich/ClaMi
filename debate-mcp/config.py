"""Configuración compartida del tablero de debate.

Antes el conninfo estaba escrito literal en server.py, relay.py e
ingest_debate.py — tres copias que había que acordarse de cambiar juntas.
Ahora hay un solo default y una sola variable de entorno para pisarlo.
"""

import os

import psycopg
from psycopg.rows import dict_row

DEFAULT_CONNINFO = "dbname=trade_debate user=adrianmedina host=localhost"
CONNINFO = os.environ.get("DEBATE_CONNINFO", DEFAULT_CONNINFO)


def connect(conninfo: str | None = None) -> psycopg.Connection:
    """Conexión al tablero. autocommit porque LISTEN no puede ir dentro de
    una transacción, y todo lo que escribimos es un INSERT suelto."""
    return psycopg.connect(conninfo or CONNINFO, autocommit=True, row_factory=dict_row)
