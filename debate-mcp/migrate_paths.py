#!/usr/bin/env python3
"""Reapunta el registro del MCP `debate` de Claude Code y de Kimi a ESTE
checkout.

Es el paso que convierte al monorepo en la fuente única. Hasta acá, el sistema
que corría en la máquina apuntaba a `/Users/adrianmedina/src/debate-mcp`, un
checkout aparte idéntico salvo el `.git`: todo cambio hecho en el monorepo era
inerte, y ninguno de los dos lados sabía del otro.

Deja un `.bak` con timestamp al lado de cada archivo antes de tocarlo, y no
escribe nada si el archivo ya apunta acá.

Uso:
    python migrate_paths.py --dry-run    # muestra qué cambiaría
    python migrate_paths.py              # aplica
"""

import json
import shutil
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PYTHON = BASE_DIR / ".venv" / "bin" / "python"
SERVER = BASE_DIR / "server.py"

CLAUDE_CONFIG = Path.home() / ".claude.json"
KIMI_CONFIG = Path.home() / ".kimi-code" / "mcp.json"
SERVER_NAME = "debate"


def backup(path: Path) -> Path:
    dest = path.with_suffix(path.suffix + f".bak-{time.strftime('%Y%m%dT%H%M%S')}")
    shutil.copy2(path, dest)
    return dest


def _desired(existing: dict) -> dict:
    """Conserva las claves propias de cada cliente (type, toolTimeoutMs, env)
    y sólo cambia a qué apunta."""
    updated = dict(existing)
    updated["command"] = str(PYTHON)
    updated["args"] = [str(SERVER)]
    return updated


def patch(path: Path, get_servers, dry_run: bool) -> bool:
    if not path.exists():
        print(f"  -- {path} no existe, lo salteo")
        return False

    data = json.loads(path.read_text())
    servers = get_servers(data)
    if servers is None or SERVER_NAME not in servers:
        print(f"  -- {path}: no hay un server '{SERVER_NAME}' registrado")
        return False

    current = servers[SERVER_NAME]
    updated = _desired(current)
    if updated == current:
        print(f"  ok {path}: ya apunta a este checkout")
        return False

    print(f"  -> {path}")
    print(f"       antes: {current.get('command')} {current.get('args')}")
    print(f"       ahora: {updated['command']} {updated['args']}")
    if dry_run:
        return True

    dest = backup(path)
    print(f"       backup: {dest.name}")
    servers[SERVER_NAME] = updated
    path.write_text(json.dumps(data, indent=2))
    return True


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    if not PYTHON.exists():
        print(f"[migrate] falta el venv en {PYTHON}", file=sys.stderr)
        print("[migrate] creálo antes: python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt", file=sys.stderr)
        return 1
    if not SERVER.exists():
        print(f"[migrate] no encuentro {SERVER}", file=sys.stderr)
        return 1

    print(f"[migrate] destino: {PYTHON} {SERVER}")
    if dry_run:
        print("[migrate] DRY RUN, no se escribe nada")

    changed = False
    changed |= patch(CLAUDE_CONFIG, lambda d: d.get("mcpServers"), dry_run)
    changed |= patch(KIMI_CONFIG, lambda d: d.get("mcpServers"), dry_run)

    print()
    if not changed:
        print("[migrate] nada que cambiar")
    elif dry_run:
        print("[migrate] corré sin --dry-run para aplicar")
    else:
        print("[migrate] listo. Reiniciá las sesiones de Claude Code y Kimi para que")
        print("          levanten el servidor desde el checkout nuevo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
