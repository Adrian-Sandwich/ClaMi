"""La parte mecánica, compartida, de leer un log de sesión.

`ingest_claude.extract_facts` e `ingest_kimi.extract_agent_facts` eran las dos
funciones más complejas del repo (cyclomatic 16 y 15, cognitive 54 y 53, medido
sobre el grafo de código) y hacían casi lo mismo: leer un .jsonl línea por
línea aguantando basura, contar turnos, quedarse con el primer y el último
timestamp, acumular archivos tocados y threads del tablero.

Lo que de verdad difiere entre Claude y Kimi es el DIALECTO: cómo se llama el
campo del timestamp, qué evento es un turno, dónde vive el nombre de la
herramienta. Eso se queda en cada ingestor, explícito. Todo lo demás vive acá,
y es un solo lugar donde arreglar el drift de formato cuando aparezca.
"""

import json
from pathlib import Path

# Claves que usan las herramientas de ambos agentes para referirse a un archivo.
FILE_PATH_KEYS = ("file_path", "path", "notebook_path")


def read_jsonl(path: Path, facts: "Facts"):
    """Itera los registros JSON de un log, contando las líneas rotas en vez de
    cortar la ingesta. Los transcripts se escriben mientras el agente trabaja:
    una línea truncada al final es normal, no un error."""
    # encoding="utf-8" explícito: en Windows el default del locale no es UTF-8
    # y los transcripts/logs con tildes se corrompen.
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                facts.bad_lines += 1


class Facts:
    """Acumulador de los hechos que el grafo saca de una sesión."""

    def __init__(self) -> None:
        self.n_turns = 0
        self.first_ts = None
        self.last_ts = None
        self.touched: dict[str, int] = {}
        self.threads: set[str] = set()
        self.bad_lines = 0

    def observe_ts(self, ts) -> None:
        """Los logs vienen ordenados, así que el primero que se ve es el más
        viejo y el último que se ve es el más nuevo."""
        if not ts:
            return
        if self.first_ts is None:
            self.first_ts = ts
        self.last_ts = ts

    def touch(self, path: str) -> None:
        self.touched[path] = self.touched.get(path, 0) + 1

    def touch_from(self, args: dict) -> None:
        """Cuenta un archivo por llamada de herramienta, mirando la primera
        clave conocida — no todas, para que un `path` y un `file_path` en la
        misma llamada no cuenten doble."""
        for key in FILE_PATH_KEYS:
            if key in args:
                self.touch(args[key])
                return

    def note_thread(self, thread: str) -> None:
        self.threads.add(thread)

    def as_dict(self, **extra) -> dict:
        """`threads` sale como lista ordenada: estos hechos se serializan a
        JSON en el cache de archivos de db.py."""
        return {
            "n_turns": self.n_turns,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "touched": self.touched,
            "threads": sorted(self.threads),
            "bad_lines": self.bad_lines,
            **extra,
        }


def merge_facts(facts_list: list[dict], first_wins: tuple[str, ...] = ()) -> dict:
    """Junta los hechos de varios archivos que pertenecen a la misma sesión
    (los transcripts de subagentes comparten sessionId; Kimi guarda un
    wire.jsonl por agente).

    `first_wins` son los campos de los que se conserva el primer valor no vacío
    — título, prompt inicial, cwd: cosas de las que hay una sola por sesión.
    """
    merged = {
        "n_turns": 0, "first_ts": None, "last_ts": None,
        "touched": {}, "threads": set(), "bad_lines": 0,
    }
    for key in first_wins:
        merged[key] = None

    for f in facts_list:
        for key in first_wins:
            merged[key] = merged[key] or f.get(key)
        merged["n_turns"] += f["n_turns"]
        merged["bad_lines"] += f["bad_lines"]
        if f["first_ts"] and (merged["first_ts"] is None or f["first_ts"] < merged["first_ts"]):
            merged["first_ts"] = f["first_ts"]
        if f["last_ts"] and (merged["last_ts"] is None or f["last_ts"] > merged["last_ts"]):
            merged["last_ts"] = f["last_ts"]
        for path_, count in f["touched"].items():
            merged["touched"][path_] = merged["touched"].get(path_, 0) + count
        merged["threads"] |= set(f["threads"])

    return merged
