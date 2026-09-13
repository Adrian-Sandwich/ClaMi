"""Tests de la lógica de disparo del relay.

Escritos a partir de una falla real: el 2026-08-26, durante el debate
`clami-mejoras`, el relay generó 15 mensajes de más. Dos agentes postearon casi a
la vez, el relay disparó un proceso por CADA fila nueva en vez de uno por
thread, y cada ronda duplicó la cantidad de agentes. No había tope, así que
sólo se detuvo por casualidad, cuando dos veredictos consecutivos de autores
distintos cayeron uno detrás del otro.

Cada test de acá corresponde a una de esas causas. La sección de decisiones
MAGI cubre lo nuevo: el relay ya no calcula "el otro" a pulso; pide los turnos
pendientes al motor (decision.py) y dispara los asientos que faltan.

Los tests de threads libres usan un registry con asientos 'kimi' y 'claude'
(el mundo anterior al registry MAGI); los de decisiones usan los asientos
canónicos. Ningún test toca el registry real del repo.
"""

import json
import sys
import threading

import pytest

import heads
import relay


class FakeConn:
    """Postgres de mentira: responde las queries que usa el ciclo."""

    def __init__(self, messages, decisions=None, positions=None):
        self.messages = messages  # [{id, thread, author, kind, artifact}]
        self.decisions = decisions or []  # filas de decisions (dicts)
        self.positions = positions or []  # filas de positions (dicts)

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
        elif q.startswith("SELECT author, kind, body"):
            rows = []
        elif q.startswith("SELECT id, title, artifact") and "'open'" in q:
            rows = [d for d in self.decisions if d.get("status") == "open"]
        elif q.startswith("SELECT id, title, artifact"):
            # fetch_executing_decisions (modo producción): en los tests, ninguna
            rows = [d for d in self.decisions if d.get("status") == "executing"]
        elif q.startswith("SELECT id, title, thread, ruling, confidence"):
            # _maybe_merge_reviews: en los tests, ninguna revisión cerrada
            rows = []
        elif q.startswith("SELECT 1 FROM messages"):
            # _ejecucion_gestionada / marca de merge: en los tests, nada
            rows = []
        elif q.startswith("SELECT decision_id, head, round, position"):
            ids = params[0]
            rows = [p for p in self.positions if p["decision_id"] in ids]
        else:
            raise AssertionError(f"query inesperada: {q}")
        return _Result(rows)

    def transaction(self):
        return _Tx()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Tx:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def msg(id, thread="t", author="kimi", kind="critica", artifact=None):
    return {"id": id, "thread": thread, "author": author, "kind": kind, "artifact": artifact}


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(relay, "STATE_PATH", tmp_path / "relay_state.json")
    monkeypatch.setattr(relay, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(relay, "HEARTBEAT_PATH", tmp_path / "hb.json")
    relay._inflight.clear()


def _capture_trigger(monkeypatch, calls):
    def fake_trigger(seat_name, thread, since_id, cwd, prompt, meta=None):
        calls.append({
            "author": seat_name, "thread": thread, "since_id": since_id,
            "cwd": cwd, "prompt": prompt, "meta": meta or {},
        })
        return True

    monkeypatch.setattr(relay, "trigger", fake_trigger)


def _patch_registry(monkeypatch, seats, bin=None):
    monkeypatch.setattr(heads, "load", lambda: [
        {"seat": s, "name": s, "type": "cli", "bin": bin, "args": ["-p"]}
        for s in seats
    ])


@pytest.fixture
def fired(monkeypatch, tmp_path):
    """Aísla el estado en disco y captura los disparos en vez de ejecutarlos.
    Registry con los asientos del mundo pre-MAGI, para que los tests de
    threads libres sigan ejercitando el flip kimi↔claude."""
    _isolate(monkeypatch, tmp_path)
    _patch_registry(monkeypatch, ["kimi", "claude"])
    calls = []
    _capture_trigger(monkeypatch, calls)
    return calls


@pytest.fixture
def fired_magi(monkeypatch, tmp_path):
    """Igual que `fired`, con los asientos canónicos del sistema MAGI y un
    binario simulado (el relay exige binario antes de disparar)."""
    _isolate(monkeypatch, tmp_path)
    _patch_registry(monkeypatch, ["melchior", "balthasar", "casper"], bin="/fake/bin")
    calls = []
    _capture_trigger(monkeypatch, calls)
    return calls


def fresh_state():
    return {"last_id": 0, "threads": {}, "pending": []}


# ------------------------------------------------- cierre de thread (libre)

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
    conn = FakeConn([msg(1, author="adrian", kind="critica")])
    relay.process_cycle(conn, fresh_state())
    assert fired == []


def test_analisis_de_adrian_abre_ronda_y_dispara_la_primera_cabeza(fired):
    """El chat de la UI: el operador escribe libre y el relay tiene que
    arrancar el round-robin (next_seat devuelve seats[0] para autores fuera
    del registry). Con el 'continue' plano por autor, el chat nunca arrancaba."""
    conn = FakeConn([msg(1, author="adrian", kind="analisis")])
    relay.process_cycle(conn, fresh_state())
    assert len(fired) == 1
    assert fired[0]["author"] == "kimi", "la primera cabeza del registry"
    assert fired[0]["since_id"] == 0


def test_arbitraje_de_adrian_no_dispara(fired):
    conn = FakeConn([msg(1, author="adrian", kind="arbitraje")])
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
    _patch_registry(monkeypatch, ["kimi", "claude"])
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


# ------------------------------------------------- decisiones MAGI

def mk_decision_row(**kw):
    base = {
        "id": 42,
        "title": "¿Hubo un ataque?",
        "artifact": None,
        "protocol": "vote",
        "status": "open",
        "round": 1,
        "thread": "d-42",
        "heads": ["melchior", "balthasar", "casper"],
        "anchor_id": 7,
    }
    base.update(kw)
    return base


def mk_pos_row(seat, position, round=1):
    return {
        "decision_id": 42, "head": seat, "round": round,
        "position": position, "conditions": None, "message_id": 1,
    }


def test_decision_abierta_dispara_a_las_cabezas_que_faltan(fired_magi):
    conn = FakeConn([], decisions=[mk_decision_row()])
    relay.process_cycle(conn, fresh_state())

    assert sorted(c["author"] for c in fired_magi) == ["balthasar", "casper", "melchior"]
    assert all(c["thread"] == "d-42" for c in fired_magi)
    assert all(c["since_id"] == 6 for c in fired_magi)  # anchor_id - 1
    assert all(c["meta"]["decision_id"] == 42 for c in fired_magi)
    assert all(c["meta"]["turn"] == "answer" for c in fired_magi)


def test_decision_no_redispara_a_la_cabeza_que_ya_voto(fired_magi):
    conn = FakeConn(
        [], decisions=[mk_decision_row()],
        positions=[mk_pos_row("melchior", "yes")],
    )
    relay.process_cycle(conn, fresh_state())
    assert sorted(c["author"] for c in fired_magi) == ["balthasar", "casper"]


def test_mensaje_en_journal_no_dispara_respuesta_libre(fired_magi):
    """Un 'posicion' en el journal es un turno de decisión, no un mensaje de
    debate libre: no debe saltar el flip de 'el otro asiento'."""
    conn = FakeConn(
        [msg(9, thread="d-42", author="melchior", kind="posicion")],
        decisions=[mk_decision_row()],
    )
    relay.process_cycle(conn, fresh_state())
    assert fired_magi, "sí dispara: faltan las otras dos cabezas"
    assert all(c["meta"].get("turn") in ("answer", "recast") for c in fired_magi)


def test_journal_de_decision_cerrada_no_dispara_thread_libre(fired_magi):
    """La prueba manual del 2026-09-12: al cerrarse una decisión, los mensajes
    de sus posiciones quedan en el thread d<id>. Si el relay los trata como
    debate libre, le dispara al asiento siguiente sobre el journal ya cerrado
    (en el daemon real, con cabezas reales, eso gasta tokens y ensucia el
    journal hasta el tope de disparos)."""
    conn = FakeConn(
        [msg(9, thread="d42", author="melchior", kind="posicion")],
        decisions=[],  # cerrada: no figura entre las abiertas
    )
    relay.process_cycle(conn, fresh_state())
    assert fired_magi == []


def test_decision_cerrada_no_dispara(fired_magi):
    conn = FakeConn([], decisions=[mk_decision_row(status="closed")])
    relay.process_cycle(conn, fresh_state())
    assert fired_magi == []


def test_tope_de_disparos_por_decision(fired_magi):
    state = fresh_state()
    state["threads"]["d-42"] = {"triggers": relay.MAX_TRIGGERS_PER_DECISION, "cwd": "/tmp"}
    relay.process_cycle(FakeConn([], decisions=[mk_decision_row()]), state)
    assert fired_magi == []


def test_turno_de_decision_que_no_arranca_queda_pendiente(fired_magi, monkeypatch):
    monkeypatch.setattr(relay, "trigger", lambda *a, **kw: False)
    state = fresh_state()
    relay.process_cycle(FakeConn([], decisions=[mk_decision_row()]), state)
    assert {p["thread"] for p in state["pending"]} == {"d-42"}


def test_segunda_ronda_pide_recita_a_las_cabezas(fired_magi):
    conn = FakeConn(
        [], decisions=[mk_decision_row(round=2)],
        positions=[mk_pos_row(s, "yes", round=1) for s in ("melchior", "balthasar", "casper")],
    )
    relay.process_cycle(conn, fresh_state())
    assert sorted(c["author"] for c in fired_magi) == ["balthasar", "casper", "melchior"]
    assert all("Revisá la tuya" in c["prompt"] for c in fired_magi)
    assert all(c["meta"]["turn"] == "recast" for c in fired_magi)


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


# ------------------------------------------------- paralelismo y asientos API

def test_tres_cabezas_de_la_misma_decision_disparan_en_paralelo(fired_magi, monkeypatch):
    """El candado in-flight es por asiento (thread::seat), no por thread:
    las tres cabezas de una decisión investigan al mismo tiempo. Con candado
    por thread, dos quedaban encoladas hasta el próximo wake (300s)."""
    def fake_trigger(seat_name, thread, since_id, cwd, prompt, meta=None):
        fired_magi.append({
            "author": seat_name, "thread": thread, "since_id": since_id,
            "cwd": cwd, "prompt": prompt, "meta": meta or {},
        })
        with relay._inflight_lock:
            relay._inflight.add(relay._token(thread, seat_name))
        return True

    monkeypatch.setattr(relay, "trigger", fake_trigger)
    relay.process_cycle(FakeConn([], decisions=[mk_decision_row()]), fresh_state())
    assert sorted(c["author"] for c in fired_magi) == ["balthasar", "casper", "melchior"]


class _SyncThread:
    """Reemplaza threading.Thread para correr el target en el hilo del test."""
    def __init__(self, target, args=(), daemon=True):
        target(*args)

    def start(self):
        pass


def test_asiento_api_dispara_turno_api_sin_proceso(fired_magi, monkeypatch):
    """Un asiento type='api' no ejecuta ningún binario: el relay dispara el
    turno API (HTTP + registro del voto vía board.record_position). Los
    asientos CLI de la misma decisión siguen yendo por trigger()."""
    monkeypatch.setattr(heads, "load", lambda: [
        {"seat": "melchior", "name": "qwen3", "type": "api",
         "model": "qwen3", "base_url": "http://localhost:11434/v1"},
        {"seat": "balthasar", "name": "kimi", "type": "cli", "bin": "/fake/bin", "args": ["-p"]},
        {"seat": "casper", "name": "codex", "type": "cli", "bin": "/fake/bin", "args": ["-p"]},
    ])

    recorded = {}

    def fake_record(conn, decision_id, author, position, body, conditions=None):
        recorded.update({
            "decision_id": decision_id, "author": author,
            "position": position, "body": body,
        })
        return {"action": "wait"}, 99

    monkeypatch.setattr(relay.board, "record_position", fake_record)
    monkeypatch.setattr(relay.apihead, "run_turn", lambda seat, d, journal, memory=None: {
        "position": "yes", "conditions": None, "body": "evidencia en el log",
    })
    monkeypatch.setattr(relay, "connect", lambda: FakeConn([]))
    monkeypatch.setattr(threading, "Thread", _SyncThread)

    state = fresh_state()
    relay.process_cycle(FakeConn([], decisions=[mk_decision_row()]), state)

    assert recorded.get("author") == "melchior"
    assert recorded.get("position") == "yes"
    assert recorded.get("decision_id") == 42
    # los asientos CLI de la misma decisión sí pasaron por trigger()
    assert sorted(c["author"] for c in fired_magi) == ["balthasar", "casper"]
    assert state["threads"]["d-42"]["triggers"] == 3


def test_asiento_api_en_thread_libre_dispara_turno_api(fired, monkeypatch):
    """El round-robin del chat también sirve para cabezas API: sin esto el
    relay exigía un binario CLI y el chat se colgaba cada vez que tocaba un
    asiento API (casper en Ollama, p.ej.) — lo vio la demo del 2026-09-12."""
    monkeypatch.setattr(heads, "load", lambda: [
        {"seat": "melchior", "name": "qwen", "type": "api",
         "model": "qwen", "base_url": "http://localhost:11434/v1"},
        {"seat": "balthasar", "name": "kimi", "type": "cli", "bin": "/fake/bin", "args": []},
    ])
    api_calls = []
    monkeypatch.setattr(relay, "fire_api_chat_turn",
                        lambda seat, thread, since_id: api_calls.append((seat["seat"], thread)) or True)
    relay.process_cycle(FakeConn([msg(1, author="adrian", kind="analisis")]), fresh_state())
    assert api_calls == [("melchior", "t")]
    assert fired == [], "el asiento API no pasa por el spawn CLI"


def test_turno_api_de_chat_publica_un_mensaje_respuesta(fired, monkeypatch):
    """Un turno de chat API completo: lee el journal, chatea y postea UN
    mensaje 'respuesta'. Si falla el INSERT no llega a pasar: el reintento
    sale del próximo ciclo del relay (mismo último autor)."""
    inserted = {}

    class _ChatConn(FakeConn):
        def execute(self, query, params=()):
            q = " ".join(query.split())
            if q.startswith("INSERT INTO messages"):
                # VALUES (%s, %s, 'respuesta', %s, NULL): thread, author, body
                inserted.update(thread=params[0], author=params[1],
                                kind="respuesta", body=params[2])
                return _Result([])
            return super().execute(query, params)

    monkeypatch.setattr(relay, "connect", lambda: _ChatConn([]))
    monkeypatch.setattr(relay.apihead, "run_chat_turn",
                        lambda seat, journal: "sí, yo lo revisaría con calma")

    relay._run_api_chat_turn(
        {"seat": "melchior", "model": "qwen", "base_url": "http://x/v1"}, "chat", 0,
    )
    assert inserted == {
        "thread": "chat", "author": "melchior",
        "kind": "respuesta", "body": "sí, yo lo revisaría con calma",
    }


def test_kill_tree_usa_el_mecanismo_de_la_plataforma(monkeypatch):
    """Windows no tiene grupos POSIX ni SIGKILL: mata el árbol con taskkill
    /T /F. POSIX: killpg sobre el grupo del hijo. Lo que no puede pasar es
    que el hilo supervisor reviente por usar una API inexistente."""
    calls = []

    class _Proc:
        pid = 4242

    if sys.platform == "win32":
        monkeypatch.setattr(
            relay.subprocess, "run",
            lambda cmd, **kw: calls.append(cmd),
        )
    else:
        monkeypatch.setattr(
            relay.signal, "SIGKILL", 9, raising=False,
        )
        monkeypatch.setattr(
            relay.os, "killpg",
            lambda pid, sig: calls.append((pid, sig)),
        )
    relay._kill_tree(_Proc())
    assert calls, "tuvo que intentar matar el árbol de procesos"
    if sys.platform == "win32":
        assert calls[0][:3] == ["taskkill", "/F", "/T"]
        assert str(_Proc.pid) in calls[0]


# --------------------------------------------------------- cabezas CLI journal-inline

def test_cabeza_cli_inline_parsea_el_voto_de_stdout(fired_magi, monkeypatch, tmp_path, allow_real_processes):
    """Asiento CLI sin MCP (journal='inline', p.ej. codex exec, cuyo modo
    no interactivo no expone tools de servers externos): el relay inlinea
    el journal en el prompt, corre el proceso y registra el voto parseado
    del POSITION: del stdout."""
    stub = tmp_path / "stub_vota.py"
    stub.write_text(
        "print('POSITION: yes')" + chr(10) + "print()" + chr(10) + "print('[stub] razon inline')" + chr(10),
        encoding="utf-8",
    )
    monkeypatch.setattr(heads, "load", lambda: [
        {"seat": "melchior", "name": "stub-inline", "type": "cli",
         "journal": "inline", "bin": sys.executable, "args": [str(stub)]},
    ])
    recorded = {}

    def fake_record(conn, decision_id, author, position, body, conditions=None):
        recorded.update(decision_id=decision_id, author=author,
                        position=position, body=body)
        return {"action": "wait"}, 1

    monkeypatch.setattr(relay.board, "record_position", fake_record)
    monkeypatch.setattr(relay, "connect", lambda: FakeConn([]))
    monkeypatch.setattr(threading, "Thread", _SyncThread)

    relay.process_cycle(FakeConn([], decisions=[mk_decision_row()]), fresh_state())

    assert recorded["author"] == "melchior"
    assert recorded["position"] == "yes"
    assert "razon inline" in recorded["body"]


def test_cabeza_cli_inline_en_chat_postea_su_stdout(fired, monkeypatch, tmp_path, allow_real_processes):
    """El chat libre también funciona sin MCP: el stdout completo se postea
    como un único mensaje 'respuesta'."""
    stub = tmp_path / "stub_charla.py"
    stub.write_text("print('charla de prueba del stub inline')", encoding="utf-8")
    monkeypatch.setattr(heads, "load", lambda: [
        {"seat": "melchior", "name": "stub-inline", "type": "cli",
         "journal": "inline", "bin": sys.executable, "args": [str(stub)]},
    ])

    inserted = {}

    class _ChatConn(FakeConn):
        def execute(self, query, params=()):
            q = " ".join(query.split())
            if q.startswith("INSERT INTO messages"):
                # VALUES (%s, %s, 'respuesta', %s, NULL): thread, author, body
                inserted.update(thread=params[0], author=params[1], body=params[2])
                return _Result([])
            return super().execute(query, params)

    monkeypatch.setattr(relay, "connect", lambda: _ChatConn([]))
    monkeypatch.setattr(threading, "Thread", _SyncThread)

    relay.process_cycle(FakeConn([msg(1, author="adrian", kind="analisis")]), fresh_state())

    assert inserted == {
        "thread": "t", "author": "melchior",
        "body": "charla de prueba del stub inline",
    }


def test_la_memoria_del_grafo_entra_al_prompt_de_la_cabeza(fired_magi, monkeypatch):
    """El relay consulta el grafo una vez por tanda de turnos y la misma
    memoria llega a todas las cabezas (es contexto compartido). Sin grafo
    (degradado) los prompts no cambian."""
    import memory_ctx
    monkeypatch.setattr(memory_ctx, "memoria_para", lambda t, a=None: "MEMORIA-PRUEBA-X")
    relay.process_cycle(FakeConn([], decisions=[mk_decision_row()]), fresh_state())
    assert fired_magi, "tiene que haber disparos"
    assert all("MEMORIA-PRUEBA-X" in c["prompt"] for c in fired_magi)
