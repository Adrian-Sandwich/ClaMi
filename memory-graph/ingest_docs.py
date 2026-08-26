"""Nodos doc para las notas escritas a mano + edges por regex: cita de thread,
referencia a otro doc ("ver X.md"), referencia a archivo (path entre backticks
con extensión conocida).

Qué documentos entran sale de `settings.doc_globs()`. Antes era una lista
literal de cinco archivos del proyecto `trade` dentro de este mismo .py: sumar
un doc pedía editar código, y los README del propio monorepo nunca llegaron al
grafo. Ahora son globs configurables, y la raíz de proyecto contra la que se
resuelven las referencias del doc se deduce por documento — no hay un único
TRADE_ROOT para todo.
"""

import re
from datetime import datetime, timezone
from glob import glob
from pathlib import Path

import db
import ingest_debate
import settings
from code_lookup import CodeIndex

SOURCE = "ingest_docs"

RE_THREAD = re.compile(r"thread\s+`([\w-]+)`")
RE_DOC_REF = re.compile(r"[Vv]er\s+([\w./-]+\.md)")
RE_FILE_REF = re.compile(r"`([\w./-]+\.(?:rs|py|md|json|toml|sh|sql|ts|tsx|js))`")


def doc_id(path: Path) -> str:
    return f"doc:{path}"


def discover_docs() -> list[Path]:
    """Resuelve los globs configurados a rutas concretas, sin repetidos y en
    orden estable."""
    found: list[Path] = []
    for pattern in settings.doc_globs():
        for hit in sorted(glob(pattern, recursive=True)):
            p = Path(hit)
            if p.is_file() and p not in found:
                found.append(p)
    return found


def file_id(code_index: CodeIndex, project_root: Path, rel_or_name: str) -> str:
    candidate = project_root / rel_or_name
    if not candidate.exists():
        matches = list(project_root.rglob(rel_or_name))
        candidate = matches[0] if matches else candidate
    resolved = code_index.resolve_file(str(candidate))
    if resolved:
        return resolved
    return f"file:{candidate.resolve() if candidate.exists() else candidate}"


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = db.connect()
    code_index = CodeIndex()
    seen: set[str] = set()
    n_docs = n_edges = 0

    for path in discover_docs():
        # cada doc resuelve sus referencias contra el repo al que pertenece
        project_root = settings.project_root_for(path)
        text = path.read_text(errors="replace")
        did = doc_id(path)
        seen.add(did)
        title = text.splitlines()[0].lstrip("# ").strip() if text else path.name
        db.upsert_node(
            conn,
            id=did,
            domain="doc",
            source=SOURCE,
            updated_at=now,
            label=path.name,
            tag="Doc",
            size=10,
            tooltip=title[:200],
            props={"path": str(path), "project_root": str(project_root)},
        )
        n_docs += 1
        db.reset_source_edges(conn, SOURCE, did)

        for m in RE_THREAD.finditer(text):
            db.upsert_edge(
                conn, did, ingest_debate.node_id(m.group(1)), "documents",
                source=SOURCE, updated_at=now, label_forward="documenta",
            )
            n_edges += 1

        for m in RE_DOC_REF.finditer(text):
            target = path.parent / m.group(1)
            if target.exists():
                db.upsert_edge(
                    conn, did, doc_id(target), "references",
                    source=SOURCE, updated_at=now, label_forward="ver",
                )
                n_edges += 1

        for m in RE_FILE_REF.finditer(text):
            name = m.group(1)
            if name.endswith(".md"):
                continue  # ya cubierto por RE_DOC_REF si corresponde
            target = file_id(code_index, project_root, name)
            if not target.startswith("code:"):
                # fallback genérico; si resolvió a un nodo `code:` ya lo creó ingest_code.py
                db.upsert_node(conn, id=target, domain="file", source=SOURCE, updated_at=now, label=name, tag="File")
            db.upsert_edge(
                conn, did, target, "references",
                source=SOURCE, updated_at=now, label_forward="cita",
            )
            n_edges += 1

    n_swept = db.sweep_domain(conn, "doc", seen)

    code_index.close()
    conn.commit()
    conn.close()
    print(f"[ingest_docs] {n_docs} docs, {n_edges} edges, {n_swept} borrados")


if __name__ == "__main__":
    main()
