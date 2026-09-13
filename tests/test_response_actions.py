"""Explicit UX choices override the legacy text shortcut without altering the draft."""
from unittest.mock import Mock

import pytest

import board


@pytest.mark.parametrize("action,body,kind", [
    ("resume", "Here is the missing information", "contexto"),
    ("arbitrate", "seguí is no longer the right instruction", "arbitraje"),
])
def test_explicit_split_action(action, body, kind):
    conn = Mock()
    conn.execute.return_value.fetchone.side_effect = [
        {"id": 4, "status": "split", "round": 1}, {"id": 9}, {"id": 4}]
    result = board.human_message(conn, "d4", body, action=action)
    assert result["kind"] == kind
    assert conn.execute.call_args_list[1].args[1] == ("d4", kind, body)


def test_stale_explicit_action_cannot_silently_add_context():
    conn = Mock()
    conn.execute.return_value.fetchone.return_value = {"id": 4, "status": "open", "round": 2}
    with pytest.raises(ValueError):
        board.human_message(conn, "d4", "My final ruling", action="arbitrate")
    assert conn.execute.call_count == 1
