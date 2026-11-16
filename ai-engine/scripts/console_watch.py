"""
Console watcher: keeps a listening Telnet connection open on every router/PC console of a GNS3
project. GNS3 consoles send their output to every connected client, so a ping typed by hand in a
GNS3 console window (and its result) is seen here too. Each finished ping is stored as an event
that the dashboard shows as the "ping history".
"""
import socket
import threading
import time
from collections import deque

import gns3_service as g

_REPLAY_SECONDS = 2.5          # a console replays its old output when a client connects
_events = deque(maxlen=400)    # live ping events, oldest first
_replayed = {}                 # device -> {ip: record} found in the replayed history
_watchers = {}                 # (host, port) -> ConsoleWatcher
_lock = threading.Lock()


def _strip_iac(chunk, sock):
    out, i = bytearray(), 0
    while i < len(chunk):
        if chunk[i] == 255 and i + 2 < len(chunk):
            if chunk[i + 1] in (253, 251):
                try:
                    sock.sendall(bytes([255, 252 if chunk[i + 1] == 253 else 254, chunk[i + 2]]))
                except OSError:
                    pass
            i += 3
        else:
            out.append(chunk[i])
            i += 1
    return out.decode("ascii", "replace").replace("\r", "")


class ConsoleWatcher(threading.Thread):
    def __init__(self, device, host, port):
        super().__init__(daemon=True, name=f"watch-{device}")
        self.device, self.host, self.port = device, host, port
        self.stop_flag = False

    def run(self):
        while not self.stop_flag:
            try:
                self._session()
            except Exception:
                pass
            time.sleep(4)

    def _session(self):
        sock = socket.create_connection((self.host, self.port), timeout=8)
        sock.settimeout(0.5)
        started, replay, pending, replay_done = time.time(), "", "", False
        try:
            while not self.stop_flag:
                text = ""
                try:
                    data = sock.recv(4096)
                    if not data:
                        return
                    text = _strip_iac(data, sock)
                except socket.timeout:
                    pass

                if not replay_done:
                    replay += text
                    if time.time() - started >= _REPLAY_SECONDS:
                        replay_done = True
                        latest = {}
                        for r in g._parse_ping_history(replay):
                            latest[r["ip"]] = r
                        with _lock:
                            _replayed[self.device] = latest
                    continue

                pending += text
                if len(pending) > 20000:
                    pending = pending[-2000:]
                # a finished ping is followed by the device prompt
                if pending.rstrip().endswith((">", "#")):
                    for r in g._parse_ping_history(pending):
                        r["time"] = time.strftime("%H:%M:%S")
                        r["source"] = self.device
                        r["origin"] = "Website" if g._web_ping_recent(self.host, self.port, r["ip"]) else "GNS3 console"
                        with _lock:
                            _events.append(r)
                    # keep the prompt line: the next command is echoed right after it
                    pending = pending.rstrip("\n").split("\n")[-1]
        finally:
            sock.close()


def ensure_watchers(devices):
    """devices: list of (name, host, port). Returns True if any new watcher was started."""
    started = False
    with _lock:
        for name, host, port in devices:
            key = (host, port)
            w = _watchers.get(key)
            if w is None or not w.is_alive():
                w = ConsoleWatcher(name, host, port)
                _watchers[key] = w
                w.start()
                started = True
    return started


def snapshot(device_names):
    """(live events newest first, replayed latest-per-pair) for the given devices."""
    names = set(device_names)
    with _lock:
        live = [e for e in _events if e["source"] in names]
        replayed = {d: dict(v) for d, v in _replayed.items() if d in names}
    live.reverse()
    return live, replayed
