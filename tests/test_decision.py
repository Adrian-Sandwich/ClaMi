"""Tests del motor de decisiones MAGI (decision.py): funciones puras, sin
base ni procesos. El invariante central: mayoría 2/3 cierra con minority
report completo; tres posiciones distintas son split; el cierre espera a
todos los asientos de la ronda."""

import pytest

import decision
import personas

SEATS = ["melchior", "balthasar", "casper"]


def act_minority(pos):
    return [
        {"head": p["head"], "position": p["position"], "conditions": p.get("conditions")}
        for p in pos
    ]


def mk_decision(**kw):
    base = {
        "id": 1,
        "title": "¿Hubo SQLi en auth.log?",
        "artifact": "/repo/auth.log",
        "protocol": "vote",
        "status": "open",
        "round": 1,
        "thread": "d1",
        "heads": list(SEATS),
        "anchor_id": 10,
        "created_by": "adrian",
    }
    base.update(kw)
    return base


def mk_pos(seat, position, round=1, conditions=None):
    return {"head": seat, "round": round, "position": position, "conditions": conditions}


# ------------------------------------------------------------ resolve_votes

def test_mayoria_unanime_cierra():
    res = decision.resolve_votes(mk_decision(), [mk_pos(s, "yes") for s in SEATS])
    assert res["ruling"] == "yes"
    assert res["confidence"] == decision.CONFIDENCE_UNANIMOUS
    assert res["minority"] == []
    assert res["degraded"] is False


def test_mayoria_dos_a_uno_con_minority_report():
    res = decision.resolve_votes(
        mk_decision(),
        [
            mk_pos("melchior", "yes"),
            mk_pos("balthasar", "yes"),
            mk_pos("casper", "no", conditions=["no hay egress"]),
        ],
    )
    assert res["ruling"] == "yes"
    assert res["confidence"] == decision.CONFIDENCE_MAJORITY
    assert res["minority"] == [
        {"head": "casper", "position": "no", "conditions": ["no hay egress"]}
    ]


def test_tres_posiciones_distintas_no_tienen_mayoria():
    pos = [mk_pos("melchior", "yes"), mk_pos("balthasar", "no"), mk_pos("casper", "conditional")]
    assert decision.resolve_votes(mk_decision(), pos) is None


def test_dos_asientos_divididos_no_tienen_mayoria():
    d = mk_decision(heads=["melchior", "balthasar"])
    pos = [mk_pos("melchior", "yes"), mk_pos("balthasar", "no")]
    assert decision.resolve_votes(d, pos) is None


def test_un_asiento_degradado_manda_con_confidence_minima():
    # modo degradado: una sola cabeza votó. Su voto es el mejor esfuerzo
    # disponible, no un veredicto MAGI.
    res = decision.resolve_votes(mk_decision(), [mk_pos("melchior", "no")])
    assert res["ruling"] == "no"
    assert res["confidence"] == decision.CONFIDENCE_DEGRADED
    assert res["degraded"] is True


def test_solo_cuenta_la_ronda_actual():
    d = mk_decision(round=2)
    pos = [mk_pos(s, "yes", round=2) for s in SEATS]
    pos += [mk_pos("melchior", "no"), mk_pos("balthasar", "no"), mk_pos("casper", "no")]
    res = decision.resolve_votes(d, pos)
    assert res["ruling"] == "yes"


# ------------------------------------------------------------ pending_turns

def test_turnos_pendientes_son_los_asientos_que_faltan():
    turns = decision.pending_turns(mk_decision(), [mk_pos("melchior", "yes")])
    assert [t["seat"] for t in turns] == ["balthasar", "casper"]
    assert all(t["kind"] == "answer" and t["round"] == 1 for t in turns)


def test_segunda_ronda_pide_recast():
    turns = decision.pending_turns(mk_decision(round=2), [])
    assert [t["seat"] for t in turns] == SEATS
    assert all(t["kind"] == "recast" for t in turns)


def test_decision_cerrada_no_pide_turnos():
    assert decision.pending_turns(mk_decision(status="closed"), []) == []


