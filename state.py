"""
Packet-driven player state.

Ingests c2s/s2c JSON packets observed by the proxy and maintains a live snapshot
of the player: name, zone, quests accepted/completed, cached quest definitions.

All field interpretations come from observed packets in logs/packets.jsonl. When
new packet shapes appear, add a handler here rather than special-casing in GUI.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class PlayerState:
    name: str = ""
    user_id: int = 0
    server_time: str = ""
    zone: str = ""
    cell: str = ""
    # Quest completion is a server-sent bitset; bit N == quest ID N complete.
    quest_bits: bytes = b""
    quests_accepted: list[int] = field(default_factory=list)
    # Quest definitions cached from getQuests responses: {qid: full_def_dict}
    quest_defs: dict[int, dict] = field(default_factory=dict)
    # Total packet counts for the status bar
    packets_seen: int = 0
    last_packet_ts: float = 0.0
    last_cmd: str = ""

    def is_quest_complete(self, qid: int) -> bool:
        byte_idx, bit_idx = divmod(qid, 8)
        if byte_idx >= len(self.quest_bits):
            return False
        return bool(self.quest_bits[byte_idx] & (1 << bit_idx))

    def quests_complete_count(self) -> int:
        return sum(bin(b).count("1") for b in self.quest_bits)


# Server response field name typo: "questsCopmlete" — see Player.log finding.
# Tolerate both spellings so a server-side fix doesn't break us.
_QUEST_COMPLETE_KEYS = ("questsComplete", "questsCopmlete")


def _apply(state: PlayerState, direction: str, pkt: dict) -> None:
    cmd = pkt.get("Cmd")
    state.packets_seen += 1
    state.last_cmd = cmd or "?"

    if direction == "c2s":
        if cmd == "Login":
            params = pkt.get("Params", [])
            if len(params) >= 2:
                state.name = params[1]
        return

    # s2c
    if cmd == "loginResponse":
        state.name = pkt.get("Username", state.name)
        state.user_id = pkt.get("UserID", state.user_id)
        state.server_time = pkt.get("serverTime", state.server_time)
    elif cmd == "initPlayer":
        user = pkt.get("user", {}) or {}
        state.name = user.get("Name", state.name)
    elif cmd == "questData":
        state.quests_accepted = list(pkt.get("questsAccepted", []))
        for k in _QUEST_COMPLETE_KEYS:
            if k in pkt:
                state.quest_bits = bytes(pkt[k])
                break
    elif cmd == "updateQuestBits":
        bits = pkt.get("qComplete") or pkt.get("questsComplete")
        if bits is not None:
            state.quest_bits = bytes(bits)
    elif cmd == "getQuests":
        quests = pkt.get("quests", {}) or {}
        for qid_str, qdef in quests.items():
            try:
                state.quest_defs[int(qid_str)] = qdef
            except (TypeError, ValueError):
                continue
    elif cmd == "AreaJoin":
        # Field name is a best guess — overwrite on the first non-empty hit
        for k in ("strMapName", "MapName", "Map", "mapName"):
            v = pkt.get(k)
            if v:
                state.zone = v
                break
    elif cmd == "CellJoin" or cmd == "CellMove":
        for k in ("strFrame", "Frame", "cell", "Cell"):
            v = pkt.get(k)
            if v:
                state.cell = v
                break


class PacketTail:
    """
    Tails logs/packets.jsonl, feeds each packet to the state object, and
    invokes on_packet(direction, pkt) callbacks for UI consumers.

    Runs in a background thread. Re-opens the file if it disappears (e.g. user
    rotates logs while running).
    """

    def __init__(
        self,
        path: Path,
        state: PlayerState,
        on_packet: Callable[[str, dict], None] | None = None,
        from_start: bool = True,
    ):
        self.path = path
        self.state = state
        self.on_packet = on_packet
        self.from_start = from_start
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self.path.exists():
                time.sleep(0.5)
                continue
            try:
                with self.path.open("r", encoding="utf-8") as fh:
                    if not self.from_start:
                        fh.seek(0, 2)  # jump to end
                    self._follow(fh)
            except FileNotFoundError:
                time.sleep(0.5)

    def _follow(self, fh) -> None:
        while not self._stop.is_set():
            line = fh.readline()
            if not line:
                time.sleep(0.05)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not entry.get("ok"):
                continue
            pkt = entry.get("pkt") or {}
            direction = entry.get("dir", "?")
            self.state.last_packet_ts = entry.get("ts", time.time())
            _apply(self.state, direction, pkt)
            if self.on_packet:
                try:
                    self.on_packet(direction, pkt)
                except Exception:
                    pass
