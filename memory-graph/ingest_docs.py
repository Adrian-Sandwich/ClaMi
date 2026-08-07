"""Nodos doc para las notas escritas a mano de trade + edges por regex:
cita de thread, referencia a otro doc ("ver X.md"), referencia a archivo
(path entre backticks con extensión conocida)."""

import re
from datetime import datetime, timezone
from pathlib import Path

import db
import ingest_debate
from code_lookup import CodeIndex

SOURCE = "ingest_docs"
TRADE_ROOT = Path("/Users/adrianmedina/src/trade")

DOCS = [
    TRADE_ROOT / "CLAUDE.md",
    TRADE_ROOT / "README.md",
    TRADE_ROOT / "experiments" / "2026-07-asset-candidates.md",
    TRADE_ROOT / "experiments" / "2026-08-sleeves-plan.md",
    TRADE_ROOT / "data" / "paper" / "report.md",
]

RE_THREAD = re.compile(r"thread\s+`([\w-]+)`")
RE_DOC_REF = re.compile(r"[Vv]er\s+([\w./-]+\.md)")
RE_FILE_REF = re.compile(r"`([\w./-]+\.(?:rs|py|md|json|toml|sh))`")


def doc_id(path: Path) -> str:
    return f"doc:{path}"


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
    n_docs = n_edges = 0

    for path in DOCS:
        if not path.exists():
            continue
        text = path.read_text(errors="replace")
        did = doc_id(path)
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
            props={"path": str(path)},
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
            target = file_id(code_index, TRADE_ROOT, name)
            if not target.startswith("code:"):
                # fallback genérico; si resolvió a un nodo `code:` ya lo creó ingest_code.py
                db.upsert_node(conn, id=target, domain="file", source=SOURCE, updated_at=now, label=name, tag="File")
            db.upsert_edge(
                conn, did, target, "references",
                source=SOURCE, updated_at=now, label_forward="cita",
            )
            n_edges += 1

    code_index.close()
    conn.commit()
    conn.close()
    print(f"[ingest_docs] {n_docs} docs, {n_edges} edges")


if __name__ == "__main__":
    main()