def test_ronda_completa_no_pide_turnos():
    pos = [mk_pos(s, "yes") for s in SEATS]
    assert decision.pending_turns(mk_decision(), pos) == []


# ------------------------------------------------------------ advance

def test_advance_espera_a_que_voten_todos():
    pos = [mk_pos("melchior", "yes"), mk_pos("balthasar", "yes")]
    assert decision.advance(mk_decision(), pos) == {"action": "wait"}


def test_advance_con_protocolo_vote_manda_el_split_a_arbitraje():
    d = mk_decision(protocol="vote")
    pos = [mk_pos("melchior", "yes"), mk_pos("balthasar", "no"), mk_pos("casper", "conditional")]
    act = decision.advance(d, pos)
    assert act["action"] == "split"
    assert {m["head"] for m in act["minority"]} == set(SEATS)


def test_advance_con_critique_abre_la_segunda_ronda():
    d = mk_decision(protocol="critique")
    pos = [mk_pos("melchior", "yes"), mk_pos("balthasar", "no"), mk_pos("casper", "conditional")]
    assert decision.advance(d, pos) == {"action": "next_round", "minority": act_minority(pos)}


def act_minority(pos):
    return [{"head": p["head"], "position": p["position"], "conditions": p.get("conditions")} for p in pos]


def test_advance_agota_max_rounds_y_declara_split():
    d = mk_decision(protocol="critique", round=decision.MAX_ROUNDS)
    pos = [mk_pos(s, p, round=decision.MAX_ROUNDS) for s, p in
           zip(SEATS, ("yes", "no", "conditional"))]
    assert decision.advance(d, pos)["action"] == "split"


def test_adaptive_cierra_directo_con_mayoria():
    d = mk_decision(protocol="adaptive")
    pos = [mk_pos("melchior", "yes"), mk_pos("balthasar", "yes"), mk_pos("casper", "no")]
    act = decision.advance(d, pos)
    assert act["action"] == "close" and act["ruling"] == "yes"


def test_adaptive_con_split_abre_critique():
    d = mk_decision(protocol="adaptive")
    pos = [mk_pos("melchior", "yes"), mk_pos("balthasar", "no"), mk_pos("casper", "conditional")]
    assert decision.advance(d, pos)["action"] == "next_round"


def test_advance_cerrada_no_opera():
    assert decision.advance(mk_decision(status="closed"), []) == {"action": "none"}


# ------------------------------------------------------------ prompts

def test_prompt_de_primera_ronda_pide_investigar_y_votar():
    d = mk_decision()
    txt = decision.build_head_prompt("melchior", personas.system_prompt("melchior"), d, 9)
    assert "MELCHIOR" in txt and "verdad técnica" in txt
    assert "read_thread(thread='d1', since_id=9)" in txt
    assert "cast_position(decision_id=1" in txt
    assert "Investigá" in txt


def test_prompt_de_recita_pide_revisar_la_posicion():
    txt = decision.build_head_prompt("casper", personas.system_prompt("casper"), mk_decision(round=2), 9)
    assert "Revisá la tuya" in txt


def test_prompt_de_asiento_custom_no_explota():
    d = mk_decision(heads=["custom1", "custom2", "custom3"])
    txt = decision.build_head_prompt("custom1", "Sos el asiento 'custom1' del sistema MAGI.", d, 0)
    assert "custom1" in txt


def test_persona_desconocida_explota():
    with pytest.raises(ValueError):
        personas.system_prompt("gendo")


# ------------------------------------------------------------ resultado_text

def test_resultado_de_cierre_menciona_ruling_y_minority():
    d = mk_decision()
    pos = [mk_pos("melchior", "yes"), mk_pos("balthasar", "yes"), mk_pos("casper", "no")]
    txt = decision.resultado_text(d, decision.advance(d, pos))
    assert "CERRADA" in txt and "ruling: yes" in txt and "casper votó no" in txt


