# -*- coding: utf-8 -*-
"""dsh_client.py - transport + event correlation for the DSH WeChat bridge plugin.

Contract (see 文档/规范/DSH微信桥接-探测报告-DSH端.md and DEV-INTERFACES.md):
  GET  {origin}{prefix}/health                      (no auth, loopback only)
  GET  {origin}{prefix}/status                      (Bearer token)
  POST {origin}{prefix}/inbound                     {requestId,mode,content,clientTimeZone}
  GET  {origin}{prefix}/events?since=<seq>&timeout=<ms>  long poll (Bearer token)

Runtime discovery:  ~/.dsh/dsh-wechat-bridge/runtime.json + token
  {pid, origin, pathPrefix, sessionId, title, startedAt}

Correlation: requestId -> user/message.source.rpcId -> turn -> assistant/message*
             -> turn/end(reason.kind == completed) -> last assistant text of turn
The plugin's sessionId is fixed; the bridge never sends sessionId itself.

Classes:
  DshError(code, message, status, details)
  DshOffline(DshError)              # plugin not reachable / not live yet
  DshClient                         # thin HTTP client + runtime discovery
  DshSession                        # poll thread + correlation + result queue
"""
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request

EVENT_TYPES_RAW = ("user/message", "assistant/message", "turn/start", "turn/end",
                   "session/event")


class DshError(RuntimeError):
    def __init__(self, code, message, status=None, details=None):
        super(DshError, self).__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


