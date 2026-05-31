"""
AQWI MITM proxy.

Listens on a local TCP port, forwards bytes to the real AEC server, and logs every
null-terminated JSON packet to logs/packets.jsonl with direction + timestamp.

Wire format (from Assembly-CSharp.dll AEC.cs):
    UTF-8 JSON of Request{Cmd, Params[]}  +  one 0x00 byte

The XOR routine EncryptDecrypt() in AEC.cs exists but is never called, so the
stream is plaintext.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONTROL_PORT_DEFAULT = 7780


@dataclass
class Config:
    listen_host: str
    listen_port: int
    upstream_host: str
    upstream_port: int
    packets_path: Path
    console: bool
    control_port: int

    @classmethod
    def load(cls, path: Path) -> "Config":
        with path.open() as f:
            data = yaml.safe_load(f)
        return cls(
            listen_host=data["listen"]["host"],
            listen_port=int(data["listen"]["port"]),
            upstream_host=data["upstream"]["host"],
            upstream_port=int(data["upstream"]["port"]),
            packets_path=ROOT / data["logging"]["packets_jsonl"],
            console=bool(data["logging"]["console"]),
            control_port=int(data.get("control", {}).get("port", CONTROL_PORT_DEFAULT)),
        )


class InjectHub:
    """
    Tracks the most-recently-connected game session's writers so the control
    server can inject packets either direction:
      - c2s: write to upstream (server thinks the client sent it)
      - s2c: write to client  (client thinks the server sent it — spoofing)
    """

    def __init__(self) -> None:
        self._upstream: asyncio.StreamWriter | None = None
        self._client: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    def set(
        self,
        upstream: asyncio.StreamWriter | None,
        client: asyncio.StreamWriter | None = None,
    ) -> None:
        self._upstream = upstream
        if client is not None:
            self._client = client

    def clear_if(self, writer: asyncio.StreamWriter) -> None:
        if self._upstream is writer:
            self._upstream = None
        if self._client is writer:
            self._client = None

    async def inject(
        self, pkt: dict, log: "PacketLog", direction: str = "c2s"
    ) -> tuple[bool, str]:
        if direction == "c2s":
            w = self._upstream
        elif direction == "s2c":
            w = self._client
        else:
            return False, f"bad direction: {direction!r}"
        if w is None or w.is_closing():
            return False, f"no active {direction} writer"
        data = json.dumps(pkt, separators=(",", ":")).encode("utf-8") + b"\x00"
        async with self._lock:
            try:
                w.write(data)
                await w.drain()
            except (ConnectionResetError, BrokenPipeError) as e:
                return False, f"write failed: {e}"
        log.write(direction, data[:-1], synthetic=True)
        return True, "ok"


class PacketLog:
    def __init__(self, path: Path, console: bool):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", buffering=1, encoding="utf-8")
        self._console = console

    def write(self, direction: str, raw: bytes, synthetic: bool = False) -> None:
        ts = time.time()
        text = raw.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(text)
            entry = {"ts": ts, "dir": direction, "ok": True, "pkt": parsed}
        except json.JSONDecodeError as e:
            entry = {"ts": ts, "dir": direction, "ok": False, "raw": text, "err": str(e)}
        if synthetic:
            entry["src"] = "inject"
        line = json.dumps(entry, separators=(",", ":"))
        self._fh.write(line + "\n")
        if self._console:
            arrow = "⇉" if synthetic else ("→" if direction == "c2s" else "←")
            cmd = entry.get("pkt", {}).get("Cmd") if entry["ok"] else "?"
            print(f"{arrow} {cmd}  {text[:200]}", file=sys.stderr)

    def close(self) -> None:
        self._fh.close()


async def pump(
    src: asyncio.StreamReader,
    dst: asyncio.StreamWriter,
    direction: str,
    log: PacketLog,
) -> None:
    """Read null-terminated frames from src, log each, forward bytes verbatim to dst."""
    buf = bytearray()
    try:
        while True:
            chunk = await src.read(4096)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
            buf.extend(chunk)
            while True:
                nul = buf.find(0)
                if nul < 0:
                    break
                frame = bytes(buf[:nul])
                del buf[: nul + 1]
                if frame:
                    log.write(direction, frame)
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass


async def handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    cfg: Config,
    log: PacketLog,
    hub: InjectHub,
) -> None:
    peer = client_writer.get_extra_info("peername")
    print(f"[+] client connected from {peer}", file=sys.stderr)
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(
            cfg.upstream_host, cfg.upstream_port
        )
    except OSError as e:
        print(f"[!] upstream connect failed: {e}", file=sys.stderr)
        client_writer.close()
        return
    print(
        f"[+] upstream connected to {cfg.upstream_host}:{cfg.upstream_port}",
        file=sys.stderr,
    )
    hub.set(upstream_writer, client_writer)
    try:
        await asyncio.gather(
            pump(client_reader, upstream_writer, "c2s", log),
            pump(upstream_reader, client_writer, "s2c", log),
        )
    finally:
        hub.clear_if(upstream_writer)
    print(f"[-] client {peer} disconnected", file=sys.stderr)


async def handle_control(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    hub: InjectHub,
    log: PacketLog,
) -> None:
    """
    Newline-JSON control protocol on 127.0.0.1:control_port.

    Request:  one JSON object per line, either
              {"op":"inject","pkt":{"Cmd":"...","Params":[...]}}
              {"op":"status"}
    Response: one JSON object per line: {"ok":true/false, ...}
    """
    peer = writer.get_extra_info("peername")
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as e:
                resp = {"ok": False, "err": f"bad json: {e}"}
            else:
                op = msg.get("op")
                if op == "inject":
                    pkt = msg.get("pkt") or {}
                    direction = msg.get("dir", "c2s")
                    # c2s requires {Cmd, Params}; s2c is free-form (server msgs
                    # use varied shapes like {Cmd, msg, Name, channel}).
                    if not isinstance(pkt, dict) or "Cmd" not in pkt:
                        resp = {"ok": False, "err": "pkt must contain Cmd"}
                    else:
                        ok, info = await hub.inject(pkt, log, direction)
                        resp = {"ok": ok, "info": info}
                elif op == "status":
                    resp = {"ok": True, "active": hub._upstream is not None}
                else:
                    resp = {"ok": False, "err": f"unknown op: {op}"}
            writer.write((json.dumps(resp) + "\n").encode("utf-8"))
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def main() -> None:
    cfg = Config.load(ROOT / "config.yaml")
    log = PacketLog(cfg.packets_path, cfg.console)
    hub = InjectHub()
    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, cfg, log, hub),
        cfg.listen_host,
        cfg.listen_port,
    )
    control = await asyncio.start_server(
        lambda r, w: handle_control(r, w, hub, log),
        "127.0.0.1",
        cfg.control_port,
    )
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    caddrs = ", ".join(str(s.getsockname()) for s in control.sockets)
    print(f"[*] proxy listening on {addrs}", file=sys.stderr)
    print(f"[*] control listening on {caddrs}", file=sys.stderr)
    print(f"[*] forwarding to {cfg.upstream_host}:{cfg.upstream_port}", file=sys.stderr)
    print(f"[*] logging to {cfg.packets_path}", file=sys.stderr)
    async with server, control:
        await asyncio.gather(server.serve_forever(), control.serve_forever())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[*] shutting down", file=sys.stderr)
