"""Memoria del consejo: lectura del grafo de memory-graph para enriquecer
los prompts de las cabezas con lo que el sistema ya vivió.

El tablero es solo un LECTOR del grafo (sqlite3 en modo read-only): si la
base no existe (la ingesta nunca corrió) o una consulta falla, devuelve
texto vacío y las cabezas deliberan como siempre — degradado, nunca roto.
Nada de esto toca el protocolo de voto: la memoria es contexto, no verdad.
"""

import json
import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR.parent / "memory-graph" / "memory.db"
DB_PATH = Path(os.environ.get("MEMORY_GRAPH_DB", DEFAULT_DB))

MAX_HITS = 8        # nodos por término del título
MAX_DECISIONES = 5  # decisiones recientes, siempre (memoria institucional)

# términos funcionales del español rioplatense que no filtran nada
STOPWORDS = {
    "para", "sobre", "como", "esta", "este", "esto", "estas", "estos",
    "quiero", "cual", "cuál", "cuales", "donde", "dónde", "cuando",
    "cuándo", "desde", "hasta", "entre", "consejo", "decision", "decisión",
    "sistema", "pregunta", "prueba", "votad", "voten", "argumenten",
}


def _terminos(titulo: str) -> list[str]:
    out = []
    for t in titulo.split():
        t = t.strip(".,;:!?¿¡()[]\"'").lower()
        if len(t) >= 4 and t not in STOPWORDS:
            out.append(t)
    return out[:4]


def _connect() -> sqlite3.Connection | None:
    if not DB_PATH.exists():
        return None
    try:
        # Sin mode=ro: la base queda en WAL (db.connect la abre así) y un
        # read-only no puede crear el -shm. Solo hacemos SELECTs; WAL da
        # snapshots consistentes aunque la ingesta escriba en paralelo.
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _linea_decision(row: sqlite3.Row) -> str:
    label = row["label"]
    try:
        props = json.loads(row["props"] or "{}")
    except json.JSONDecodeError:
        props = {}
    ruling = props.get("ruling") or "sin ruling"
    conf = props.get("confidence")
    conf_txt = f" (confianza {conf})" if conf is not None else ""
    return f"- {label} → {ruling}{conf_txt}"


def memoria_para(titulo: str, artifact: str | None = None) -> str:
    """Bloque de texto con memoria relevante para una deliberación, o ""."""
    conn = _connect()
    if conn is None:
        return ""
    try:
        lineas: list[str] = []
        vistos: set[str] = set()

        # 1) memoria institucional básica: las decisiones más recientes
        rows = conn.execute(
            """
            SELECT id, label, props FROM nodes
            WHERE domain = 'decision'
            ORDER BY updated_at DESC LIMIT %s
            """ % MAX_DECISIONES,
        ).fetchall()
        for r in rows:
            vistos.add(r["id"])
            lineas.append(_linea_decision(r))

        # 2) nodos que mencionan los términos del título o del artefacto
        terminos = _terminos(titulo)
        if artifact:
            terminos.extend(_terminos(Path(artifact.replace("\\", "/")).name))
        for t in terminos:
            like = f"%{t}%"
            hits = conn.execute(
                """
                SELECT id, label, domain, props FROM nodes
                WHERE (lower(label) LIKE ? OR lower(id) LIKE ?)
                  AND id NOT IN (%s)
                ORDER BY updated_at DESC LIMIT %s
                """ % (",".join("?" * len(vistos)) or "''", MAX_HITS),
                ([like, like] + list(vistos)),
            ).fetchall()
            for h in hits:
                if h["id"] in vistos:
                    continue
                vistos.add(h["id"])
                if h["domain"] == "decision":
                    lineas.append(_linea_decision(h))
                else:
                    lineas.append(f"- [{h['domain']}] {h['label']}")

        if not lineas:
            return ""
        return "Memoria del consejo (decisiones y nodos previos del grafo):\n" + "\n".join(lineas)
    except sqlite3.Error:
        return ""
    finally:
        conn.close()
