"""Tests de la lógica de disparo del relay.

Escritos a partir de una falla real: el 2026-08-26, durante el debate
`clami-mejoras`, el relay generó 15 mensajes de más. Dos agentes postearon casi
a la vez, el relay disparó un proceso por CADA fila nueva en vez de uno por
thread, y cada ronda duplicó la cantidad de agentes. No había tope, así que
sólo se detuvo por casualidad, cuando dos veredictos consecutivos de autores
distintos cayeron uno detrás del otro.

Cada test de acá corresponde a una de esas causas.
"""

import json

import pytest

import relay


class FakeConn:
    """Postgres de mentira: responde las tres queries que usa el ciclo."""

    def __init__(self, messages):
        self.messages = messages  # [{id, thread, author, kind, artifact}]

    def execute(self, query, params=()):
        q = " ".join(query.split())
        if q.startswith("SELECT id, thread, author, kind"):
            rows = [m for m in self.messages if m["id"] > params[0]]
            rows.sort(key=lambda m: m["id"])
        elif q.startswith("SELECT author, kind"):
            rows = sorted(
                (m for m in self.messages if m["thread"] == params[0]),
                key=lambda m: -m["id"],
            )[:2]
        elif q.startswith("SELECT artifact"):
            rows = [
                m for m in sorted(self.messages, key=lambda m: m["id"])
                if m["thread"] == params[0] and m.get("artifact")
            ][:5]
        else:
            raise AssertionError(f"query inesperada: {q}")
        return _Result(rows)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


def msg(id, thread="t", author="kimi", kind="critica", artifact=None):
    return {"id": id, "thread": thread, "author": author, "kind": kind, "artifact": artifact}