class DshOffline(DshError):
    def __init__(self, message="DSH plugin offline", code="BRIDGE_DSH_UNAVAILABLE"):
        super(DshOffline, self).__init__(code, message)


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _dig_key(obj, key, depth=0):
    """Find the first `key` value in a nested dict/list (bounded depth)."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        if key in obj and not isinstance(obj[key], (dict, list)):
            return obj[key]
        for v in obj.values():
            got = _dig_key(v, key, depth + 1)
            if got is not None:
                return got
    elif isinstance(obj, list):
        for v in obj[:20]:
            got = _dig_key(v, key, depth + 1)
            if got is not None:
                return got
    return None


def _assistant_text(data):
    """Extract text parts from an assistant/message event data payload."""
    if not isinstance(data, dict):
        return ""
    msg = data.get("message") if isinstance(data.get("message"), dict) else data
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and part.get("text"):
            parts.append(str(part["text"]))
    return "\n".join(parts).strip()


def _event_turn(ev):
    data = ev.get("data") if isinstance(ev, dict) else None
    if isinstance(data, dict):
        t = data.get("turn")
        if isinstance(t, int):
            return t
        t = _dig_key(data, "turn")
        if isinstance(t, int):
            return t
    return None


class DshClient(object):
    def __init__(self, cfg, log):
        dcfg = cfg.get("dsh", {}) if cfg else {}
        self.runtime_file = dcfg.get("runtime_file") or os.path.join(
            os.path.expanduser("~"), ".dsh", "dsh-wechat-bridge", "runtime.json")
        self.token_file = dcfg.get("token_file") or os.path.join(
            os.path.dirname(self.runtime_file), "token")
        self.expected_title = dcfg.get("expected_session_title") or ""
        self.inbound_timeout = dcfg.get("inbound_timeout_ms", 15000) / 1000.0
        self.poll_timeout_ms = int(dcfg.get("poll_timeout_ms", 20000))
        self.health_timeout = dcfg.get("health_timeout_ms", 3000) / 1000.0
        self.client_time_zone = dcfg.get("client_time_zone", "Asia/Shanghai")
        self.log = log
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.origin = None
        self.prefix = ""
        self.token = None
        self.session_id = None
        self.title = None
        self.pid = None
        self.ready = False
        self.last_error = ""
        self._runtime_mtime = None
        self._token_mtime = None

    # ---------------- discovery ----------------
    @property
    def base(self):
        if not self.origin:
            return ""
        return self.origin.rstrip("/") + (self.prefix or "")

    def load_runtime(self):
        """Read runtime.json + token. Returns True when both parse."""
        try:
            st = os.stat(self.runtime_file)
        except OSError:
            self.last_error = "runtime.json missing: %s" % self.runtime_file
            return False
        try:
            rt = _read_json(self.runtime_file)
        except Exception as exc:  # noqa: BLE001
            self.last_error = "runtime.json invalid: %s" % exc
            return False
        origin = rt.get("origin") or ""
        prefix = rt.get("pathPrefix") or ""
        if not origin:
            self.last_error = "runtime.json has no origin"
            return False
        try:
            tst = os.stat(self.token_file)
            with open(self.token_file, "r", encoding="utf-8") as f:
                token = f.read().strip()
        except OSError as exc:
            self.last_error = "token missing: %s" % exc
            return False
        if not token:
            self.last_error = "token empty"
            return False
        changed = (self.origin != origin or self.prefix != prefix
                   or self.token != token or self.pid != rt.get("pid")
                   or self.session_id != rt.get("sessionId"))
        self.origin = origin
        self.prefix = prefix
        self.token = token
        self.session_id = rt.get("sessionId")
        self.title = rt.get("title") or rt.get("sessionTitle")
        self.pid = rt.get("pid")
        self._runtime_mtime = st.st_mtime
        self._token_mtime = tst.st_mtime
        return changed

    def runtime_changed(self):
        try:
            if os.stat(self.runtime_file).st_mtime != self._runtime_mtime:
                return True
            if os.stat(self.token_file).st_mtime != self._token_mtime:
                return True
        except OSError:
            return True
        return False

    # ---------------- HTTP ----------------
    def _request(self, method, path, body=None, auth=True, timeout=None, raw=False):
        if not self.base:
            raise DshOffline("runtime not loaded")
        url = self.base + path
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if auth:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=timeout or self.inbound_timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            text = ""
            try:
                text = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            payload = {}
            try:
                payload = json.loads(text) if text else {}
            except Exception:
                payload = {}
            err = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(err, dict):
                raise DshError(err.get("code") or "HTTP_%d" % exc.code,
                               err.get("message") or text[:200],
                               exc.code, err.get("details"))
            raise DshError("HTTP_%d" % exc.code, text[:200] or str(exc), exc.code)
        except (urllib.error.URLError, OSError) as exc:
            raise DshOffline("connect %s failed: %s" % (url, exc))
        if raw:
            return text
        try:
            return json.loads(text) if text else {}
        except Exception:
            raise DshError("BRIDGE_BAD_JSON", "response is not json: %r" % text[:200])

    def health(self):
        """Unauthenticated liveness check; returns dict or raises DshOffline."""
        try:
            resp = self._request("GET", "/health", auth=False,
                                 timeout=self.health_timeout)
        except DshError as exc:
            raise DshOffline("health failed: %s" % exc.message, exc.code)
        if not resp.get("ok"):
            raise DshOffline("health not ok: %r" % (resp,))
        return resp

    def status(self):
        return self._request("GET", "/status", timeout=self.health_timeout)

    def ensure_ready(self, force=False):
        """Load runtime (if changed), check /health and /status. Never raises."""
        try:
            changed = self.load_runtime()
            if not self.ready or force or changed:
                if not self.origin:
                    self.ready = False
                    return False
                h = self.health()
                st = self.status()
                self.session_id = st.get("sessionId") or self.session_id
                self.title = st.get("title") or self.title
                self.ready = True
                self.last_error = ""
                self.log("dsh.ready", origin=self.origin, session=self.session_id,
                         title=self.title, last_seq=st.get("lastSeq"), pid=self.pid)
                if self.expected_title and self.title and self.expected_title not in self.title:
                    self.log("dsh.title_mismatch", expected=self.expected_title,
                             actual=self.title)
            return self.ready
        except DshError as exc:
            self.ready = False
            self.last_error = "%s" % exc
            self.log("dsh.not_ready", error=self.last_error)
            return False
        except Exception as exc:  # noqa: BLE001
            self.ready = False
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            self.log("dsh.not_ready", error=self.last_error)
            return False

    def post_inbound(self, request_id, parts, mode="queue"):
        body = {
            "requestId": request_id,
            "mode": mode,
            "content": parts,
            "clientTimeZone": self.client_time_zone,
        }
        return self._request("POST", "/inbound", body=body,
                             timeout=self.inbound_timeout)

    def get_events(self, since, timeout_ms=None):
        timeout_ms = int(timeout_ms or self.poll_timeout_ms)
        path = "/events?since=%d&timeout=%d" % (int(since), timeout_ms)
        sock_timeout = timeout_ms / 1000.0 + 8.0
        return self._request("GET", path, timeout=sock_timeout)

    def close(self):
        try:
            self._opener.close()
        except Exception:
            pass


class DshSession(object):
    """Keeps a long-poll thread alive and correlates requests to turn results."""

    def __init__(self, cfg, log, on_event=None):
        self.cfg = cfg
        self.log = log
        self.client = DshClient(cfg, log)
        dcfg = cfg.get("dsh", {}) if cfg else {}
        self.reply_timeout = dcfg.get("reply_timeout_ms", 600000) / 1000.0
        self.reconnect_backoff = dcfg.get("reconnect_backoff_ms", 5000) / 1000.0
        self.on_event = on_event
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._wake = threading.Event()
        self._results = {}
        self._result_queue = queue.Queue()
        self._pending = {}
        self._turn_text = {}
        self._turn_reason = {}
        self._turn_order = []
        self._current_turn = None
        self.last_seq = 0
        self.running = False
        self.last_error = ""
        self.connected = False
        self._pid_seen = None

    # ---------------- lifecycle ----------------
    def start(self):
        self.client.ensure_ready()
        if self.client.ready:
            try:
                st = self.client.status()
                self.last_seq = int(st.get("lastSeq") or 0)
                self.running = bool(st.get("running"))
                self._pid_seen = self.client.pid
                self.connected = True
                self.log("dsh.start", since=self.last_seq, running=self.running)
            except DshError as exc:
                self.connected = False
                self.last_error = str(exc)
                self.log("dsh.start_status_failed", error=self.last_error)
        else:
            self.connected = False
            self.last_error = self.client.last_error
            self.log("dsh.start_offline", error=self.last_error)
        self._thread = threading.Thread(target=self._poll_loop, name="dsh-events",
                                        daemon=True)
        self._thread.start()
        return self.client.ready

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        self.client.close()

    # ---------------- public API ----------------
    def submit(self, request_id, parts, mode="queue"):
        """POST /inbound. Raises DshOffline / DshError. Idempotent by requestId."""
        if not self.client.ready:
            self.client.ensure_ready()
        if not self.client.ready:
            raise DshOffline(self.client.last_error or "DSH plugin offline")
        with self._lock:
            if request_id not in self._pending:
                self._pending[request_id] = {
                    "submitted": time.time(), "turn": None,
                    "request_seq": None, "mode": mode,
                }
        resp = self.client.post_inbound(request_id, parts, mode=mode)
        if isinstance(resp, dict):
            ls = resp.get("lastSeq")
            if isinstance(ls, int) and ls > self.last_seq:
                self.last_seq = ls
            self.running = bool(resp.get("running", self.running))
            self.log("dsh.inbound_ok", request=request_id,
                     accepted=resp.get("accepted"), queued=resp.get("queued"),
                     running=resp.get("running"))
        self._wake.set()
        return resp

    def pop_results(self):
        out = []
        while True:
            try:
                out.append(self._result_queue.get_nowait())
            except queue.Empty:
                break
        return out

    def pending(self, request_id=None):
        with self._lock:
            if request_id:
                p = self._pending.get(request_id)
                return dict(p) if p else None
            return {k: dict(v) for k, v in self._pending.items()}

    def request_timed_out(self, request_id, now=None):
        p = self.pending(request_id)
        if not p:
            return False
        now = now or time.time()
        return (now - p["submitted"]) > self.reply_timeout

    def force_fail(self, request_id, reason, text=""):
        self._finish(request_id, ok=False, text=text, reason=reason)

    def connection_state(self):
        return {
            "connected": self.connected,
            "ready": self.client.ready,
            "last_seq": self.last_seq,
            "running": self.running,
            "session": self.client.session_id,
            "title": self.client.title,
            "error": self.last_error or self.client.last_error,
        }

    # ---------------- poll loop ----------------
    def _poll_loop(self):
        backoff = 0.5
        while not self._stop.is_set():
            if not self.client.ready:
                ok = self.client.ensure_ready()
                if not ok:
                    self.connected = False
                    self.last_error = self.client.last_error
                    self._stop.wait(min(self.reconnect_backoff, max(backoff, 1.0)))
                    backoff = min(backoff * 1.5, self.reconnect_backoff)
                    continue
                # (re)connect: align sequence with the plugin's current state
                try:
                    st = self.client.status()
                    if self._pid_seen != self.client.pid:
                        self._pid_seen = self.client.pid
                        self.last_seq = int(st.get("lastSeq") or 0)
                        self.log("dsh.reconnect_reset", pid=self.client.pid,
                                 since=self.last_seq)
                    self.running = bool(st.get("running"))
                except DshError as exc:
                    self.log("dsh.status_error", error=str(exc))
                self.connected = True
                self.last_error = ""
                backoff = 0.5
            try:
                resp = self.client.get_events(self.last_seq, self.client.poll_timeout_ms)
            except DshOffline as exc:
                self.connected = False
                self.client.ready = False
                self.last_error = str(exc)
                self.log("dsh.events_offline", error=self.last_error)
                self._stop.wait(1.0)
                continue
            except DshError as exc:
                self.log("dsh.events_error", error=str(exc), code=exc.code)
                if exc.status in (401, 403):
                    self.connected = False
                    self.client.ready = False
                    self._stop.wait(1.0)
                else:
                    self._stop.wait(0.5)
                continue
            if not isinstance(resp, dict):
                continue
            self.connected = True
            events = resp.get("events") or []
            for ev in events:
                try:
                    self._handle_event(ev)
                except Exception as exc:  # noqa: BLE001
                    self.log("dsh.event_error", error="%s: %s" % (type(exc).__name__, exc))
            ls = resp.get("lastSeq")
            if isinstance(ls, int) and ls > self.last_seq:
                self.last_seq = ls
            if not events:
                # long poll returned nothing: tiny pause only if server ignored timeout
                self._wake.wait(0.2)
                self._wake.clear()
            else:
                backoff = 0.5

    # ---------------- event handling ----------------
    def _handle_event(self, ev):
        if not isinstance(ev, dict):
            return
        etype = ev.get("type") or ""
        seq = ev.get("seq")
        if isinstance(seq, int) and seq > self.last_seq:
            self.last_seq = seq
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:
                pass
        if etype == "snapshot":
            data = ev.get("data") or {}
            ls = data.get("lastSeq") if isinstance(data, dict) else None
            if isinstance(ls, int):
                self.last_seq = max(self.last_seq, ls)
            self.log("dsh.snapshot", last_seq=self.last_seq,
                     running=(data or {}).get("running") if isinstance(data, dict) else None)
            return
        if etype == "resync":
            # too old: realign with /status instead of replaying old events
            try:
                st = self.client.status()
                self.last_seq = int(st.get("lastSeq") or self.last_seq)
                self.log("dsh.resync", last_seq=self.last_seq,
                         reason=(ev.get("data") or {}).get("reason"))
            except DshError as exc:
                self.log("dsh.resync_error", error=str(exc))
            return
        if etype == "status":
            data = ev.get("data") or {}
            if isinstance(data, dict) and "running" in data:
                self.running = bool(data.get("running"))
            return
        if etype == "assistant":
            data = ev.get("data") or {}
            if isinstance(data, dict):
                turn = data.get("turn")
                if isinstance(turn, int):
                    self._record_text(turn, data.get("text") or "")
            return
        if etype == "turn":
            data = ev.get("data") or {}
            if isinstance(data, dict):
                turn = data.get("turn")
                reason = data.get("reason") or {"kind": "completed"}
                if isinstance(turn, int):
                    self._record_turn_end(turn, reason)
            return
        if etype in EVENT_TYPES_RAW or etype == "session":
            raw = ev
            if etype == "session":
                data = ev.get("data")
                if isinstance(data, dict) and data.get("type"):
                    raw = data
                else:
                    raw = data if isinstance(data, dict) else ev
            self._handle_session_event(raw)
            return
        # unknown frame types are ignored on purpose

    def _handle_session_event(self, raw):
        if not isinstance(raw, dict):
            return
        etype = raw.get("type") or ""
        seq = raw.get("seq")
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
        if isinstance(seq, int) and seq > self.last_seq:
            self.last_seq = seq
        if etype == "turn/start":
            turn = data.get("turn") if isinstance(data.get("turn"), int) else _event_turn(raw)
            if isinstance(turn, int):
                self._current_turn = turn
            return
        if etype == "user/message":
            rpc = data.get("rpcId")
            if rpc is None:
                rpc = _dig_key(data, "rpcId")
            if not rpc:
                return
            turn = data.get("turn") if isinstance(data.get("turn"), int) else self._current_turn
            with self._lock:
                p = self._pending.get(rpc)
                if p is not None:
                    p["request_seq"] = seq if isinstance(seq, int) else p.get("request_seq")
                    if isinstance(turn, int):
                        p["turn"] = turn
            self.log("dsh.user_message", request=rpc, turn=turn)
            return
        if etype == "assistant/message":
            turn = data.get("turn") if isinstance(data.get("turn"), int) else _event_turn(raw)
            text = _assistant_text(data)
            if isinstance(turn, int) and text:
                self._record_text(turn, text)
            return
        if etype == "turn/end":
            turn = data.get("turn") if isinstance(data.get("turn"), int) else _event_turn(raw)
            reason = data.get("reason") or {"kind": "completed"}
            if isinstance(turn, int):
                self._record_turn_end(turn, reason)
            return

    def _record_text(self, turn, text):
        if not text:
            return
        with self._lock:
            self._turn_text[turn] = text.strip()
            if turn not in self._turn_order:
                self._turn_order.append(turn)
            if len(self._turn_order) > 500:
                old = self._turn_order.pop(0)
                self._turn_text.pop(old, None)
                self._turn_reason.pop(old, None)

    def _record_turn_end(self, turn, reason):
        kind = (reason or {}).get("kind")
        with self._lock:
            self._turn_reason[turn] = reason or {"kind": kind}
            self.running = False
            text = self._turn_text.get(turn, "")
            rids = [rid for rid, p in self._pending.items() if p.get("turn") == turn]
        self.log("dsh.turn_end", turn=turn, kind=kind, chars=len(text), requests=len(rids))
        for rid in rids:
            ok = (kind == "completed")
            err = ""
            if not ok:
                e = (reason or {}).get("error") or {}
                if isinstance(e, dict):
                    err = "%s %s" % (e.get("code") or "", e.get("message") or "")
                err = err.strip() or ("turn end: %s" % kind)
            self._finish(rid, ok=ok, text=text, reason=reason, error=err)

    def _finish(self, request_id, ok, text="", reason=None, error=""):
        kind = (reason or {}).get("kind")
        with self._lock:
            if request_id not in self._pending:
                return  # already finished (late duplicate) or never tracked
            p = self._pending.pop(request_id)
        result = {
            "request_id": request_id,
            "ok": bool(ok),
            "text": text or "",
            "reason": reason or ({"kind": kind} if kind else {}),
            "error": error,
            "turn": p.get("turn"),
            "request_seq": p.get("request_seq"),
            "ts": time.time(),
        }
        self._results[request_id] = result
        self._result_queue.put(result)
        self.log("dsh.result", request=request_id, ok=result["ok"],
                 turn=result["turn"], chars=len(result["text"]),
                 error=error[:160])
