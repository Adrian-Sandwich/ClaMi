"""Local multilingual embeddings, persisted by content and model identity.

No downloads during retrieval. Missing dependencies/index/model leave lexical
retrieval available. Run this script with --download once to install the model.
"""
from contextlib import closing
from functools import lru_cache
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import threading

MODEL = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
CACHE = Path(__file__).resolve().parent.parent / 'memory-graph' / 'models'
THRESHOLD = 0.45
_lock = threading.Lock()
_model = None
log = logging.getLogger(__name__)


def encoder(download=False):
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(model_name=MODEL, cache_dir=str(CACHE), threads=2,
                               local_files_only=not download)
    return _model


def documents(label, props):
    texts = [str(label)]
    if props.get('objective'):
        texts.append(str(props['objective']))
    report = (props.get('experience') or {}).get('latest_report') or {}
    texts.extend(report.get(k, '') for k in ('observation','evidence','lesson'))
    texts.extend(item['text'] for item in props.get('explicit_memory', {}).get('current', []) if item.get('active'))
    texts.extend(item.get('body', '') for item in props.get('evidence', []))
    # Short passages prevent the model's token limit from hiding later evidence.
    chunks = []
    for text in dict.fromkeys(texts):
        chunks.extend(text[i:i+450] for i in range(0, min(len(text), 1800), 450))
    return chunks[:24] or ['']


def digest(chunks):
    return hashlib.sha256(('fastembed-0.8.0/mean/chunks-v1:' + json.dumps(chunks, ensure_ascii=False)).encode()).hexdigest()


def unit(vector):
    values = [float(x) for x in vector]
    norm = math.sqrt(sum(x*x for x in values))
    if not norm or not math.isfinite(norm):
        raise ValueError('Invalid embedding')
    return [x/norm for x in values]


@lru_cache(maxsize=64)
def query_vector(query):
    return unit(next(encoder().embed([query[:2000]])))


def scores(conn, query):
    if not query.strip() or os.environ.get('MEMORY_SEMANTIC', '1') == '0':
        return {}
    try:
        rows = conn.execute("SELECT v.id,v.digest,v.vectors FROM semantic_vectors v JOIN nodes n ON n.id=v.id WHERE v.model=? AND n.domain IN ('decision','debate_thread','doc')", (MODEL,)).fetchall()
        if not rows:
            return {}
        with _lock:
            vector = query_vector(query)
        result = {}
        for row in rows:
            vectors = json.loads(row[2])
            similarity = max((sum(a*b for a,b in zip(vector, v)) for v in vectors if len(v) == len(vector)), default=0)
            if similarity >= THRESHOLD:
                result[row[0]] = (similarity, row[1])
        return result
    except (ImportError, OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
        log.warning('Semantic retrieval unavailable (%s); using lexical retrieval', type(exc).__name__)
        return {}


def index(path, download=False):
    with closing(sqlite3.connect(path, timeout=10)) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS semantic_vectors (id TEXT PRIMARY KEY, model TEXT NOT NULL, digest TEXT NOT NULL, vectors TEXT NOT NULL)')
        rows = conn.execute("SELECT id,label,props FROM nodes WHERE domain IN ('decision','debate_thread','doc') ORDER BY id").fetchall()
        existing = dict(conn.execute('SELECT id,digest FROM semantic_vectors WHERE model=?', (MODEL,)))
        count = 0
        for identifier, label, raw in rows:
            chunks = documents(label, json.loads(raw))
            fingerprint = digest(chunks)
            if existing.get(identifier) == fingerprint:
                continue
            vectors = [unit(v) for v in encoder(download).embed(chunks)]
            conn.execute('INSERT OR REPLACE INTO semantic_vectors VALUES (?,?,?,?)',
                         (identifier, MODEL, fingerprint, json.dumps(vectors)))
            # Bounded batches survive timeout/restart without losing progress.
            count += 1
            if count % 16 == 0:
                conn.commit()
        conn.execute("DELETE FROM semantic_vectors WHERE id NOT IN (SELECT id FROM nodes WHERE domain IN ('decision','debate_thread','doc'))")
        conn.commit()
        return count


if __name__ == '__main__':
    import sys
    from memory_ctx import DB_PATH
    print(f'Semantic nodes updated: {index(DB_PATH, download="--download" in sys.argv)}')
