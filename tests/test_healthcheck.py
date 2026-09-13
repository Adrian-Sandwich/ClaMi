"""Tests del healthcheck: ninguno toca Postgres ni el heartbeat reales —
los checks se pisan con stubs y sys.argv se controla por monkeypatch."""

import pytest

import healthcheck


def _check_falla():
    return healthcheck.CRIT, "postgres inalcanzable: connection refused"


def _check_ok():
    return healthcheck.OK, "todo bien"


def test_notify_con_check_fallido_no_revienta(monkeypatch):
    """Regresión: la variable local `notify = "--notify" in sys.argv` pisaba
    la función `notify()` del módulo y el aviso explotaba con
    TypeError: 'bool' object is not callable justo cuando hacía falta."""
    monkeypatch.setattr(healthcheck, "CHECKS", [("postgres", _check_falla)])
    monkeypatch.setattr(healthcheck, "notify", lambda *a: None)
    monkeypatch.setattr("sys.argv", ["healthcheck.py", "--notify"])
    assert healthcheck.main() == 2  # no TypeError


def test_notify_avisa_solo_si_hay_fallas(monkeypatch):
    avisos = []
    monkeypatch.setattr(healthcheck, "CHECKS", [("postgres", _check_falla)])
    monkeypatch.setattr(healthcheck, "notify", lambda title, body: avisos.append((title, body)))
    monkeypatch.setattr("sys.argv", ["healthcheck.py", "--notify"])
    healthcheck.main()
    assert len(avisos) == 1
    assert "CRIT" in avisos[0][0]
    assert "postgres" in avisos[0][1]


def test_notify_sin_fallas_no_avisa(monkeypatch):
    avisos = []
    monkeypatch.setattr(healthcheck, "CHECKS", [("postgres", _check_ok)])
    monkeypatch.setattr(healthcheck, "notify", lambda title, body: avisos.append((title, body)))
    monkeypatch.setattr("sys.argv", ["healthcheck.py", "--notify"])
    assert healthcheck.main() == 0
    assert avisos == []


def test_un_check_que_revienta_no_tumba_el_reporte(monkeypatch):
    def _check_roto():
        raise RuntimeError("boom inesperado")

    monkeypatch.setattr(healthcheck, "CHECKS", [("roto", _check_roto), ("ok", _check_ok)])
    monkeypatch.setattr("sys.argv", ["healthcheck.py"])
    assert healthcheck.main() == 2
