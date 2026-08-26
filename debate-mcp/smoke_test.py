#!/usr/bin/env python3
"""Smoke test de arranque: ¿este checkout puede correr el tablero?

No reemplaza a los tests unitarios de memory-graph (ver `tests/`). Lo que
verifica es lo que se rompe al mover el proyecto de lugar: que el venv tenga
las dependencias, que el conninfo apunte a una base con el esquema aplicado,
que los tools del servidor respondan de verdad contra Postgres, que el
LISTEN/NOTIFY esté conectado, y que los binarios de los agentes existan.

Fue escrito para la migración del monorepo: correr esto ANTES de reapuntar
`~/.claude.json`, `~/.kimi-code/mcp.json` y el plist de launchd, y otra vez
después.

Escribe y borra un mensaje real en el thread `__smoke__`, con author 'adrian'
para que el relay no lo tome como turno de nadie y no dispare agentes.

Uso:  python smoke_test.py     (exit 0 = listo para migrar)
"""

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg  # noqa: E402

import relay  # noqa: E402
import server  # noqa: E402
from config import CONNINFO, connect  # noqa: E402

THREAD = "__smoke__"
_failures: list[str] = []


def check(name: str):
    def deco(fn):
        try:
            detail = fn()
        except Exception:
            _failures.append(name)
            print(f"  FAIL {name}")
            print("       " + traceback.format_exc().strip().replace("\n", "\n       "))
        else:
            print(f"  ok   {name}" + (f" — {detail}" if detail else ""))
        return fn
    return deco


def main() -> int:
    print(f"[smoke] checkout: {Path(__file__).resolve().parent}")
    print(f"[smoke] python:   {sys.executable}")
    print(f"[smoke] conninfo: {CONNINFO}")

    @check("deps del venv")
    def _deps():
        import mcp  # noqa: F401
        return f"psycopg {psycopg.__version__}"

    @check("postgres alcanzable")
    def _pg():
        with connect() as conn:
            return f"{conn.execute('SELECT count(*) AS n FROM messages').fetchone()['n']} mensajes"

    @check("esquema aplicado")
    def _schema():
        with connect() as conn:
            row = conn.execute("SELECT max(version) AS v FROM schema_version").fetchone()
        assert row["v"] is not None, "schema_version vacía: falta correr schema/migrate.py"
        return f"v{row['v']}"

    @check("tool list_threads")
    def _list():
        threads = server.list_threads()
        assert isinstance(threads, list)
        return f"{len(threads)} threads"

    @check("tool post_message + read_thread (ida y vuelta)")
    def _roundtrip():
        posted = server.post_message(
            thread=THREAD, author="adrian", kind="arbitraje",
            body="smoke test", artifact="/tmp/smoke",
        )
        out = server.read_thread(thread=THREAD, since_id=posted["id"] - 1)
        assert out["messages"], "read_thread no devolvió el mensaje recién posteado"
        assert out["messages"][-1]["id"] == posted["id"]
        assert out["max_id"] == posted["id"], f"max_id {out['max_id']} != {posted['id']}"
        # el caso que rompía el query viejo: página vacía pero max_id real
        empty = server.read_thread(thread=THREAD, since_id=posted["id"])
        assert empty["messages"] == [], "esperaba página vacía"
        assert empty["max_id"] == posted["id"], "max_id se perdió en la página vacía"
        return f"id={posted['id']}"

    @check("validación de thread largo")
    def _validate():
        try:
            server.post_message(thread="x" * 100, author="adrian", kind="arbitraje", body="no")
        except ValueError:
            return "rechaza threads que romperían el canal de NOTIFY"
        raise AssertionError("aceptó un thread de 100 bytes")

    @check("LISTEN/NOTIFY por thread")
    def _notify_thread():
        with connect() as listener:
            listener.execute(f'LISTEN "debate_{THREAD}"')
            posted = server.post_message(
                thread=THREAD, author="adrian", kind="arbitraje", body="notify",
            )
            for n in listener.notifies(timeout=10, stop_after=1):
                assert n.payload == str(posted["id"]), f"payload {n.payload}"
                return f"canal debate_{THREAD}"
        raise AssertionError("no llegó el NOTIFY por thread en 10s")

    @check("LISTEN/NOTIFY global (canal del relay)")
    def _notify_all():
        with connect() as listener:
            listener.execute(f"LISTEN {relay.CHANNEL_ALL}")
            posted = server.post_message(
                thread=THREAD, author="adrian", kind="arbitraje", body="notify all",
            )
            for n in listener.notifies(timeout=10, stop_after=1):
                assert n.payload == str(posted["id"])
                return f"canal {relay.CHANNEL_ALL}"
        raise AssertionError(
            f"no llegó el NOTIFY global en 10s — ¿falta la migración 002_notify_all?"
        )

    @check("binarios de los agentes")
    def _bins():
        missing = [b for b in (relay.CLAUDE_BIN, relay.KIMI_BIN) if not Path(b).exists()]
        assert not missing, f"no existen: {missing}"
        return "claude y kimi presentes"

    @check("relay: cwd derivado del artifact")
    def _cwd():
        root = str(Path(__file__).resolve().parent.parent)
        got = relay.cwd_from_artifact(root)
        assert got == root, f"esperaba {root}, obtuve {got}"
        assert relay.cwd_from_artifact("src/paper.rs:120") is None, "no debería aceptar relativos"
        return root

    # limpieza: los mensajes del smoke no tienen que quedar en el tablero
    try:
        with connect() as conn:
            n = conn.execute(
                "DELETE FROM messages WHERE thread = %s", (THREAD,)
            ).rowcount
        print(f"  ok   limpieza — {n} mensajes de prueba borrados")
    except Exception as exc:
        print(f"  WARN limpieza falló: {exc}")

    print()
    if _failures:
        print(f"[smoke] FALLÓ: {', '.join(_failures)}")
        return 1
    print("[smoke] todo verde")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
