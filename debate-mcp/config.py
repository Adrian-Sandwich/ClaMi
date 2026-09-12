"""Configuración compartida del tablero de debate.

Un solo default y una sola variable de entorno para pisarlo (DEBATE_CONNINFO,
el conninfo estándar de libpq). El default no fija usuario ni host raros:
libpq toma el usuario del sistema operativo y localhost, así el mismo código
arranca en cualquier máquina. Antes el default traía user=adrianmedina del
Mac de desarrollo y en cualquier otro lado fallaba de entrada.
"""

import os

import psycopg
from psycopg.rows import dict_row

DEFAULT_CONNINFO = "dbname=debate host=localhost"
CONNINFO = os.environ.get("DEBATE_CONNINFO", DEFAULT_CONNINFO)


def connect(conninfo: str | None = None) -> psycopg.Connection:
    """Conexión al tablero. autocommit porque LISTEN no puede ir dentro de
    una transacción, y todo lo que escribimos es un INSERT suelto.

    client_encoding forzado a utf-8: el journal guarda tildes, → y kanjis;
    sin esto, psycopg serializa los params con el encoding del locale del
    SO (cp1252 en Windows) y cualquier texto con caracteres fuera de esa
    tabla explota al dumpear — lo encontró el smoke del critique en Windows.

    connect_timeout acotado: sin él, un Postgres caído (o una laptop
    suspendida, que es lo mismo para TCP) cuelga cada intento ~2 minutos.
    Con esto, el relay reintenta cada pocos segundos y la UI deja de
    congelar requests — el heartbeat pg_ok=false del relay es la señal.
    """
    base = conninfo or CONNINFO
    if "connect_timeout" not in base:
        sep = "&" if base.strip().startswith("postgresql://") else " "
        base = f"{base}{sep}connect_timeout=10"
    return psycopg.connect(
        base, autocommit=True, row_factory=dict_row,
        client_encoding="utf-8",
    )
