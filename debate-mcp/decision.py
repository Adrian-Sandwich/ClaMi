"""Motor de decisiones MAGI: funciones puras, sin IO.

Reciben estado ya leído de Postgres (filas de decisions/positions como dicts)
y devuelven ÓRDENES: a quién disparar, con qué prompt, o cómo cerrar la ronda.
server.py aplica los cambios en la base; relay.py dispara los procesos. Que
sea puro es lo que lo hace testeable sin mocks de red ni de base.

Protocolos (referencia conceptual: fshiori/magi, reescrito a lo ClaMi):
- vote:     una ronda de posiciones; mayoría cierra; split va a arbitraje.
- critique: rondas de crítica mutua hasta acuerdo o MAX_ROUNDS.
- adaptive: vota primero; si hay split 3-vías, pasa a critique solo.

El cierre espera las posiciones de TODOS los asientos participantes de la
ronda: el minority report sólo es completo si las tres cabezas votaron.
"""

POSITIONS = ("yes", "no", "conditional", "info")
PROTOCOLS = ("vote", "critique", "adaptive")
MAX_ROUNDS = 3

# una sola cabeza (modo degradado): su voto manda, pero con confidence mínima —
# no es un veredicto MAGI, es el mejor esfuerzo disponible.
CONFIDENCE_UNANIMOUS = 1.0
CONFIDENCE_MAJORITY = 0.66
CONFIDENCE_DEGRADED = 0.5


def round_positions(positions: list[dict], round: int) -> list[dict]:
    return [p for p in positions if p["round"] == round]


def mind_changes(positions: list[dict]) -> list[dict]:
    """Cambios de parecer entre rondas consecutivas, por cabeza.

    Un recast con la misma posición no cuenta: mantenerse firme también es
    información, pero no es un cambio de parecer. Sólo se comparan rondas
    donde la cabeza votó en ambas — la que todavía no recasteó no entra.
    El dossier los reporta porque un consenso alcanzado DESPUÉS de un cambio
    vale distinto que uno sostenido desde la primera ronda.
    """
    by_head: dict[str, dict[int, dict]] = {}
    for p in positions:
        by_head.setdefault(p["head"], {})[p["round"]] = p
    changes = []
    for head, rounds in by_head.items():
        for r in sorted(rounds):
            prev = rounds.get(r - 1)
            if prev is not None and prev["position"] != rounds[r]["position"]:
                changes.append({
                    "head": head,
                    "from": prev["position"],
                    "to": rounds[r]["position"],
                    "round": r,
                })
    return changes


def missing_seats(decision: dict, positions: list[dict]) -> list[str]:
    """Asientos participantes que todavía no votaron en la ronda actual."""
    cast = {p["head"] for p in round_positions(positions, decision["round"])}
    return [h for h in decision["heads"] if h not in cast]


def pending_turns(decision: dict, positions: list[dict]) -> list[dict]:
    """Turnos que el relay debe disparar ahora: los asientos que faltan en la
    ronda actual de una decisión abierta."""
    if decision["status"] != "open":
        return []
    kind = "answer" if decision["round"] == 1 else "recast"
    return [
        {"seat": h, "kind": kind, "round": decision["round"]}
        for h in missing_seats(decision, positions)
    ]


def resolve_votes(decision: dict, positions: list[dict]) -> dict | None:
    """Mayoría sobre las posiciones de la ronda actual.

    Devuelve {"ruling", "confidence", "minority", "degraded"} o None cuando
    no hay mayoría (todas distintas). Con una sola cabeza (degradado) su voto
    manda con confidence mínima; con dos divididas no hay mayoría posible.
    """
    votes = {p["head"]: p for p in round_positions(positions, decision["round"])}
    participating = [h for h in decision["heads"] if h in votes]
    if not participating:
        return None
    if len(participating) == 1:
        p = votes[participating[0]]
        return {
            "ruling": p["position"],
            "confidence": CONFIDENCE_DEGRADED,
            "minority": [],
            "degraded": True,
        }

    counts: dict[str, int] = {}
    for h in participating:
        pos = votes[h]["position"]
        counts[pos] = counts.get(pos, 0) + 1
    best = max(counts.values())
    if best < 2:
        return None  # todas distintas: split
    ruling = next(pos for pos, n in counts.items() if n == best)
    return {
        "ruling": ruling,
        "confidence": CONFIDENCE_UNANIMOUS if best == len(participating) else CONFIDENCE_MAJORITY,
        "minority": [
            {
                "head": h,
                "position": votes[h]["position"],
                "conditions": votes[h].get("conditions"),
            }
            for h in participating
            if votes[h]["position"] != ruling
        ],
        "degraded": len(participating) < len(decision["heads"]),
    }


