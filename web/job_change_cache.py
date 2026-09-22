"""Shared, bounded-lifetime filesystem observations for local SSE clients."""
import copy
import hashlib
import json
import threading
import time


class JobChangeCache:
    def __init__(self, *, interval=0.5, clock=time.monotonic):
        self._interval = interval
        self._clock = clock
        self._lock = threading.Lock()
        self._next_refresh = 0.0
        self._root = None
        self._entries = {}
        self._snapshot = {}

    def invalidate(self, job_id):
        with self._lock:
            self._entries.pop(job_id, None)
            self._next_refresh = 0.0

    def snapshot(self, root, load):
        with self._lock:
            now = self._clock()
            if root == self._root and now < self._next_refresh:
                return copy.deepcopy(self._snapshot)
            if root != self._root:
                self._entries.clear()
                self._root = root
            entries, snapshot = {}, {}
            for path in root.glob('*/job.json'):
                job_id = path.parent.name
                try:
                    stat = path.stat()
                    signature = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
                    cached = self._entries.get(job_id)
                    if cached is not None and cached[0] == signature:
                        entry = cached[1]
                    else:
                        job = load(job_id)
                        source = {
                            'status': job.get('status'),
                            'stage': job.get('stage'),
                            'progress': job.get('progress'),
                            'generation': int(job.get('generation', 0) or 0),
                            'updated_at': str(job.get('updated_at') or ''),
                            'next_event_seq': int(job.get('next_event_seq', 0) or 0),
                            'archived': bool(job.get('archived')),
                        }
                        revision = hashlib.sha256(json.dumps(
                            source, sort_keys=True, ensure_ascii=False,
                            separators=(',', ':'),
                        ).encode('utf-8')).hexdigest()[:16]
                        entry = {key: source[key] for key in ('generation', 'updated_at')}
                        entry['revision'] = revision
                    entries[job_id] = (signature, entry)
                    snapshot[job_id] = entry
                except (OSError, ValueError, TypeError):
                    continue
            # Evict removed/corrupt records rather than retaining old revisions.
            self._entries = entries
            self._snapshot = snapshot
            self._next_refresh = now + self._interval
            return copy.deepcopy(snapshot)
