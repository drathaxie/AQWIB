"""
AQWI-Bot driver.

Talks to the proxy's control port (TCP, newline-JSON) to inject packets into the
live game session. The state module observes the resulting server responses, so
verification reads come from PlayerState — not from the inject layer.

Public surface:
    Injector(host, port).send(cmd, params)   -> (ok: bool, info: str)
    QuestLoop(injector, state, qid, iters, on_event).run()
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from state import PlayerState


@dataclass
class InjectResult:
    ok: bool
    info: str


class Injector:
    """Persistent TCP connection to proxy control port. Thread-safe."""

    def __init__(self, host: str = "127.0.0.1", port: int = 7780):
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()

    def _ensure(self) -> socket.socket:
        if self._sock is None:
            s = socket.create_connection((self.host, self.port), timeout=2.0)
            s.settimeout(2.0)
            self._sock = s
        return self._sock

    def _drop(self) -> None:
        try:
            if self._sock:
                self._sock.close()
        finally:
            self._sock = None

    def _send_json(self, msg: dict) -> dict:
        with self._lock:
            for _attempt in range(2):
                try:
                    s = self._ensure()
                    s.sendall((json.dumps(msg) + "\n").encode("utf-8"))
                    buf = bytearray()
                    while b"\n" not in buf:
                        chunk = s.recv(4096)
                        if not chunk:
                            raise ConnectionError("control closed")
                        buf.extend(chunk)
                    line, _, _ = buf.partition(b"\n")
                    return json.loads(line.decode("utf-8"))
                except (OSError, ConnectionError, json.JSONDecodeError):
                    self._drop()
            return {"ok": False, "info": "proxy control unreachable"}

    def send(self, cmd: str, params: list[str] | None = None) -> InjectResult:
        pkt = {"Cmd": cmd, "Params": [str(p) for p in (params or [])]}
        resp = self._send_json({"op": "inject", "pkt": pkt})
        return InjectResult(bool(resp.get("ok")), str(resp.get("info", "")))

    def spoof(self, pkt: dict) -> InjectResult:
        """Inject a server→client packet (spoof). Free-form pkt shape — must
        include Cmd; other keys depend on what the client expects for that cmd."""
        resp = self._send_json({"op": "inject", "dir": "s2c", "pkt": pkt})
        return InjectResult(bool(resp.get("ok")), str(resp.get("info", "")))

    def status(self) -> bool:
        resp = self._send_json({"op": "status"})
        return bool(resp.get("ok") and resp.get("active"))


# ---- quest loop ----------------------------------------------------------


@dataclass
class LoopStats:
    target_qid: int = 0
    requested: int = 0
    completed_ok: int = 0
    failed: int = 0
    last_error: str = ""
    iter_durations: list[float] = field(default_factory=list)


def _wait_until(
    predicate: Callable[[], bool],
    stop: threading.Event,
    timeout: float,
    poll: float = 0.1,
) -> bool:
    """Poll until predicate() returns True or timeout/stop. Returns final value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop.is_set():
            return predicate()
        if predicate():
            return True
        time.sleep(poll)
    return predicate()


class QuestLoop:
    """
    Drives one quest repeatedly: accept → wait for accepted → tryQuestComplete →
    wait for completion bit to flip → log → loop.

    NOTE: this assumes the quest's turn-in objectives are already satisfied each
    time (e.g. a "talk-to-NPC" or "instant" quest, or the user is parked next to
    a respawning kill target and combat happens elsewhere). For kill/collect
    quests, additional steps will be needed; we'll add them once we observe the
    relevant packets for a specific test quest.
    """

    def __init__(
        self,
        injector: Injector,
        state: PlayerState,
        qid: int,
        iterations: int,
        on_event: Callable[[str, dict], None] | None = None,
        accept_timeout: float = 5.0,
        complete_timeout: float = 8.0,
        between_iters: float = 0.5,
    ):
        self.injector = injector
        self.state = state
        self.qid = qid
        self.iterations = iterations
        self.on_event = on_event or (lambda kind, payload: None)
        self.accept_timeout = accept_timeout
        self.complete_timeout = complete_timeout
        self.between_iters = between_iters

        self.stats = LoopStats(target_qid=qid)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _emit(self, kind: str, **payload) -> None:
        try:
            self.on_event(kind, payload)
        except Exception:
            pass

    def _run(self) -> None:
        self._emit("start", qid=self.qid, iterations=self.iterations)
        for i in range(1, self.iterations + 1):
            if self._stop.is_set():
                break
            t0 = time.monotonic()
            ok, err = self._one_iteration(i)
            elapsed = time.monotonic() - t0
            self.stats.iter_durations.append(elapsed)
            self.stats.requested += 1
            if ok:
                self.stats.completed_ok += 1
                self._emit("iter_ok", n=i, secs=elapsed)
            else:
                self.stats.failed += 1
                self.stats.last_error = err
                self._emit("iter_fail", n=i, err=err)
                # Halt-on-mismatch is the whole QA value prop.
                break
            if self._stop.is_set():
                break
            time.sleep(self.between_iters)
        self._emit("done", stats=self.stats)

    def _one_iteration(self, n: int) -> tuple[bool, str]:
        qid = self.qid

        # 1. Accept (idempotent — server should no-op if already accepted)
        r = self.injector.send("acceptQuest", [qid])
        if not r.ok:
            return False, f"acceptQuest send failed: {r.info}"

        if not _wait_until(
            lambda: qid in self.state.quests_accepted,
            self._stop,
            self.accept_timeout,
        ):
            return False, f"quest {qid} did not appear in accepted list within {self.accept_timeout}s"

        # 2. Snapshot pre-completion state for delta
        pre_complete = self.state.is_quest_complete(qid)
        pre_count = self.state.quests_complete_count()

        # 3. Try complete
        r = self.injector.send("tryQuestComplete", [qid])
        if not r.ok:
            return False, f"tryQuestComplete send failed: {r.info}"

        # 4. Wait for server-side state change. For repeatable quests the
        #    completion bit doesn't necessarily flip "on" (it may already be
        #    set, or be reset immediately). The reliable signal is that the
        #    quest leaves quests_accepted, OR updateQuestBits arrives at all.
        baseline_accepted = qid in self.state.quests_accepted
        baseline_bits = self.state.quest_bits

        def changed() -> bool:
            return (
                self.state.quest_bits is not baseline_bits
                and self.state.quest_bits != baseline_bits
            ) or (qid not in self.state.quests_accepted) != (not baseline_accepted)

        if not _wait_until(changed, self._stop, self.complete_timeout):
            return (
                False,
                f"no server-side change after tryQuestComplete within {self.complete_timeout}s "
                f"(pre_complete={pre_complete}, pre_count={pre_count})",
            )

        return True, ""