def test_resultado_de_split_pide_arbitraje():
    d = mk_decision(protocol="vote")
    pos = [mk_pos("melchior", "yes"), mk_pos("balthasar", "no"), mk_pos("casper", "conditional")]
    txt = decision.resultado_text(d, decision.advance(d, pos))
    assert "SIN ACUERDO" in txt and "arbitraje" in txt


# ------------------------------------------------------------ mind_changes

def test_mind_changes_detecta_cambios_entre_rondas():
    pos = [mk_pos(s, p, 1) for s, p in zip(SEATS, ("yes", "no", "conditional"))]
    pos += [mk_pos("melchior", "yes", 2), mk_pos("balthasar", "yes", 2), mk_pos("casper", "no", 2)]
    assert decision.mind_changes(pos) == [
        {"head": "balthasar", "from": "no", "to": "yes", "round": 2},
        {"head": "casper", "from": "conditional", "to": "no", "round": 2},
    ]


def test_mind_changes_vacio_si_todos_se_mantienen():
    pos = [mk_pos(s, "yes", 1) for s in SEATS] + [mk_pos(s, "yes", 2) for s in SEATS]
    assert decision.mind_changes(pos) == []


def test_mind_changes_solo_compara_rondas_con_voto_en_ambas():
    """La cabeza que todavía no recasteó en la ronda nueva no entra al
    diff: no hay 'cambio' que medir hasta que vote."""
    pos = [mk_pos(s, p, 1) for s, p in zip(SEATS, ("yes", "no", "conditional"))]
    pos += [mk_pos("melchior", "no", 2), mk_pos("balthasar", "yes", 2)]
    assert {m["head"] for m in decision.mind_changes(pos)} == {"melchior", "balthasar"}


def test_mind_changes_detecta_cambios_acumulados_en_varias_rondas():
    pos = [mk_pos(s, "no", 1) for s in SEATS]
    pos += [mk_pos(s, "no", 2) for s in SEATS]
    pos += [mk_pos("melchior", "yes", 3), mk_pos("balthasar", "no", 3), mk_pos("casper", "no", 3)]
    assert decision.mind_changes(pos) == [
        {"head": "melchior", "from": "no", "to": "yes", "round": 3}
    ]


def test_prompt_de_recita_resume_las_posiciones_de_la_ronda_anterior():
    d = mk_decision(round=2)
    d["positions"] = [mk_pos(s, p, 1) for s, p in zip(SEATS, ("yes", "no", "conditional"))]
    txt = decision.build_head_prompt("melchior", personas.system_prompt("melchior"), d, 9)
    assert "ronda 1" in txt
    assert "melchior=yes, balthasar=no, casper=conditional" in txt
    assert "Revisá la tuya" in txt


def test_resultado_de_cierre_menciona_cambios_de_posicion():
    d = mk_decision(protocol="critique", round=2)
    pos = [mk_pos(s, p, 1) for s, p in zip(SEATS, ("yes", "no", "conditional"))]
    pos += [mk_pos("melchior", "yes", 2), mk_pos("balthasar", "yes", 2), mk_pos("casper", "no", 2)]
    act = decision.advance(d, pos)
    act["mind_changes"] = decision.mind_changes(pos)
    txt = decision.resultado_text(d, act)
    assert "Cambios de posición" in txt
    assert "balthasar no→yes (ronda 2)" in txt


def test_critique_cierra_en_ronda_dos_despues_del_cambio_de_parecer():
    """Flujo completo de critique: split 3-vías → ronda 2 → mayoría con una
    cabeza que cambió de parecer. El minority report queda completo."""
    d = mk_decision(protocol="critique")
    pos = [mk_pos(s, p, 1) for s, p in zip(SEATS, ("yes", "no", "conditional"))]
    assert decision.advance(d, pos)["action"] == "next_round"

    d2 = mk_decision(protocol="critique", round=2)
    pos += [mk_pos("melchior", "yes", 2), mk_pos("balthasar", "yes", 2), mk_pos("casper", "no", 2)]
    act = decision.advance(d2, pos)
    assert act["action"] == "close"
    assert act["ruling"] == "yes"
    assert act["minority"] == [
        {"head": "casper", "position": "no", "conditions": None}
    ]
    assert decision.mind_changes(pos) == [
        {"head": "balthasar", "from": "no", "to": "yes", "round": 2},
        {"head": "casper", "from": "conditional", "to": "no", "round": 2},
    ]


