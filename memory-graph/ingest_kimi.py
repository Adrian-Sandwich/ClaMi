"""Camina ~/.kimi-code/sessions/wd_<slug>/session_<uuid>/agents/*/wire.jsonl.
No confía en session_index.jsonl como enumeración (se vio desactualizado —
11 líneas mientras había sesiones nuevas en disco no listadas); enumera
caminando los directorios reales y resuelve el proyecto vía workspaces.json.

Nota de compatibilidad: distintas versiones de kimi-code escriben distinto
wire.jsonl. Versiones viejas tienen eventos `tool.call` explícitos (source de
los edges touched/posted_to); la build actual (0.34.0, verificado en vivo)
no los emite en este log — solo turn.prompt/content.part. El ingestor soporta
ambos formatos pero en sesiones nuevas puede no haber edges touched/posted_to,
solo el nodo de sesión con label/timestamps. Documentado, no es un bug."""

import json
from datetime import datetime, timezone
from pathlib import Path

import db
import ingest_debate
from code_lookup import CodeIndex
from ingest_claude import FILE_PATH_KEYS, project_id, resolve_file_node

SOURCE = "ingest_kimi"
KIMI_HOME = Path.home() / ".kimi-code"
SESSIONS_DIR = KIMI_HOME / "sessions"


def load_workspaces() -> dict:
    data = json.loads((KIMI_HOME / "workspaces.json").read_text())
    return {slug: w["root"] for slug, w in data.get("workspaces", {}).items()}


def kimi_session_id(sid: str) -> str:
    return f"kimi_session:{sid}"


def extract_agent_facts(path: Path) -> dict:
    first_prompt = None
    n_turns = 0
    first_ts = last_ts = None
    touched: dict[str, int] = {}
    threads: set[str] = set()
    n_bad_lines = 0

    with path.open(errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                n_bad_lines += 1
                continue

            ts = d.get("time")
            if ts:
                first_ts = first_ts if first_ts is not None else ts
                last_ts = ts

            t = d.get("type")
            if t == "turn.prompt":
                n_turns += 1
                if first_prompt is None:
                    for block in d.get("input", []) or []:
                        if block.get("type") == "text":
                            first_prompt = block["text"]
                            break
                continue

            if t != "context.append_loop_event":
                continue
            ev = d.get("event", {})
            et = ev.get("type")
            if et == "tool.call":
                # formato viejo de kimi-code; puede no existir en builds nuevas
                name = ev.get("name", "")
                args = ev.get("args", {}) or {}
                for key in FILE_PATH_KEYS:
                    if key in args:
                        touched[args[key]] = touched.get(args[key], 0) + 1
                        break
                if name in ("post_message", "read_thread", "wait_messages") and "thread" in args:
                    threads.add(args["thread"])

    return {
        "first_prompt": first_prompt, "n_turns": n_turns,
        "first_ts": first_ts, "last_ts": last_ts,
        "touched": touched, "threads": threads, "bad_lines": n_bad_lines,
    }


def merge(facts_list: list[dict]) -> dict:
    m = {"first_prompt": None, "n_turns": 0, "first_ts": None, "last_ts": None,
         "touched": {}, "threads": set(), "bad_lines": 0}
    for f in facts_list:
        m["first_prompt"] = m["first_prompt"] or f["first_prompt"]
        m["n_turns"] += f["n_turns"]
        m["bad_lines"] += f["bad_lines"]
        if f["first_ts"] and (m["first_ts"] is None or f["first_ts"] < m["first_ts"]):
            m["first_ts"] = f["first_ts"]
        if f["last_ts"] and (m["last_ts"] is None or f["last_ts"] > m["last_ts"]):
            m["last_ts"] = f["last_ts"]
        for p, c in f["touched"].items():
            m["touched"][p] = m["touched"].get(p, 0) + c
        m["threads"] |= f["threads"]
    return m


def epoch_ms_to_iso(ms) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = db.connect()
    code_index = CodeIndex()
    workspaces = load_workspaces()

    n_sessions = 0
    for workspace_dir in SESSIONS_DIR.glob("wd_*"):
        cwd = workspaces.get(workspace_dir.name)
        if not cwd:
            continue  # workspace borrado/desconocido, sin root_path confiable
        for session_dir in workspace_dir.glob("session_*"):
            sid = session_dir.name.removeprefix("session_")
            wire_files = list(session_dir.glob("agents/*/wire.jsonl"))
            if not wire_files:
                continue
            facts_list = [extract_agent_facts(p) for p in wire_files]
            m = merge(facts_list)

            pid = project_id(cwd)
            db.upsert_node(conn, id=pid, domain="project", source=SOURCE, updated_at=now, label=Path(cwd).name, tag="Project")

            sid_node = kimi_session_id(sid)
            label = m["first_prompt"][:80] if m["first_prompt"] else sid
            db.upsert_node(
                conn, id=sid_node, domain="kimi_session", source=SOURCE, updated_at=now,
                label=label, tag="KimiSession",
                size=min(30, max(6, 6 + m["n_turns"] // 2)),
                tooltip=f"{m['n_turns']} turnos, {epoch_ms_to_iso(m['first_ts'])} - {epoch_ms_to_iso(m['last_ts'])}",
                props={
                    "cwd": cwd, "n_turns": m["n_turns"], "n_agents": len(wire_files),
                    "first_ts": epoch_ms_to_iso(m["first_ts"]), "last_ts": epoch_ms_to_iso(m["last_ts"]),
                    "bad_lines": m["bad_lines"],
                },
            )

            db.reset_source_edges(conn, SOURCE, sid_node)
            db.upsert_edge(conn, sid_node, pid, "belongs_to", source=SOURCE, updated_at=now)
            for fpath, count in m["touched"].items():
                abs_path = fpath if Path(fpath).is_absolute() else str(Path(cwd) / fpath)
                target = resolve_file_node(code_index, abs_path)
                if not target.startswith("code:"):
                    db.upsert_node(conn, id=target, domain="file", source=SOURCE, updated_at=now, label=Path(fpath).name, tag="File")
                db.upsert_edge(conn, sid_node, target, "touched", source=SOURCE, updated_at=now, weight=float(count))
            for thread in m["threads"]:
                db.upsert_edge(conn, sid_node, ingest_debate.node_id(thread), "posted_to", source=SOURCE, updated_at=now)

            n_sessions += 1

    code_index.close()
    conn.commit()
    conn.close()
    print(f"[ingest_kimi] {n_sessions} sesiones")


if __name__ == "__main__":
    main()
