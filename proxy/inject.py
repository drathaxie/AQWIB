"""
Dry-run-first packet injector for the AQWI MITM proxy.

The proxy exposes a localhost control socket that writes a packet into the
currently active game session and logs it as a synthetic c2s packet. This script
uses that path so probes stay visible in logs/packets.jsonl.

Default behavior is preview-only. It will not send unless both --send and the
explicit approval flag are present.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"
DEFAULT_CONTROL_PORT = 7780


def load_control_port() -> int:
    with CONFIG.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return int((cfg.get("control") or {}).get("port", DEFAULT_CONTROL_PORT))


def build_probe_packet(type_name: str) -> dict[str, Any]:
    return {
        "$type": type_name,
        "Cmd": "getQuests",
        "Params": ["1"],
    }


def send_via_control(pkt: dict[str, Any], host: str, port: int, timeout: float) -> dict[str, Any]:
    req = {"op": "inject", "pkt": pkt}
    line = json.dumps(req, separators=(",", ":")).encode("utf-8") + b"\n"
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(line)
        fh = sock.makefile("rb")
        resp = fh.readline()
    if not resp:
        raise RuntimeError("proxy control socket closed without a response")
    return json.loads(resp.decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preview or send a single AQWI JSON packet through the MITM proxy control socket."
    )
    parser.add_argument(
        "--type",
        default="AQWI_Pentest_Probe_20260530.NonExistentType, AQWI_Pentest_Probe_20260530",
        help="Assembly-qualified $type value to place at the top level of the Request JSON.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Proxy control host.")
    parser.add_argument("--port", type=int, default=None, help="Proxy control port; defaults to config.yaml/control.port or 7780.")
    parser.add_argument("--timeout", type=float, default=3.0, help="Control socket timeout in seconds.")
    parser.add_argument("--send", action="store_true", help="Actually send the packet through the proxy control socket.")
    parser.add_argument(
        "--yes-i-have-approval",
        action="store_true",
        help="Required together with --send after the exact payload has been approved.",
    )
    args = parser.parse_args()

    pkt = build_probe_packet(args.type)
    wire_json = json.dumps(pkt, separators=(",", ":"))

    print("Probe packet preview:")
    print(wire_json)
    print()
    print("Null-terminated wire bytes:")
    print(repr(wire_json.encode("utf-8") + b"\x00"))

    if not args.send:
        print()
        print("Dry run only. Re-run with --send --yes-i-have-approval after approval.")
        return 0

    if not args.yes_i_have_approval:
        print("Refusing to send without --yes-i-have-approval.", file=sys.stderr)
        return 2

    port = args.port if args.port is not None else load_control_port()
    resp = send_via_control(pkt, args.host, port, args.timeout)
    print()
    print("Proxy control response:")
    print(json.dumps(resp, indent=2, sort_keys=True))
    return 0 if resp.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
