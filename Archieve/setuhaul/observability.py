"""Trace = one /chat request; span = one timed step (S8). Never log tokens or phone numbers."""
import json, time, uuid
from contextlib import contextmanager

class Tracer:
    def __init__(self, path=None): self.spans, self.path, self._stack, self.request_id = [], path, [], None
    def new_request(self): self.request_id = uuid.uuid4().hex[:8]; self.spans_start = len(self.spans); return self.request_id
    @contextmanager
    def span(self, name, **attrs):
        sid = uuid.uuid4().hex[:6]; parent = self._stack[-1] if self._stack else None
        self._stack.append(sid); t0 = time.perf_counter(); rec = dict(request_id=self.request_id, span_id=sid, parent=parent, name=name, **attrs)
        try: yield rec
        except Exception as e: rec["error"] = repr(e); raise
        finally:
            self._stack.pop(); rec["ms"] = round((time.perf_counter() - t0) * 1000, 2); self.spans.append(rec)
            if self.path:
                with open(self.path, "a") as f: f.write(json.dumps(rec, default=str) + "\n")
    def tool_calls_per_trace(self, rid):
        return sum(1 for s in self.spans if s["request_id"] == rid and s["name"].startswith("tool."))
