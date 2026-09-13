#!/usr/bin/env python3
"""UI MAGI: server HTTP stdlib que sirve la interfaz y empuja estado por SSE.

Rutas:
    GET  /            → la interfaz (ui/index.html + style.css + app.js)
    GET  /state       → snapshot JSON de decisiones (abiertas + últimas cerradas)
    GET  /events      → Server-Sent Events: cada notify de Postgres
                        (debate_all / decision_all) o cada POLL_SECS empuja
                        el snapshot a los clientes conectados
    POST /start       → {title, artifact?, protocol?} abre una decisión vía
                        board.start_decision — el mismo código que el tool MCP

SSE le queda perfecto al tablero: el backend ya empuja eventos por
LISTEN/NOTIFY, y la UI es de un solo sentido. Sin dependencias nuevas.

La interfaz es un port a vanilla JS de los componentes tontos de
TomaszRewak/MAGI (MIT, © 2023 Tomasz Rewak — ver aviso en ui/style.css):
mismo CSS, mismas clases, mismos colores y kanjis.
"""

import json
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board  # noqa: E402

from config import connect  # noqa: E402

UI_DIR = Path(__file__).resolve().parent / "ui"

PORT = int(os.environ.get("MAGI_UI_PORT", "8051"))
POLL_SECS = 5
CLOSED_DECISIONS = 10
JOURNAL_MESSAGES = 12

# thread del chat libre con las cabezas (modo "charla"): un solo thread
# compartido, como la conversación con un CLI — la historia queda en el journal.
CHAT_THREAD = "chat"
CHAT_MESSAGES = 12

# Labels del veredicto en inglés: los kanjis decoraban el centro del
# triángulo pero no informaban (queja real del operador). El layout visual
# sigue siendo el de TomaszRewak/MAGI; lo que cambia es el texto.
VERDICT_LABELS = {
    "yes": "APPROVED",
    "no": "REJECTED",
    "conditional": "CONDITIONAL",
    "info": "INFO",
    "error": "ERROR",
}
COLORS = {
    "yes": "#52e691",
    "no": "#a41413",
    "conditional": "#ff8d00",
    "info": "#3caee0",
    "error": "gray",
}

# En el journal de una decisión, la conversación son las posiciones con su
# razonamiento, los contextos del operador, la consulta de destrabe y el
# cierre — no el título (ya es el encabezado) ni la mecánica interna.
CONVERSATION_KINDS = ("posicion", "resultado", "arbitraje", "contexto", "consulta")

CLIENTS: list[queue.Queue] = []
CLIENTS_LOCK = threading.Lock()


def verdict_badge(d: dict) -> dict:
    """El veredicto del centro del triángulo, según el estado de la decisión."""
    if d["status"] == "open":
        return {"text": "DELIBERATING", "color": "#ff8d00", "flicker": True}
    if d["status"] == "split" or (d["status"] == "closed" and not d["ruling"]):
        # split (o cerrada por arbitraje humano, sin ruling de máquina)
        return {"text": "STALEMATE", "color": "gray", "flicker": False}
    return {"text": VERDICT_LABELS[d["ruling"]], "color": COLORS[d["ruling"]], "flicker": False}


