"""Validaciones de server.post_message sin Postgres: el arbitraje es solo de
adrian — una cabeza que posteara kind='arbitraje' corría el UPDATE que cierra
una decisión split con un ruling de máquina."""

import pytest

import server


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, arbitrated=None):
        self.executed = []
        self.arbitrated = arbitrated

    def execute(self, query, params=()):
        self.executed.append(" ".join(query.split()))
        q = self.executed[-1]
        if q.startswith("INSERT INTO messages"):
            return _Result([{"id": 77}])
        if q.startswith("UPDATE decisions"):
            return _Result([{"id": self.arbitrated}] if self.arbitrated else [])
        raise AssertionError(f"query inesperada: {q}")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_cabeza_no_puede_arbitrar(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(server, "connect", lambda: conn)
    with pytest.raises(ValueError, match="solo de adrian"):
        server.post_message("d1", "melchior", "arbitraje", "cierro yo")
    assert conn.executed == [], "la validación tiene que cortar antes de escribir"


def test_adrian_si_puede_arbitrar(monkeypatch):
    conn = _FakeConn(arbitrated=5)
    monkeypatch.setattr(server, "connect", lambda: conn)
    out = server.post_message("d1", "adrian", "arbitraje", "ruling: sí")
    assert out == {"id": 77, "arbitrated_decision": 5}
    assert any(q.startswith("UPDATE decisions") for q in conn.executed)


def test_posicion_de_cabeza_sigue_andando(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(server, "connect", lambda: conn)
    out = server.post_message("d1", "melchior", "posicion", "mi voto")
    assert out == {"id": 77, "arbitrated_decision": None}
    assert any(q.startswith("INSERT INTO messages") for q in conn.executed)
