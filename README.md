# AQWI-Bot

Pentest/QA tooling for **AdventureQuest Worlds Infinity** Steam playtest. The intended deliverable
is a working proof-of-concept that the devs use to scope a patch.

## Architecture

```
   AQWI.exe  ──TCP──▶  proxy.py (127.0.0.1)  ──TCP──▶  AEC live server
             ◀──────                        ◀──────
                              │
                              ▼
                       logs/packets.jsonl
                       (later: state snapshot + IPC for driver)
```

- **Proxy**: passive MITM over the AEC TCP socket. Frames on null bytes, parses each
  packet as JSON (`{Cmd, Params}`), writes a JSONL log with direction + timestamp.
  Forwards bytes unchanged in both directions.
- **Driver** (later): simulates input to run a repeatable quest in a loop, reads the
  proxy's structured log as ground truth (instead of OCR) to verify reward deltas,
  halts on any mismatch and dumps state.

## Why MITM instead of BepInEx

- Zero modification to the game install — purely observational.
- AEC wire format is plaintext UTF-8 JSON + null terminator (confirmed: the
  `EncryptDecrypt` XOR routine in `AEC.cs` is defined but never called).
- Cleaner ToS framing for the pentest engagement.

## Setup

AEC server discovered live (2026-05-30): **`sockett4.aq.com` → `172.65.210.123:6150`**
(Cloudflare Spectrum TCP LB).

1. `pip install -r proxy/requirements.txt`
2. Add this line to `C:\Windows\System32\drivers\etc\hosts` (admin required):
   ```
   127.0.0.1   sockett4.aq.com
   ```
3. **Quit the game fully** (otherwise the existing socket stays connected to the real
   IP, ignoring the new hosts entry).
4. `python proxy/proxy.py`  → listens on `127.0.0.1:6150`, forwards to
   `172.65.210.123:6150`, writes `logs/packets.jsonl`.
5. Launch the game and log in. Every packet flows through the proxy.

### Tear down
- Remove the hosts line. Done.
- (Proxy makes no other system changes — no game files touched, no DLLs injected.)

## Running the GUI

In a second terminal, cd into the directory where GUI is hosted (proxy stays running in the first):
```
python gui.py
```
Tails `logs/packets.jsonl`, shows live player state, cached quest defs, and a
filterable packet stream. Updates ~5×/s.

## Status

- [x] Recon, design, proxy, state replay, GUI phase 1
- [x] Inject endpoint (proxy control port `127.0.0.1:7780`, newline-JSON)
- [x] Driver (`bot.py`) — `QuestLoop` state machine, halts on first mismatch
- [x] GUI Bot tab — quest picker, manual injector, Start/Stop, live event log
- [x] Packet view filters broadcast noise (mv, Attack, RespawnMon, …) by default
- [ ] Per-quest verification rules (reward delta, inventory delta) — next
- [ ] Kill/collect quest support — needs more packet captures to model