# ------------------------------------------------------------ board.human_message

class _HMConn:
    """Postgres de mentira para board.human_message: estado de la decisión del
    thread + INSERT del message + UPDATE de arbitraje."""

    def __init__(self, decision_status=None):
        self.decision_status = decision_status
        self.inserted = []
        self.arbitration_attempted = False

    def execute(self, query, params=()):
        q = " ".join(query.split())
        if q.startswith("SELECT id, status FROM decisions"):
            rows = [{"id": 7, "status": self.decision_status}] if self.decision_status else []
            return _HMResult(rows)
        if q.startswith("INSERT INTO messages"):
            self.inserted.append(params)
            return _HMResult([{"id": 99}])
        if q.startswith("UPDATE decisions"):
            self.arbitration_attempted = True
            rows = [{"id": 7}] if self.decision_status == "split" else []
            return _HMResult(rows)
        raise AssertionError(f"query inesperada: {q}")


class _HMResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


def test_human_message_en_thread_libre_abre_ronda():
    import board

    conn = _HMConn()
    out = board.human_message(conn, "chat", "¿qué opinan de este plan?")
    assert out["kind"] == "analisis"
    assert out["arbitrated_decision"] is None
    assert conn.inserted == [("chat", "analisis", "¿qué opinan de este plan?")]
    assert not conn.arbitration_attempted


def test_human_message_en_decision_abierta_es_contexto():
    import board

    conn = _HMConn(decision_status="open")
    out = board.human_message(conn, "d7", "mirá también src/x.py")
    assert out["kind"] == "contexto"
    assert out["arbitrated_decision"] is None
    assert not conn.arbitration_attempted


def test_human_message_en_decision_split_arbitra_y_cierra():
    import board

    conn = _HMConn(decision_status="split")
    out = board.human_message(conn, "d7", "apruebo con la condición de casper")
    assert out["kind"] == "arbitraje"
    assert out["arbitrated_decision"] == 7
    assert conn.arbitration_attempted


def test_build_head_prompt_incluye_la_memoria_opcional():
    d = mk_decision()
    persona = personas.system_prompt("melchior")
    prompt = decision.build_head_prompt(
        "melchior", persona, d, 0, memory="Memoria del consejo:\n- #1 algo → yes")
    assert "Memoria del consejo:" in prompt
    sin = decision.build_head_prompt("melchior", persona, d, 0)
    assert "Memoria del consejo:" not in sin


def test_memory_ctx_trae_decisiones_y_nodos_relacionados(tmp_path, monkeypatch):
    """El lector del grafo: decisiones recientes siempre + nodos que matchean
    los términos del título/artefacto. Sin base (ingesta nunca corrida):
    texto vacío, el sistema deliber igual."""
    import db as graph_db
    import memory_ctx

    NOW = "2026-01-01T00:00:00+00:00"
    path = tmp_path / "memory.db"
    conn = graph_db.connect(path)
    graph_db.upsert_node(conn, id="decision:1", domain="decision", source="t",
                         updated_at=NOW, label="#1 mergeamos relay?",
                         props={"ruling": "yes", "confidence": 0.66})
    graph_db.upsert_node(conn, id="file:relay.py", domain="file", source="t",
                         updated_at=NOW, label="debate-mcp/relay.py")
    conn.commit()
    conn.close()

    monkeypatch.setattr(memory_ctx, "DB_PATH", path)
    mem = memory_ctx.memoria_para("otra vez sobre relay.py")
    assert "#1 mergeamos relay?" in mem
    assert "yes" in mem
    assert "file" in mem and "relay.py" in mem

    monkeypatch.setattr(memory_ctx, "DB_PATH", tmp_path / "no-existe.db")
    assert memory_ctx.memoria_para("cualquier cosa") == ""
