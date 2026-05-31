"""Quick packet-log inspector. Print full bodies of selected Cmd types."""
import json, sys
from pathlib import Path

LOG = Path(__file__).resolve().parent.parent / "logs" / "packets.jsonl"
keep = set(sys.argv[1:]) or {
    "Login","loginResponse","questData","updateQuestBits",
    "getQuests","resetsaga","firstJoin","getDialog","initPlayer",
}
for line in LOG.open(encoding="utf-8"):
    e = json.loads(line)
    if not e["ok"]: continue
    cmd = e["pkt"].get("Cmd")
    if cmd in keep:
        body = json.dumps(e["pkt"], separators=(",", ":"))
        print(f"[{e['dir']}] {cmd}: {body[:1200]}")
        print()
