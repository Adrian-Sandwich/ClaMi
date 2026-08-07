"""Camina ~/.claude/projects/**/*.jsonl (incluye subagents/, que reusan el
sessionId de la sesión padre). Un nodo por sesión, un nodo por proyecto (cwd),
edges touched (Read/Edit/Write/NotebookEdit -> archivo) y posted_to (llamadas
mcp__debate__* -> thread).

Varios archivos .jsonl pueden compartir el mismo sessionId (transcripts de
subagentes) — por eso se extraen los hechos de CADA archivo primero y se
agregan por sessionId antes de escribir a la DB, en vez de escribir por
archivo (que pisaría los edges del anterior por el mismo id)."""

import json
from datetime import datetime, timezone
from pathlib import Path

import db
import ingest_debate
from code_lookup import CodeIndex

SOURCE = "ingest_claude"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
FILE_PATH_KEYS = ("file_path", "path", "notebook_path")


def project_id(cwd: str) -> str:
    return f"project:{cwd}"


def session_id(sid: str) -> str:
    return f"claude_session:{sid}"


def resolve_file_node(code_index: CodeIndex, abs_path: str) -> str:
    resolved = code_index.resolve_file(abs_path)
    return resolved if resolved else f"file:{abs_path}"


def extract_facts(path: Path) -> dict | None:
    sid = cwd = ai_title = first_user_text = None
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

            sid = sid or d.get("sessionId")
            cwd = cwd or d.get("cwd")
            ts = d.get("timestamp")
            if ts:
                first_ts = first_ts or ts
                last_ts = ts

            t = d.get("type")
            if t == "ai-title":
                ai_title = d.get("aiTitle")
                continue
            if t not in ("user", "assistant"):
                continue

            n_turns += 1
            content = d.get("message", {}).get("content")
            if t == "user" and first_user_text is None and isinstance(content, str):
                first_user_text = content

            if t == "assistant" and isinstance(content, list):
                for block in content:
                    if block.get("type") != "tool_use":
                        continue
                    name = block.get("name", "")
                    inp = block.get("input", {}) or {}
                    for key in FILE_PATH_KEYS:
                        if key in inp:
                            touched[inp[key]] = touched.get(inp[key], 0) + 1
                            break
                    if name.startswith("mcp__debate__") and "thread" in inp:
                        threads.add(inp["thread"])

    if sid is None:
        return None

    return {
        "sid": sid, "cwd": cwd, "ai_title": ai_title, "first_user_text": first_user_text,
        "n_turns": n_turns, "first_ts": first_ts, "last_ts": last_ts,
        "touched": touched, "threads": threads, "bad_lines": n_bad_lines,
    }


def merge(facts_list: list[dict]) -> dict:
    m = {
        "cwd": None, "ai_title": None, "first_user_text": None, "n_turns": 0,
        "first_ts": None, "last_ts": None, "touched": {}, "threads": set(), "bad_lines": 0,
    }
    for f in facts_list:
        m["cwd"] = m["cwd"] or f["cwd"]
        m["ai_title"] = m["ai_title"] or f["ai_title"]
        m["first_user_text"] = m["first_user_text"] or f["first_user_text"]
        m["n_turns"] += f["n_turns"]
        m["bad_lines"] += f["bad_lines"]
        if f["first_ts"] and (m["first_ts"] is None or f["first_ts"] < m["first_ts"]):
            m["first_ts"] = f["first_ts"]
        if f["last_ts"] and (m["last_ts"] is None or f["last_ts"] > m["last_ts"]):
            m["last_ts"] = f["last_ts"]
        for path_, count in f["touched"].items():
            m["touched"][path_] = m["touched"].get(path_, 0) + count
        m["threads"] |= f["threads"]
    return m


def write_session(conn, code_index: CodeIndex, sid: str, m: dict, now: str) -> None:
    cwd = m["cwd"] or "unknown"
    label = m["ai_title"] or (m["first_user_text"][:80] if m["first_user_text"] else sid)
    pid = project_id(cwd)
    db.upsert_node(conn, id=pid, domain="project", source=SOURCE, updated_at=now, label=Path(cwd).name, tag="Project")

    sid_node = session_id(sid)
    db.upsert_node(
        conn, id=sid_node, domain="claude_session", source=SOURCE, updated_at=now,
        label=label, tag="ClaudeSession",
        size=min(30, max(6, 6 + m["n_turns"] // 5)),
        tooltip=f"{m['n_turns']} turnos, {m['first_ts']} - {m['last_ts']}",
        props={"cwd": cwd, "n_turns": m["n_turns"], "first_ts": m["first_ts"], "last_ts": m["last_ts"], "bad_lines": m["bad_lines"]},
    )

    db.reset_source_edges(conn, SOURCE, sid_node)
    db.upsert_edge(conn, sid_node, pid, "belongs_to", source=SOURCE, updated_at=now)
    for fpath, count in m["touched"].items():
        target = resolve_file_node(code_index, fpath)
        if not target.startswith("code:"):
            db.upsert_node(conn, id=target, domain="file", source=SOURCE, updated_at=now, label=Path(fpath).name, tag="File")
        db.upsert_edge(conn, sid_node, target, "touched", source=SOURCE, updated_at=now, weight=float(count))
    for thread in m["threads"]:
        db.upsert_edge(conn, sid_node, ingest_debate.node_id(thread), "posted_to", source=SOURCE, updated_at=now)


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = db.connect()
    code_index = CodeIndex()

    by_sid: dict[str, list[dict]] = {}
    n_files = n_empty = 0
    for jsonl_path in PROJECTS_DIR.rglob("*.jsonl"):
        n_files += 1
        facts = extract_facts(jsonl_path)
        if facts is None:
            n_empty += 1
            continue
        by_sid.setdefault(facts["sid"], []).append(facts)

    for sid, facts_list in by_sid.items():
        write_session(conn, code_index, sid, merge(facts_list), now)

    code_index.close()
    conn.commit()
    conn.close()
    print(f"[ingest_claude] {n_files} archivos ({n_empty} vacíos/sin sessionId) -> {len(by_sid)} sesiones")


if __name__ == "__main__":
    main()