@pytest.fixture
def fired(monkeypatch, tmp_path):
    """Aísla el estado en disco y captura los disparos en vez de ejecutarlos."""
    monkeypatch.setattr(relay, "STATE_PATH", tmp_path / "relay_state.json")
    monkeypatch.setattr(relay, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(relay, "HEARTBEAT_PATH", tmp_path / "hb.json")
    relay._inflight.clear()

    calls = []

    def fake_trigger(author_to_call, thread, since_id, other_author, cwd):
        calls.append({
            "author": author_to_call, "thread": thread,
            "since_id": since_id, "other": other_author, "cwd": cwd,
        })
        return True

    monkeypatch.setattr(relay, "trigger", fake_trigger)
    return calls


def fresh_state():
    return {"last_id": 0, "threads": {}, "pending": []}


# ------------------------------------------------- cierre de thread

def test_thread_abierto_dispara_al_otro_analista(fired):
    conn = FakeConn([msg(1, author="kimi", kind="critica")])
    relay.process_cycle(conn, fresh_state())
    assert len(fired) == 1
    assert fired[0]["author"] == "claude"
    assert fired[0]["since_id"] == 0   # id-1: read_thread devuelve id > since_id


def test_veredictos_cruzados_cierran_el_thread(fired):
    conn = FakeConn([
        msg(1, author="kimi", kind="veredicto"),
        msg(2, author="claude", kind="veredicto"),
    ])
    relay.process_cycle(conn, fresh_state())
    assert fired == []


def test_dos_veredictos_del_mismo_autor_no_cierran(fired):
    conn = FakeConn([
        msg(1, author="kimi", kind="veredicto"),
        msg(2, author="kimi", kind="veredicto"),
    ])
    relay.process_cycle(conn, fresh_state())
    assert len(fired) == 1


def test_arbitraje_cierra_el_thread(fired):
    conn = FakeConn([
        msg(1, author="kimi", kind="critica"),
        msg(2, author="adrian", kind="arbitraje"),
    ])
    relay.process_cycle(conn, fresh_state())
    assert fired == []


def test_mensaje_de_adrian_no_dispara(fired):
    conn = FakeConn([msg(1, author="adrian", kind="analisis")])
    relay.process_cycle(conn, fresh_state())
    assert fired == []


# ------------------------------------------------- B2: colapsar la ráfaga

def test_varios_mensajes_del_mismo_thread_disparan_una_sola_vez(fired):
    """La causa de los 15 mensajes de más: dos agentes posteando casi a la vez
    (o un backlog tras una caída) generaban un proceso por fila."""
    conn = FakeConn([
        msg(1, author="kimi", kind="critica"),
        msg(2, author="claude", kind="respuesta"),
        msg(3, author="claude", kind="respuesta"),
    ])
    relay.process_cycle(conn, fresh_state())
    assert len(fired) == 1
    assert fired[0]["since_id"] == 2   # responde al último, no al primero
    assert fired[0]["author"] == "kimi"


def test_threads_distintos_disparan_por_separado(fired):
    conn = FakeConn([
        msg(1, thread="a", author="kimi"),
        msg(2, thread="b", author="claude"),
    ])
    relay.process_cycle(conn, fresh_state())
    assert sorted(c["thread"] for c in fired) == ["a", "b"]


def test_thread_con_agente_corriendo_se_encola(fired):
    state = fresh_state()
    relay._inflight.add("t")
    try:
        relay.process_cycle(FakeConn([msg(1)]), state)
    finally:
        relay._inflight.discard("t")
    assert fired == []
    assert [p["id"] for p in state["pending"]] == [1]


# ------------------------------------------------- B1: tope de rondas

def test_tope_de_disparos_corta_el_loop(fired):
    state = fresh_state()
    conn = FakeConn([msg(1)])
    state["threads"]["t"] = {"triggers": relay.MAX_TRIGGERS_PER_THREAD, "cwd": "/tmp"}

    relay.process_cycle(conn, state)
    assert fired == []


def test_un_analisis_nuevo_reinicia_el_tope(fired):
    state = fresh_state()
    state["threads"]["t"] = {"triggers": relay.MAX_TRIGGERS_PER_THREAD, "cwd": "/tmp"}
    conn = FakeConn([msg(1, author="kimi", kind="analisis", artifact="/tmp")])

    relay.process_cycle(conn, state)
    assert len(fired) == 1
    assert state["threads"]["t"]["triggers"] == 1


def test_los_disparos_se_cuentan(fired):
    state = fresh_state()
    relay.process_cycle(FakeConn([msg(1)]), state)
    assert state["threads"]["t"]["triggers"] == 1


# ------------------------------------------------- B4: no perder turnos

def test_un_disparo_que_no_arranca_queda_pendiente(monkeypatch, tmp_path):
    monkeypatch.setattr(relay, "STATE_PATH", tmp_path / "s.json")
    monkeypatch.setattr(relay, "EVENTS_PATH", tmp_path / "e.jsonl")
    relay._inflight.clear()
    monkeypatch.setattr(relay, "trigger", lambda *a, **kw: False)

    state = fresh_state()
    relay.process_cycle(FakeConn([msg(1)]), state)

    assert [p["id"] for p in state["pending"]] == [1]
    assert state["threads"]["t"]["triggers"] == 0, "un disparo fallido no gasta cupo"


def test_lo_pendiente_se_reintenta_en_el_ciclo_siguiente(fired):
    state = fresh_state()
    state["last_id"] = 1
    state["pending"] = [{"id": 1, "thread": "t", "author": "kimi"}]

    relay.process_cycle(FakeConn([msg(1)]), state)
    assert len(fired) == 1
    assert state["pending"] == []


def test_un_mensaje_nuevo_pisa_lo_pendiente_del_mismo_thread(fired):
    state = fresh_state()
    state["pending"] = [{"id": 1, "thread": "t", "author": "kimi"}]
    relay.process_cycle(FakeConn([msg(1), msg(2, author="claude")]), state)

    assert len(fired) == 1
    assert fired[0]["since_id"] == 1   # el mensaje 2, no el 1


def test_el_watermark_avanza(fired):
    state = fresh_state()
    relay.process_cycle(FakeConn([msg(1), msg(2, thread="otro")]), state)
    assert state["last_id"] == 2


# ------------------------------------------------- A3: cwd por thread

def test_cwd_sale_del_artifact_del_thread(fired, tmp_path):
    repo = tmp_path / "proyecto"
    (repo / ".git").mkdir(parents=True)
    conn = FakeConn([msg(1, artifact=f"{repo}/src/x.py:42")])

    relay.process_cycle(conn, fresh_state())
    assert fired[0]["cwd"] == str(repo)


def test_cwd_cae_al_default_sin_artifact(fired):
    relay.process_cycle(FakeConn([msg(1)]), fresh_state())
    assert fired[0]["cwd"] == relay.DEFAULT_CWD


def test_cwd_from_artifact_ignora_rutas_relativas():
    assert relay.cwd_from_artifact("src/paper.rs:120") is None
    assert relay.cwd_from_artifact(None) is None
    assert relay.cwd_from_artifact("") is None


def test_cwd_from_artifact_sube_hasta_la_raiz_del_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "a" / "b").mkdir(parents=True)
    archivo = repo / "a" / "b" / "x.py"
    archivo.write_text("")
    assert relay.cwd_from_artifact(str(archivo)) == str(repo)


def test_thread_cwd_manual_gana_sobre_el_artifact(fired, monkeypatch, tmp_path):
    repo = tmp_path / "proyecto"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setitem(relay.THREAD_CWD, "t", "/override")
    relay.process_cycle(FakeConn([msg(1, artifact=str(repo))]), fresh_state())
    assert fired[0]["cwd"] == "/override"


# ------------------------------------------------- estado y heartbeat

def test_el_estado_sobrevive_una_vuelta_completa(fired, tmp_path, monkeypatch):
    state = fresh_state()
    relay.process_cycle(FakeConn([msg(1)]), state)
    reloaded = relay.load_state()
    assert reloaded["last_id"] == 1
    assert reloaded["threads"]["t"]["triggers"] == 1


def test_load_state_completa_un_estado_viejo(monkeypatch, tmp_path):
    """El formato anterior era sólo {"last_id": N}."""
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"last_id": 42}))
    monkeypatch.setattr(relay, "STATE_PATH", path)

    state = relay.load_state()
    assert state == {"last_id": 42, "threads": {}, "pending": []}


def test_heartbeat_reporta_los_threads_frenados(fired, tmp_path, monkeypatch):
    monkeypatch.setattr(relay, "HEARTBEAT_PATH", tmp_path / "hb.json")
    state = fresh_state()
    state["threads"]["t"] = {"triggers": relay.MAX_TRIGGERS_PER_THREAD, "cwd": None}

    relay.write_heartbeat(state, pg_ok=True)
    hb = json.loads((tmp_path / "hb.json").read_text())
    assert hb["capped_threads"] == ["t"]
    assert hb["pg_ok"] is True
