"""Resuelve un path absoluto a un nodo `code:<project>:<qualified_name>` de
codebase-memory-mcp, leyendo sus SQLite directo (sin pasar por MCP). Usado
por ingest_claude.py/ingest_kimi.py/ingest_docs.py para que un archivo tocado
o citado apunte al mismo nodo que ingest_code.py ya creó, en vez de duplicar
un nodo `file:` genérico cuando el proyecto sí está indexado."""

import sqlite3
from pathlib import Path

CACHE_DIR = Path.home() / ".cache" / "codebase-memory-mcp"


class CodeIndex:
    def __init__(self) -> None:
        self._conns: dict[str, sqlite3.Connection] = {}
        self.projects: list[tuple[Path, str]] = []  # (root_path resuelto, project name)
        for db_path in CACHE_DIR.glob("*.db"):
            if db_path.stem == "_config":
                continue
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            row = conn.execute("SELECT name, root_path FROM projects LIMIT 1").fetchone()
            if not row:
                conn.close()
                continue
            name, root_path = row
            self._conns[name] = conn
            self.projects.append((Path(root_path).resolve(), name))
        # match más específico primero (root_path más largo)
        self.projects.sort(key=lambda t: -len(str(t[0])))

    def project_for(self, abs_path: str) -> str | None:
        try:
            p = Path(abs_path).resolve()
        except OSError:
            return None
        for root, name in self.projects:
            try:
                p.relative_to(root)
                return name
            except ValueError:
                continue
        return None

    def resolve_file(self, abs_path: str) -> str | None:
        try:
            p = Path(abs_path).resolve()
        except OSError:
            return None
        for root, name in self.projects:
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            row = self._conns[name].execute(
                "SELECT qualified_name FROM nodes WHERE label='File' AND file_path=?",
                (str(rel),),
            ).fetchone()
            return f"code:{name}:{row[0]}" if row else None
        return None

    def close(self) -> None:
        for conn in self._conns.values():
            conn.close()