def build_state(conn) -> dict:
    """Snapshot para la UI: decisiones abiertas + últimas cerradas, con las
    posiciones por asiento (la de la ronda más alta, y si votó en la actual)
    y la cola del journal. Recibe una conexión: testeable sin levantar HTTP."""
    open_rows = conn.execute(
        """
        SELECT id, title, artifact, protocol, status, ruling, confidence, round, thread, heads
        FROM decisions WHERE status = 'open' ORDER BY id
        """
    ).fetchall()
    closed_rows = conn.execute(
        """
        SELECT id, title, artifact, protocol, status, ruling, confidence, round, thread, heads
        FROM decisions WHERE status <> 'open' ORDER BY id DESC LIMIT %s
        """,
        (CLOSED_DECISIONS,),
    ).fetchall()
    rows = list(open_rows) + list(closed_rows)

    positions: list[dict] = []
    messages: list[dict] = []
    if rows:
        ids = [r["id"] for r in rows]
        positions = conn.execute(
            """
            SELECT p.decision_id, p.head, p.round, p.position, p.conditions, m.body
            FROM positions p LEFT JOIN messages m ON m.id = p.message_id
            WHERE p.decision_id = ANY(%s)
            ORDER BY p.decision_id, p.round, p.head
            """,
            (ids,),
        ).fetchall()
        threads = [r["thread"] for r in rows]
        messages = conn.execute(
            """
            SELECT thread, author, kind, body, created_at FROM messages
            WHERE thread = ANY(%s) AND kind = ANY(%s)
            ORDER BY id DESC LIMIT %s
            """,
            (threads, list(CONVERSATION_KINDS), JOURNAL_MESSAGES * len(threads)),
        ).fetchall()

    by_decision: dict[int, list[dict]] = {}
    for p in positions:
        by_decision.setdefault(p["decision_id"], []).append(p)
    msgs_by_thread: dict[str, list[dict]] = {}
    for m in messages:
        msgs_by_thread.setdefault(m["thread"], []).append(m)

    out = []
    for r in rows:
        seats = []
        for seat in r["heads"]:
            mine = [p for p in by_decision.get(r["id"], []) if p["head"] == seat]
            latest = max(mine, key=lambda p: p["round"], default=None)
            seats.append({
                "seat": seat,
                "position": latest["position"] if latest else None,
                "voted": bool(latest and latest["round"] == r["round"]),
                "conditions": list(latest["conditions"]) if latest and latest["conditions"] else None,
                "body": latest["body"] if latest else None,
            })
        journal = [
            {
                "author": m["author"], "kind": m["kind"], "body": m["body"],
                "created_at": m["created_at"].isoformat(),
            }
            for m in reversed(msgs_by_thread.get(r["thread"], [])[:JOURNAL_MESSAGES])
        ]
        out.append({
            "id": r["id"], "title": r["title"], "artifact": r["artifact"],
            "protocol": r["protocol"],
            "status": r["status"], "ruling": r["ruling"],
            "confidence": r["confidence"], "round": r["round"],
            "thread": r["thread"], "badge": verdict_badge(r),
            "seats": seats, "journal": journal,
        })

    chat_rows = conn.execute(
        """
        SELECT author, kind, body, created_at FROM messages
        WHERE thread = %s ORDER BY id DESC LIMIT %s
        """,
        (CHAT_THREAD, CHAT_MESSAGES),
    ).fetchall()
    chat = [
        {
            "author": m["author"], "kind": m["kind"], "body": m["body"],
            "created_at": m["created_at"].isoformat(),
        }
        for m in reversed(chat_rows)
    ]
    return {"decisions": out, "chat": chat}


def sse_frame(data: dict) -> bytes:
    return b"data: " + json.dumps(data, ensure_ascii=False).encode("utf-8") + b"\n\n"


def _broadcast(state: dict) -> None:
    frame = sse_frame(state)
    with CLIENTS_LOCK:
        clients = list(CLIENTS)
    for q in clients:
        q.put(frame)


