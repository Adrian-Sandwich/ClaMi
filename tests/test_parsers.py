"""Tests de los dos parsers de logs de sesión.

Dos capas, a propósito:

- Sobre fixtures sintéticas: qué tiene que extraer el parser, congelado.
- Sobre los logs REALES del disco (`test_canary_*`): que los eventos de los que
  el parser depende sigan existiendo. Esta es la parte que importa. Durante
  semanas el docstring de `ingest_kimi.py` afirmó que la build actual de
  kimi-code ya no emitía `tool.call`, dos analistas lo dieron por cierto, y al
  medirlo resultó falso. Una nota en prosa no revalida nada; un test que corre
  contra el log de ayer, sí.
"""

import pytest

import ingest_claude
import ingest_kimi


# ------------------------------------------------------------ claude

def test_claude_extrae_sesion(fixtures_dir):
    facts = ingest_claude.extract_facts(fixtures_dir / "claude_session.jsonl")

    assert facts["sid"] == "SESSION-A"
    assert facts["cwd"] == "/tmp/fake-repo"
    assert facts["ai_title"] == "Arreglo del parser"
    assert facts["first_user_text"] == "arreglá el parser"
    # user + 3 assistant; 'ai-title' y 'summary' no son turnos
    assert facts["n_turns"] == 4
    assert facts["first_ts"] == "2026-01-01T00:00:00Z"
    assert facts["last_ts"] == "2026-01-01T00:03:00Z"


def test_claude_cuenta_archivos_tocados(fixtures_dir):
    facts = ingest_claude.extract_facts(fixtures_dir / "claude_session.jsonl")
    assert facts["touched"] == {
        "/tmp/fake-repo/a.py": 2,          # Read + Edit
        "/tmp/fake-repo/nb.ipynb": 1,      # notebook_path también cuenta
    }


def test_claude_detecta_threads_del_tablero(fixtures_dir):
    facts = ingest_claude.extract_facts(fixtures_dir / "claude_session.jsonl")
    assert facts["threads"] == ["hilo-uno"]


def test_claude_tolera_lineas_corruptas(fixtures_dir):
    facts = ingest_claude.extract_facts(fixtures_dir / "claude_session.jsonl")
    assert facts["bad_lines"] == 1


def test_claude_archivo_sin_session_id_devuelve_none(tmp_path):
    path = tmp_path / "vacio.jsonl"
    path.write_text('{"type":"system","subtype":"init"}\n')
    assert ingest_claude.extract_facts(path) is None


def test_claude_merge_suma_transcripts_de_subagentes(fixtures_dir):
    """Varios .jsonl comparten sessionId (los transcripts de subagentes); hay
    que agregarlos, no dejar que el último pise al anterior."""
    facts = ingest_claude.extract_facts(fixtures_dir / "claude_session.jsonl")
    merged = ingest_claude.merge([facts, facts])
    assert merged["n_turns"] == facts["n_turns"] * 2
    assert merged["touched"]["/tmp/fake-repo/a.py"] == 4
    assert merged["threads"] == {"hilo-uno"}
    assert merged["ai_title"] == facts["ai_title"]


# ------------------------------------------------------------ kimi

def test_kimi_extrae_sesion(fixtures_dir):
    facts = ingest_kimi.extract_agent_facts(fixtures_dir / "kimi_wire.jsonl")

    assert facts["n_turns"] == 2
    assert facts["first_prompt"] == "revisá el ingestor de kimi"
    assert facts["first_ts"] == 1767225600000
    assert facts["last_ts"] == 1767225900000
    assert facts["bad_lines"] == 1


def test_kimi_cuenta_archivos_y_threads(fixtures_dir):
    facts = ingest_kimi.extract_agent_facts(fixtures_dir / "kimi_wire.jsonl")
    assert facts["touched"] == {
        "memory-graph/ingest_kimi.py": 1,   # relativo, se absolutiza con el cwd después
        "/abs/otro.py": 1,
    }
    assert facts["threads"] == ["hilo-uno"]


def test_kimi_epoch_ms_a_iso():
    assert ingest_kimi.epoch_ms_to_iso(None) is None
    assert ingest_kimi.epoch_ms_to_iso(0) is None       # 0 es "sin timestamp"
    assert ingest_kimi.epoch_ms_to_iso(1767225600000).startswith("2026-01-01T")


# ------------------------------------------------ canarios de formato

def _walk_claude(root, limit=40):
    return sorted(root.rglob("*.jsonl"), key=lambda p: -p.stat().st_mtime)[:limit]


def test_canary_claude_sigue_emitiendo_tool_use(real_claude_logs):
    """Si Claude Code cambia el shape de `message.content`, los edges
    `touched` desaparecen del grafo sin ruido. Esto lo hace ruidoso."""
    parsed = [ingest_claude.extract_facts(p) for p in _walk_claude(real_claude_logs)]
    parsed = [f for f in parsed if f]
    assert parsed, "ningún transcript reciente se pudo parsear"

    assert sum(f["n_turns"] for f in parsed) > 0, "ningún turno: cambió `type` user/assistant"
    assert sum(len(f["touched"]) for f in parsed) > 0, (
        "ningún archivo tocado en los transcripts recientes: probablemente cambió "
        "el shape de los bloques tool_use o las claves de FILE_PATH_KEYS"
    )
    assert any(f["cwd"] for f in parsed), "ningún cwd: cambió la clave del transcript"


def test_canary_kimi_sigue_emitiendo_tool_call(real_kimi_logs):
    wires = sorted(
        real_kimi_logs.glob("wd_*/session_*/agents/*/wire.jsonl"),
        key=lambda p: -p.stat().st_mtime,
    )[:40]
    parsed = [ingest_kimi.extract_agent_facts(p) for p in wires]
    assert parsed, "ningún wire.jsonl reciente"

    assert sum(f["n_turns"] for f in parsed) > 0, (
        "ningún `turn.prompt` en los wire.jsonl recientes: kimi-code cambió el "
        "formato y las sesiones nuevas entrarían al grafo sin label ni tamaño"
    )
    assert sum(len(f["touched"]) for f in parsed) > 0, (
        "ningún evento `tool.call` con path: las sesiones de Kimi entrarían al "
        "grafo sin edges `touched`"
    )


def test_canary_workspaces_de_kimi_resuelven(real_kimi_logs):
    """`load_workspaces()` es lo que le da cwd a cada sesión de Kimi; si el
    formato de workspaces.json cambia, las sesiones se descartan en silencio."""
    try:
        workspaces = ingest_kimi.load_workspaces()
    except (OSError, KeyError) as exc:
        pytest.fail(f"workspaces.json ilegible: {exc!r}")

    dirs = {d.name for d in real_kimi_logs.glob("wd_*")}
    resueltos = dirs & set(workspaces)
    assert resueltos, (
        f"ninguno de los {len(dirs)} workspaces en disco aparece en workspaces.json: "
        f"todas las sesiones de Kimi se estarían descartando"
    )
