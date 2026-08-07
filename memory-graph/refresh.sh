#!/bin/bash
set -e
cd "$(dirname "$0")"
PY=/Users/adrianmedina/src/debate-mcp/.venv/bin/python

"$PY" ingest_claude.py
"$PY" ingest_kimi.py
"$PY" ingest_debate.py
"$PY" ingest_docs.py
"$PY" ingest_code.py
"$PY" export_kgraph.py
