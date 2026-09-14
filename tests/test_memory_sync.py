import json
import subprocess

import db
import healthcheck
import ingest_debate
from explicit_memory import extract
from memory_sync import MemorySync


def test_explicit_memory_versions_and_cancellation():
    result = extract([
        {'id': 1, 'author': 'adrian', 'body': 'Objetivo: Publicar\nRestricción [datos]: Conservar datos'},
        {'id': 2, 'author': 'casper', 'body': 'Objetivo: Borrar todo'},
        {'id': 3, 'author': 'adrian', 'body': 'Objetivo: Probar primero\nPendiente [tests]: Correr pruebas'},
        {'id': 4, 'author': 'adrian', 'body': 'Pendiente [tests]: resuelto\n```\nObjetivo: ejemplo\n```\n> Objetivo: citado'},
    ])
    current = {m['kind']: m for m in result['current']}
    assert current['objetivo']['text'] == 'Probar primero'
    assert current['objetivo']['version'] == 2
    assert current['restriccion']['message_id'] == 1
    assert not current['pendiente']['active']
    assert len(result['history']) == 5


def test_checkpoint_skips_unchanged_but_recovers_missing_node(graph_db):
    graph_db.execute('CREATE TABLE debate_checkpoints (id TEXT PRIMARY KEY, digest TEXT NOT NULL)')
    row = {'body': 'primero'}
    assert ingest_debate.changed(graph_db, 'decision:1', row)
    db.upsert_node(graph_db, id='decision:1', domain='decision', source='test', updated_at='2026')
    assert not ingest_debate.changed(graph_db, 'decision:1', row)
    assert ingest_debate.changed(graph_db, 'decision:1', {'body': 'corregido'})
    graph_db.execute("DELETE FROM nodes WHERE id='decision:1'")
    assert ingest_debate.changed(graph_db, 'decision:1', {'body': 'corregido'})


def test_sync_recovers_after_timeout_without_exposing_stderr(monkeypatch):
    calls = []
    def run(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired('ingestor', 60, stderr=b'private data')
    monkeypatch.setattr(subprocess, 'run', run)
    sync = MemorySync()
    sync.run_once()
    assert sync.snapshot()['status'] == 'error'
    assert 'private' not in json.dumps(sync.snapshot())
    sync.run_once()
    assert sync.snapshot()['status'] == 'ok'
    assert sync.snapshot()['last_success']
    assert sync.snapshot()['failures'] == 0
    assert calls[0]['timeout'] == 60


def test_health_reports_failed_sync_even_if_database_is_recent(tmp_path, monkeypatch):
    graph = tmp_path / 'memory.db'
    graph.touch()
    heartbeat = tmp_path / 'heartbeat.json'
    heartbeat.write_text(json.dumps({'memory_sync': {'status': 'error', 'error': 'TimeoutExpired'}}))
    monkeypatch.setattr(healthcheck, 'MEMORY_DB', graph)
    monkeypatch.setattr(healthcheck, 'HEARTBEAT_PATH', heartbeat)
    assert healthcheck.check_graph()[0] == healthcheck.WARN