def _listener(stop: threading.Event) -> None:
    """LISTEN/NOTIFY → broadcast. Se reintenta solo si Postgres se cae."""
    backoff = 1
    while not stop.is_set():
        try:
            with connect() as conn:
                conn.execute("LISTEN debate_all")
                conn.execute("LISTEN decision_all")
                backoff = 1
                while not stop.is_set():
                    _broadcast(build_state(conn))
                    for _n in conn.notifies(timeout=POLL_SECS, stop_after=1):
                        break
        except Exception:
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    # ---------------------------------------------------------- helpers

    def _send_json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, name: str, content_type: str) -> None:
        body = (UI_DIR / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------------------------------------------------------- GET

    def do_GET(self) -> None:
        if self.path == "/":
            self._send_file("index.html", "text/html; charset=utf-8")
        elif self.path == "/style.css":
            self._send_file("style.css", "text/css; charset=utf-8")
        elif self.path == "/app.js":
            self._send_file("app.js", "text/javascript; charset=utf-8")
        elif self.path == "/state":
            with connect() as conn:
                self._send_json(build_state(conn))
        elif self.path == "/events":
            self._events()
        else:
            self._send_json({"error": "not found"}, 404)

    def _events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        q: queue.Queue = queue.Queue()
        with CLIENTS_LOCK:
            CLIENTS.append(q)
        try:
            with connect() as conn:
                self.wfile.write(sse_frame(build_state(conn)))
            self.wfile.flush()
            while True:
                try:
                    frame = q.get(timeout=30)
                except queue.Empty:
                    frame = b": ping\n\n"
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with CLIENTS_LOCK:
                if q in CLIENTS:
                    CLIENTS.remove(q)

    # ---------------------------------------------------------- POST

    def do_POST(self) -> None:
        if self.path not in ("/start", "/message"):
            self._send_json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            # application/json sin charset es UTF-8 por RFC 8259: un body en
            # otra codificación es 400, no un handler explotado sin respuesta
            # (era lo que pasaba con json.loads directo sobre los bytes).
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "JSON inválido (se espera UTF-8)"}, 400)
            return
        if self.path == "/start":
            self._start(payload)
        else:
            self._message(payload)

    def _start(self, payload: dict) -> None:
        try:
            with connect() as conn:
                with conn.transaction():
                    result = board.start_decision(
                        conn,
                        title=payload.get("title", ""),
                        artifact=payload.get("artifact"),
                        protocol=payload.get("protocol") or "vote",
                    )
            self._send_json(result, 201)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)

    def _message(self, payload: dict) -> None:
        """El chat de la UI: una sola caja de texto, el destino lo elige el
        estado del sistema — cero protocolo que memorizar.

        mode="council": si hay una decisión abierta, el texto es CONTEXTO
        para las cabezas (lo leen en su próximo turno); si está en STALEMATE,
        es el ARBITRAJE humano que la cierra; si no hay ninguna, el texto
        ABRE una decisión MAGI (protocolo adaptive). mode="chat": charla
        libre con las tres cabezas en el thread compartido. mode="message":
        destino explícito (API; compat con clientes externos).
        """
        body = (payload.get("body") or "").strip()
        if not body:
            self._send_json({"error": "falta el mensaje"}, 400)
            return
        mode = payload.get("mode") or "council"
        try:
            if mode == "decision":
                with connect() as conn:
                    with conn.transaction():
                        result = board.start_decision(
                            conn,
                            title=body,
                            artifact=payload.get("artifact"),
                            protocol=payload.get("protocol") or "adaptive",
                        )
                self._send_json({**result, "kind": "decision", "action": "opened"}, 201)
                return
            if mode == "chat":
                thread = payload.get("thread") or CHAT_THREAD
                with connect() as conn:
                    with conn.transaction():
                        result = board.human_message(conn, thread, body)
                self._send_json({**result, "action": "chat"}, 201)
                return
            if mode == "message":
                thread = payload.get("thread") or CHAT_THREAD
                with connect() as conn:
                    with conn.transaction():
                        result = board.human_message(conn, thread, body)
                if result.get("reopened_decision"):
                    result = {**result, "decision_id": result["reopened_decision"], "action": "reopened"}
                else:
                    result = {**result, "action": result["kind"]}
                self._send_json(result, 201)
                return
            # council: el sistema elige el destino por estado
            with connect() as conn:
                with conn.transaction():
                    d = conn.execute(
                        """
                        SELECT id, thread, status FROM decisions
                        WHERE status IN ('open', 'split')
                        ORDER BY id DESC LIMIT 1
                        """
                    ).fetchone()
                    if d is None:
                        result = board.start_decision(
                            conn, title=body,
                            protocol=payload.get("protocol") or "adaptive",
                        )
                        result = {**result, "kind": "decision", "action": "opened"}
                    else:
                        result = board.human_message(conn, d["thread"], body)
                        if result.get("reopened_decision"):
                            result = {
                                **result, "decision_id": result["reopened_decision"],
                                "kind": "decision", "action": "reopened",
                            }
                        else:
                            result = {
                                **result, "decision_id": d["id"], "kind": "decision",
                                "action": {
                                    "arbitraje": "arbitrated",
                                    "contexto": "context",
                                }.get(result["kind"], result["kind"]),
                            }
            self._send_json(result, 201)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)


def main() -> None:
    stop = threading.Event()
    threading.Thread(target=_listener, args=(stop,), daemon=True).start()
    print(f"[magi_ui] MAGI System en http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
