"""Tests del ejecutor de asientos API (apihead.py): parseo del voto
estructurado (tag POSITION, truco de fshiori/magi) y chat contra un endpoint
OpenAI-compatible de mentira. Nada acá toca Ollama de verdad."""

import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import apihead


class _Handler(BaseHTTPRequestHandler):
    """Devuelve una respuesta OpenAI-compatible fija y guarda el último request."""
    canned = {"choices": [{"message": {"content": "POSITION: yes\nPorque sí."}}]}
    last_payload = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        type(self).last_payload = json.loads(self.rfile.read(length))
        body = json.dumps(type(self).canned).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_endpoint():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


# ------------------------------------------------------------ parse_vote

def test_parse_vote_extrae_position_y_razonamiento():
    v = apihead.parse_vote("POSITION: no\nNo hay evidencia de egress.")
    assert v["position"] == "no"
    assert v["conditions"] is None
    assert "egress" in v["body"]


def test_parse_vote_conditional_con_conditions():
    v = apihead.parse_vote(
        "POSITION: conditional\n"
        "CONDITIONS: si hay egress; si el log es completo\n"
        "Resto del razonamiento."
    )
    assert v["position"] == "conditional"
    assert v["conditions"] == ["si hay egress", "si el log es completo"]


def test_parse_vote_case_insensitive():
    assert apihead.parse_vote("position: YES\nok")["position"] == "yes"


def test_parse_vote_sin_tag_cae_a_info_con_texto_crudo():
    """El voto nunca se pierde: sin tag parseable cae a 'info' y el
    razonamiento queda entero en el journal para que un humano lo lea."""
    v = apihead.parse_vote("Creo que sí pero no estoy seguro.")
    assert v["position"] == "info"
    assert "no estoy seguro" in v["body"]


# ------------------------------------------------------------ chat

def test_chat_postea_openai_compatible(fake_endpoint):
    text = apihead.chat(fake_endpoint, "qwen3", "system de prueba", "user de prueba", timeout_secs=5)
    assert text.startswith("POSITION: yes")

    payload = _Handler.last_payload
    assert payload["model"] == "qwen3"
    assert payload["messages"][0] == {"role": "system", "content": "system de prueba"}
    assert payload["messages"][1] == {"role": "user", "content": "user de prueba"}
    assert payload["stream"] is False


def test_chat_falla_si_el_endpoint_no_responde():
    with pytest.raises((urllib.error.URLError, OSError)):
        apihead.chat("http://127.0.0.1:1/v1", "qwen3", "s", "u", timeout_secs=2)


# ------------------------------------------------------------ prompts y run_turn

def _decision():
    return {
        "id": 3, "title": "¿Hubo SQLi?", "artifact": "/repo/auth.log",
        "protocol": "critique", "round": 2,
    }


def test_prompt_api_inlinea_journal_y_contrato_de_respuesta():
    journal = [
        {"author": "melchior", "kind": "posicion", "body": "sí, hay evidencia"},
        {"author": "balthasar", "kind": "posicion", "body": "no me convence"},
    ]
    system, user = apihead.build_api_prompt("melchior", _decision(), journal)
    assert "MELCHIOR" in system
    assert "POSITION: yes|no|conditional|info" in system
    assert "Decisión #3" in user and "¿Hubo SQLi?" in user
    assert "/repo/auth.log" in user
    assert "melchior" in user and "balthasar" in user


def test_prompt_api_trunca_cuerpos_largos_del_journal():
    journal = [{"author": "a", "kind": "posicion", "body": "x" * 10000}]
    _, user = apihead.build_api_prompt("melchior", _decision(), journal)
    assert "x" * (apihead.BODY_CHARS + 1) not in user


def test_run_turn_completo_con_endpoint_falso(fake_endpoint):
    seat = {"seat": "casper", "type": "api", "model": "qwen3", "base_url": fake_endpoint}
    journal = [{"author": "adrian", "kind": "analisis", "body": "la pregunta"}]
    vote = apihead.run_turn(seat, _decision(), journal)
    assert vote["position"] == "yes"
    assert "Porque sí." in vote["body"]