def advance(decision: dict, positions: list[dict]) -> dict:
    """Qué hacer con la ronda actual. Devuelve una ORDEN:

    - wait:        falta gente, no toca nada.
    - close:       mayoría → ruling/confidence/minority; hay que cerrar la decisión.
    - next_round:  split con protocolo critique/adaptive y rondas disponibles.
    - split:       no hay acuerdo y no queda mecanismo: pasa a arbitraje humano.
    - none:        la decisión ya está cerrada.
    """
    if decision["status"] != "open":
        return {"action": "none"}
    if missing_seats(decision, positions):
        return {"action": "wait"}
    res = resolve_votes(decision, positions)
    if res is not None:
        return {"action": "close", **res}

    rp = round_positions(positions, decision["round"])
    minority = [
        {"head": p["head"], "position": p["position"], "conditions": p.get("conditions")}
        for p in rp
    ]
    if decision["protocol"] == "vote" or decision["round"] >= MAX_ROUNDS:
        return {"action": "split", "minority": minority}
    return {"action": "next_round", "minority": minority}


def build_head_prompt(seat: str, persona: str, decision: dict, since_id: int) -> str:
    """Prompt del disparo a una cabeza: su persona + el estado de la decisión +
    las instrucciones concretas de turno."""
    d = decision
    lines = [
        persona,
        "",
        f"Decisión #{d['id']} (protocolo {d['protocol']}, ronda {d['round']}): {d['title']}",
    ]
    if d.get("artifact"):
        lines.append(f"Artefacto sobre el que se decide: {d['artifact']}")
    lines.append("")
    lines.append(
        f"1. Leé el journal del debate: read_thread(thread='{d['thread']}', since_id={since_id})."
    )
    if d["round"] > 1:
        prev = d["round"] - 1
        summary = ", ".join(
            f"{p['head']}={p['position']}" for p in round_positions(d.get("positions") or [], prev)
        )
        lines.append(
            f"2. Las posiciones de la ronda {prev} están en el journal"
            + (f": {summary}." if summary else ".")
        )
        lines.append(
            "   Revisá la tuya a la luz de las otras dos cabezas: cambiala sólo si sus "
            "argumentos son mejores que los tuyos; si te mantenés, reforzá tu posición "
            "contra ellos. Cambiar de parecer es legítimo — queda registrado."
        )
    else:
        lines.append(
            "2. Investigá el artefacto con tus herramientas (Read/Grep/Glob) antes de votar."
        )
    lines.append(
        f"3. Publicá tu análisis y votá: cast_position(decision_id={d['id']}, "
        "position=<yes|no|conditional|info>, conditions=[...] si aplica, "
        "body=<tu razonamiento completo>)."
    )
    lines.append("Posteá UN SOLO mensaje. No uses wait_messages.")
    return "\n".join(lines)


def resultado_text(decision: dict, outcome: dict) -> str:
    """Texto del message 'resultado' que resume el cierre en el journal."""
    d = decision
    if outcome["action"] == "close":
        minor = outcome.get("minority") or []
        if minor:
            detalle = "; ".join(
                f"{m['head']} votó {m['position']}"
                + (f" ({', '.join(m['conditions'])})" if m.get("conditions") else "")
                for m in minor
            )
            minor_txt = f"Minority report: {detalle}."
        else:
            minor_txt = "Sin minoría: votación unánime."
        cambios = outcome.get("mind_changes") or []
        if cambios:
            detalle = "; ".join(
                f"{c['head']} {c['from']}→{c['to']} (ronda {c['round']})" for c in cambios
            )
            minor_txt += f" Cambios de posición: {detalle}."
        degradado = " [MODO DEGRADADO]" if outcome.get("degraded") else ""
        return (
            f"DECISIÓN #{d['id']} CERRADA{degradado} — ruling: {outcome['ruling']} "
            f"(confidence {outcome['confidence']}). {minor_txt}"
        )
    if outcome["action"] == "split":
        detalle = "; ".join(
            f"{m['head']}={m['position']}" for m in outcome.get("minority", [])
        )
        return (
            f"DECISIÓN #{d['id']} SIN ACUERDO tras {d['round']} ronda(s) — posiciones: {detalle}. "
            f"El sistema no inventa consenso: adrian, cerrá con post_message(thread='{d['thread']}', "
            "author='adrian', kind='arbitraje', body=<tu ruling y justificación>)."
        )
    raise ValueError(f"outcome sin texto: {outcome['action']}")
