"""Background graph refresh owned by the relay, independent of model turns."""
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from config import CONNINFO

INTERVAL = 30
TIMEOUT = 60
SCRIPT = Path(__file__).resolve().parent.parent / 'memory-graph' / 'ingest_debate.py'
log = logging.getLogger(__name__)


class MemorySync:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = {'status': 'starting', 'last_success': None, 'failures': 0}
        self._stop = threading.Event()
        self._thread = None

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def run_once(self):
        try:
            env = dict(os.environ, DEBATE_CONNINFO=CONNINFO)
            subprocess.run([sys.executable, str(SCRIPT)], env=env,
                           cwd=str(SCRIPT.parent), check=True, timeout=TIMEOUT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except (OSError, subprocess.SubprocessError) as exc:
            # Do not copy stderr into the public heartbeat: it may contain data.
            log.warning('Memory sync failed: %s', type(exc).__name__)
            with self._lock:
                self._state.update(status='error', error=type(exc).__name__,
                                   failures=self._state['failures'] + 1)
        else:
            with self._lock:
                self._state.update(status='ok', last_success=time.time(), failures=0)
                self._state.pop('error', None)

    def _run(self):
        while not self._stop.is_set():
            self.run_once()
            delay = min(300, INTERVAL * 2 ** min(self.snapshot()['failures'], 3))
            self._stop.wait(delay)

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name='memory-sync', daemon=True)
            self._thread.start()


sync = MemorySync()
