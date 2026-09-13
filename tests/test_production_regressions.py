"""Regression cases for execution failures and the approved plan contract."""

from contextlib import nullcontext
from unittest.mock import Mock
import subprocess

import pytest

import board
import relay


@pytest.mark.parametrize("votes", [
    ["conditional", "conditional", "conditional"],
    ["conditional", "conditional", "yes"],
    ["yes", "yes", "conditional"],
])
def test_executor_receives_conditions_from_all_current_conditional_votes(votes):
    seats = ["melchior", "balthasar", "casper"]
    d = dict(id=1, thread="d1", title="plan", status="open", round=2,
             heads=seats, protocol="vote", production=True)
    positions = [dict(head=s, round=2, position=v, conditions=[s, "tests"])
                 for s, v in zip(seats, votes)]
    positions.append(dict(head="melchior", round=1, position="conditional",
                          conditions=["obsolete"]))
    conn = Mock()
    saved = {}

    def execute(query, params=()):
        result = Mock()
        if "SELECT * FROM decisions" in query:
            result.fetchone.return_value = d
        elif "SELECT 1 FROM positions" in query:
            result.fetchone.return_value = None
        elif "INSERT INTO messages" in query:
            result.fetchone.return_value = {"id": 9}
        elif "SELECT head, round" in query:
            result.fetchall.return_value = positions
        elif "SET status = 'executing'" in query:
            saved.update(params[2].obj)
        return result

    conn.execute.side_effect = execute
    board.record_position(conn, 1, seats[-1], votes[-1], "reason")
    expected = list(dict.fromkeys(c for s, v in zip(seats, votes)
                                 if v == "conditional" for c in [s, "tests"]))
    assert relay._condiciones_aprobacion({"minority_report": saved}) == expected


def test_inline_timeout_kills_and_reaps_before_unregistering(monkeypatch, tmp_path):
    proc = Mock()
    proc.wait.side_effect = [subprocess.TimeoutExpired("stub", 1), -9]
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: proc)
    killed = []

    def kill(p):
        assert relay._procs["d1::casper"] is p
        killed.append(p)

    monkeypatch.setattr(relay, "_kill_tree", kill)
    with pytest.raises(subprocess.TimeoutExpired):
        relay._run_cli_inline({"seat": "casper", "bin": "stub"}, "prompt",
                              str(tmp_path), 1, token="d1::casper")
    assert killed == [proc]
    assert proc.wait.call_count == 2
    assert "d1::casper" not in relay._procs


@pytest.mark.parametrize("body,retry", [("mirá los tests", False), ("seguí", True)])
def test_execution_context_only_retries_explicitly(body, retry):
    conn = Mock()
    conn.execute.return_value.fetchone.side_effect = (
        [{"id": 1, "status": "executing", "round": 1}, {"id": 9}]
    )
    board.human_message(conn, "d1", body)
    queries = [c.args[0] for c in conn.execute.call_args_list]
    assert not any("DELETE" in q for q in queries)
    assert any('"execution_state": "pending"' in q for q in queries) == retry


def test_executor_spawn_error_is_persisted_for_manual_retry(monkeypatch):
    monkeypatch.setattr(relay, "executor_seat", lambda: None)
    conn = Mock()
    conn.transaction.side_effect = lambda: nullcontext()
    monkeypatch.setattr(relay, "connect", lambda: nullcontext(conn))
    monkeypatch.setattr(relay, "event", lambda *a, **kw: None)
    relay._run_executor_turn({"id": 1, "thread": "d1"}, "/unused")
    calls = conn.execute.call_args_list
    assert any('"execution_state": "failed"' in c.args[0] for c in calls)
    assert any("EJECUCIÓN FALLIDA" in str(c.args) for c in calls)
