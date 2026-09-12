"""Configuración de memory-graph: todo lo que antes estaba hardcodeado.

El módulo existe porque el ingestor tenía rutas absolutas del disco de una
máquina puntual metidas en el código: el conninfo de Postgres repetido literal,
`/Users/adrianmedina/src/Node_visualizer` con un `sys.path.insert` encima, y una
lista literal de cinco `.md` del proyecto `trade`. Nada de eso se podía mover
ni compartir sin editar fuentes.

Todo se puede pisar por variable de entorno; los defaults son los valores que
ya venía usando el sistema, así que no cambia el comportamiento de nadie.
"""

import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- Postgres (tablero de debate) ---------------------------------------
DEFAULT_CONNINFO = "dbname=debate host=localhost"
CONNINFO = os.environ.get("DEBATE_CONNINFO", DEFAULT_CONNINFO)

# --- SQLite propio -------------------------------------------------------
DB_PATH = Path(os.environ.get("MEMORY_GRAPH_DB", Path(__file__).resolve().parent / "memory.db"))

# --- Visor 3D ------------------------------------------------------------
NODE_VISUALIZER = Path(
    os.environ.get("NODE_VISUALIZER_DIR", REPO_ROOT.parent / "Node_visualizer")
)
KGRAPH_OUT = Path(
    os.environ.get("MEMORY_GRAPH_OUT", NODE_VISUALIZER / "graphs" / "memory.kgraph.json")
)

# --- Fuentes de sesiones -------------------------------------------------
CLAUDE_PROJECTS_DIR = Path(
    os.environ.get("CLAUDE_PROJECTS_DIR", Path.home() / ".claude" / "projects")
)
KIMI_HOME = Path(os.environ.get("KIMI_HOME", Path.home() / ".kimi-code"))
CODE_CACHE_DIR = Path(
    os.environ.get("CODEBASE_MEMORY_CACHE", Path.home() / ".cache" / "codebase-memory-mcp")
)

# --- Docs a ingestar -----------------------------------------------------
# Lista de globs, no de archivos: antes había que editar el .py para sumar un
# documento, y los README del propio monorepo nunca entraron al grafo.
# El default sólo incluye los READMEs de ESTE repo (portable: correr en
# cualquier máquina no exige tener el checkout de un proyecto particular).
# Para sumar docs de otros proyectos, MEMORY_GRAPH_DOCS (JSON o lista
# separada por ':') y MEMORY_GRAPH_DOC_ROOTS.
DEFAULT_DOC_GLOBS = [
    str(REPO_ROOT / "README.md"),
    str(REPO_ROOT / "*" / "README.md"),
]

# Cada glob se resuelve contra la raíz del proyecto al que pertenece, para que
# las referencias relativas dentro del doc apunten al repo correcto.
DOC_ROOTS = [
    Path(p) for p in os.environ.get(
        "MEMORY_GRAPH_DOC_ROOTS", str(REPO_ROOT)
    ).split(":") if p
]


def doc_globs() -> list[str]:
    raw = os.environ.get("MEMORY_GRAPH_DOCS")
    if not raw:
        return DEFAULT_DOC_GLOBS
    # admite JSON (lista) o una lista separada por ':' para uso rápido en shell
    raw = raw.strip()
    if raw.startswith("["):
        return json.loads(raw)
    return [p for p in raw.split(":") if p]


def project_root_for(path: Path) -> Path:
    """Raíz de repo a la que pertenece un doc; se usa para resolver las
    referencias a archivos que el doc cita entre backticks."""
    path = path.resolve()
    for root in sorted(DOC_ROOTS, key=lambda p: -len(str(p))):
        try:
            path.relative_to(root)
            return root
        except ValueError:
            continue
    for parent in path.parents:
        if (parent / ".git").exists():
            return parent
    return path.parent
