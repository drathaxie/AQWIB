"""
AQWI-Bot GUI.

Tails the proxy's packet log, maintains live PlayerState, and provides controls
for the bot driver (talks to the proxy's control port to inject packets).
"""

from __future__ import annotations

import collections
import json
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from bot import Injector, QuestLoop
from state import PacketTail, PlayerState

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / "logs" / "packets.jsonl"
PACKET_HISTORY = 800
REFRESH_MS = 200
CONTROL_PORT = 7780

# Server broadcasts these for *other* players (or for our own movement echo)
# at high frequency. Hidden by default to keep the packet view useful.
NOISE_CMDS = {
    "mv", "MoveOK", "Attack", "RespawnMon", "statusEffect",
    "mtls", "ChangeState", "CellMove", "CellJoin", "rNotify",
}


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.state = PlayerState()
        self.packets: collections.deque[tuple[str, str, str]] = collections.deque(
            maxlen=PACKET_HISTORY
        )
        self.bot_events: collections.deque[str] = collections.deque(maxlen=200)
        self._filter = ""
        self._hide_noise = tk.BooleanVar(value=True)

        self.injector = Injector(port=CONTROL_PORT)
        self.loop: QuestLoop | None = None

        root.title("AQWI-Bot")
        root.geometry("1000x680")
        root.minsize(800, 500)

        self._build_status_bar()
        self._build_tabs()

        self.tail = PacketTail(
            LOG_PATH,
            self.state,
            on_packet=self._on_packet,
            from_start=True,
        )
        self.tail.start()
        self.root.after(REFRESH_MS, self._refresh)

    # ---- layout -----------------------------------------------------------

    def _build_status_bar(self) -> None:
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(side=tk.TOP, fill=tk.X)
        self.status_dot = tk.Label(bar, text="●", font=("Segoe UI", 14), fg="#a33")
        self.status_dot.pack(side=tk.LEFT)
        self.status_text = ttk.Label(bar, text="waiting for proxy log…")
        self.status_text.pack(side=tk.LEFT, padx=(6, 12))
        self.inject_dot = tk.Label(bar, text="●", font=("Segoe UI", 14), fg="#a33")
        self.inject_dot.pack(side=tk.LEFT)
        self.inject_text = ttk.Label(bar, text="control: unknown")
        self.inject_text.pack(side=tk.LEFT, padx=(6, 0))
        self.pkt_counter = ttk.Label(bar, text="0 packets")
        self.pkt_counter.pack(side=tk.RIGHT)

    def _build_tabs(self) -> None:
        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self._build_bot_tab(nb)
        self._build_player_tab(nb)
        self._build_quests_tab(nb)
        self._build_packets_tab(nb)

    def _build_bot_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, padding=10)
        nb.add(tab, text="Bot")

        # Quest config row
        cfg = ttk.LabelFrame(tab, text="Repeatable quest loop", padding=10)
        cfg.pack(fill=tk.X)
        ttk.Label(cfg, text="Quest ID:").grid(row=0, column=0, sticky=tk.W)
        self.qid_var = tk.StringVar(value="124")
        ttk.Entry(cfg, textvariable=self.qid_var, width=8).grid(row=0, column=1, padx=(4, 16))
        ttk.Label(cfg, text="Iterations:").grid(row=0, column=2, sticky=tk.W)
        self.iter_var = tk.StringVar(value="50")
        ttk.Entry(cfg, textvariable=self.iter_var, width=8).grid(row=0, column=3, padx=(4, 16))
        self.start_btn = ttk.Button(cfg, text="Start", command=self._start_loop)
        self.start_btn.grid(row=0, column=4, padx=(8, 4))
        self.stop_btn = ttk.Button(cfg, text="Stop", command=self._stop_loop, state=tk.DISABLED)
        self.stop_btn.grid(row=0, column=5)

        # Manual injector
        manual = ttk.LabelFrame(tab, text="Manual inject (send one packet)", padding=10)
        manual.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(manual, text="Cmd:").grid(row=0, column=0, sticky=tk.W)
        self.cmd_var = tk.StringVar(value="acceptQuest")
        ttk.Entry(manual, textvariable=self.cmd_var, width=18).grid(row=0, column=1, padx=(4, 16))
        ttk.Label(manual, text="Params (comma-sep):").grid(row=0, column=2, sticky=tk.W)
        self.params_var = tk.StringVar(value="124")
        ttk.Entry(manual, textvariable=self.params_var, width=40).grid(row=0, column=3, padx=(4, 16))
        ttk.Button(manual, text="Send", command=self._manual_send).grid(row=0, column=4)

        # Chat injector — c2s cmd is `message` with Params=[text, channel].
        # Server broadcasts back as `chatm` {msg, Name, channel, ID}.
        chat = ttk.LabelFrame(tab, text="Chat (sends `message` cmd)", padding=10)
        chat.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(chat, text="Channel:").grid(row=0, column=0, sticky=tk.W)
        self.chat_channel_var = tk.StringVar(value="zone")
        ttk.Combobox(
            chat,
            textvariable=self.chat_channel_var,
            values=["zone", "party", "guild", "server"],
            width=8,
            state="normal",
        ).grid(row=0, column=1, padx=(4, 16))
        ttk.Label(chat, text="Message:").grid(row=0, column=2, sticky=tk.W)
        self.chat_msg_var = tk.StringVar()
        msg_entry = ttk.Entry(chat, textvariable=self.chat_msg_var, width=56)
        msg_entry.grid(row=0, column=3, padx=(4, 16))
        msg_entry.bind("<Return>", lambda _e: self._chat_send())
        ttk.Button(chat, text="Send", command=self._chat_send).grid(row=0, column=4)

        # Spoof — writes packets toward the client so they look server-sent.
        # Bypasses server-side permission checks ("you do not have permission
        # to send server messages") because the server never sees them.
        spoof = ttk.LabelFrame(tab, text="Spoof (server→client, local only)", padding=10)
        spoof.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(spoof, text="Kind:").grid(row=0, column=0, sticky=tk.W)
        self.spoof_kind_var = tk.StringVar(value="rNotify")
        ttk.Combobox(
            spoof,
            textvariable=self.spoof_kind_var,
            values=["rNotify", "chatm (server)", "chatm (zone)"],
            width=16,
            state="readonly",
        ).grid(row=0, column=1, padx=(4, 16))
        ttk.Label(spoof, text="Text:").grid(row=0, column=2, sticky=tk.W)
        self.spoof_text_var = tk.StringVar(value="Hello from the void")
        spoof_entry = ttk.Entry(spoof, textvariable=self.spoof_text_var, width=52)
        spoof_entry.grid(row=0, column=3, padx=(4, 16))
        spoof_entry.bind("<Return>", lambda _e: self._spoof_send())
        ttk.Button(spoof, text="Spoof", command=self._spoof_send).grid(row=0, column=4)

        # Live stats
        stats = ttk.LabelFrame(tab, text="Run", padding=10)
        stats.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.stats_label = ttk.Label(
            stats, text="idle.", font=("Consolas", 10)
        )
        self.stats_label.pack(anchor=tk.W)
        ttk.Separator(stats).pack(fill=tk.X, pady=6)
        ttk.Label(stats, text="Events:").pack(anchor=tk.W)
        self.bot_log = tk.Listbox(stats, font=("Consolas", 9), height=14, activestyle="none")
        self.bot_log.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

    def _build_player_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="Player")
        cols = ("field", "value")
        self.player_tree = ttk.Treeview(tab, columns=cols, show="headings", height=12)
        self.player_tree.heading("field", text="Field")
        self.player_tree.heading("value", text="Value")
        self.player_tree.column("field", width=180, anchor=tk.W)
        self.player_tree.column("value", width=700, anchor=tk.W)
        self.player_tree.pack(fill=tk.BOTH, expand=True)

    def _build_quests_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="Quests")
        qcols = ("id", "name", "storyline", "status")
        self.quests_tree = ttk.Treeview(tab, columns=qcols, show="headings", height=18)
        for c, w in zip(qcols, (60, 320, 220, 100)):
            self.quests_tree.heading(c, text=c.title())
            self.quests_tree.column(c, width=w, anchor=tk.W)
        self.quests_tree.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            tab,
            text="Double-click a row to load that Quest ID into the Bot tab.",
            foreground="#777",
        ).pack(anchor=tk.W, pady=(6, 0))
        self.quests_tree.bind("<Double-1>", self._quest_double_click)

    def _build_packets_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="Packets")
        top = ttk.Frame(tab)
        top.pack(fill=tk.X)
        ttk.Label(top, text="Filter:").pack(side=tk.LEFT)
        self.filter_var = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.filter_var, width=24)
        ent.pack(side=tk.LEFT, padx=(4, 12))
        ent.bind("<KeyRelease>", self._on_filter_change)
        ttk.Checkbutton(
            top,
            text=f"Hide broadcast noise ({', '.join(sorted(NOISE_CMDS))})",
            variable=self._hide_noise,
        ).pack(side=tk.LEFT)
        self.pkt_list = tk.Listbox(tab, font=("Consolas", 9), activestyle="none")
        self.pkt_list.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

    # ---- bot controls -----------------------------------------------------

    def _start_loop(self) -> None:
        try:
            qid = int(self.qid_var.get())
            iters = int(self.iter_var.get())
        except ValueError:
            self._log_bot("[error] qid and iterations must be integers")
            return
        if self.loop and self.loop.is_running():
            return
        self.loop = QuestLoop(
            self.injector, self.state, qid=qid, iterations=iters,
            on_event=self._bot_event,
        )
        self.loop.start()
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

    def _stop_loop(self) -> None:
        if self.loop:
            self.loop.stop()
        self.stop_btn.config(state=tk.DISABLED)

    def _bot_event(self, kind: str, payload: dict) -> None:
        # Called from QuestLoop's worker thread. Post to UI thread via after().
        self.root.after(0, lambda: self._handle_bot_event(kind, payload))

    def _handle_bot_event(self, kind: str, payload: dict) -> None:
        if kind == "start":
            self._log_bot(f"[start] quest {payload['qid']} × {payload['iterations']}")
        elif kind == "iter_ok":
            self._log_bot(f"  ✓ iter {payload['n']:>3}  {payload['secs']:.2f}s")
        elif kind == "iter_fail":
            self._log_bot(f"  ✗ iter {payload['n']:>3}  {payload['err']}")
        elif kind == "done":
            s = payload["stats"]
            self._log_bot(
                f"[done] requested={s.requested} ok={s.completed_ok} "
                f"failed={s.failed} last_err={s.last_error or '—'}"
            )
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)

    def _log_bot(self, line: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.bot_events.append(f"{ts}  {line}")

    def _manual_send(self) -> None:
        cmd = self.cmd_var.get().strip()
        if not cmd:
            return
        params_raw = self.params_var.get().strip()
        params = [p.strip() for p in params_raw.split(",")] if params_raw else []
        r = self.injector.send(cmd, params)
        self._log_bot(f"[manual] {cmd} {params}  →  ok={r.ok}  {r.info}")

    def _chat_send(self) -> None:
        msg = self.chat_msg_var.get()
        channel = self.chat_channel_var.get().strip() or "zone"
        if not msg:
            return
        r = self.injector.send("message", [msg, channel])
        self._log_bot(f"[chat:{channel}] {msg!r}  →  ok={r.ok}  {r.info}")
        self.chat_msg_var.set("")

    def _spoof_send(self) -> None:
        kind = self.spoof_kind_var.get()
        text = self.spoof_text_var.get()
        if not text:
            return
        if kind == "rNotify":
            pkt = {"Cmd": "rNotify", "msg": text}
        elif kind == "chatm (server)":
            pkt = {"Cmd": "chatm", "msg": text, "Name": "SERVER", "channel": "server"}
        elif kind == "chatm (zone)":
            name = self.state.name or "SERVER"
            pkt = {"Cmd": "chatm", "msg": text, "Name": name, "channel": "zone"}
        else:
            self._log_bot(f"[spoof] unknown kind: {kind}")
            return
        r = self.injector.spoof(pkt)
        self._log_bot(f"[spoof:{kind}] {text!r}  →  ok={r.ok}  {r.info}")
        self.spoof_text_var.set("")

    def _quest_double_click(self, _evt) -> None:
        sel = self.quests_tree.selection()
        if not sel:
            return
        vals = self.quests_tree.item(sel[0], "values")
        if vals:
            self.qid_var.set(vals[0])

    # ---- packet flow ------------------------------------------------------

    def _on_packet(self, direction: str, pkt: dict) -> None:
        arrow = "→" if direction == "c2s" else "←"
        cmd = pkt.get("Cmd", "?")
        rest = {k: v for k, v in pkt.items() if k != "Cmd"}
        try:
            preview = json.dumps(rest, separators=(",", ":"))
        except Exception:
            preview = str(rest)
        self.packets.append((arrow, cmd, preview[:260]))

    def _on_filter_change(self, _evt=None) -> None:
        self._filter = self.filter_var.get().strip().lower()

    # ---- refresh loop -----------------------------------------------------

    def _refresh(self) -> None:
        self._refresh_status()
        self._refresh_player()
        self._refresh_quests()
        self._refresh_packets()
        self._refresh_bot()
        self.root.after(REFRESH_MS, self._refresh)

    def _refresh_status(self) -> None:
        now = time.time()
        age = now - self.state.last_packet_ts if self.state.last_packet_ts else 1e9
        if self.state.packets_seen == 0:
            self.status_dot.config(fg="#a33")
            self.status_text.config(text="waiting for proxy log…")
        elif age < 5:
            self.status_dot.config(fg="#3a3")
            self.status_text.config(text=f"connected · last cmd: {self.state.last_cmd}")
        else:
            self.status_dot.config(fg="#aa3")
            self.status_text.config(text=f"idle · last packet {age:.0f}s ago")
        self.pkt_counter.config(text=f"{self.state.packets_seen} packets")

        # Cheap inject health check: status() result is cached briefly to avoid
        # hammering the control socket every refresh.
        if not hasattr(self, "_last_status_check") or now - self._last_status_check > 1.5:
            self._last_status_check = now
            try:
                active = self.injector.status()
            except Exception:
                active = False
            if active:
                self.inject_dot.config(fg="#3a3")
                self.inject_text.config(text="control: game session active")
            else:
                self.inject_dot.config(fg="#aa3")
                self.inject_text.config(text="control: no active session")

    def _refresh_player(self) -> None:
        rows = [
            ("Name", self.state.name or "—"),
            ("UserID", str(self.state.user_id) if self.state.user_id else "—"),
            ("Server time", self.state.server_time or "—"),
            ("Zone", self.state.zone or "—"),
            ("Cell", self.state.cell or "—"),
            ("Quests accepted", ", ".join(map(str, self.state.quests_accepted)) or "—"),
            ("Quests complete", str(self.state.quests_complete_count())),
            ("Quest defs cached", str(len(self.state.quest_defs))),
        ]
        existing = self.player_tree.get_children()
        if len(existing) != len(rows):
            for iid in existing:
                self.player_tree.delete(iid)
            for f, v in rows:
                self.player_tree.insert("", tk.END, values=(f, v))
        else:
            for iid, (f, v) in zip(existing, rows):
                self.player_tree.item(iid, values=(f, v))

    def _refresh_quests(self) -> None:
        defs = self.state.quest_defs
        existing = {
            self.quests_tree.item(i, "values")[0]: i
            for i in self.quests_tree.get_children()
        }
        seen = set()
        for qid in sorted(defs):
            qdef = defs[qid]
            name = qdef.get("Name", "?")
            storyline = (qdef.get("storylineData") or {}).get("Name", "")
            if self.state.is_quest_complete(qid):
                status = "complete"
            elif qid in self.state.quests_accepted:
                status = "accepted"
            else:
                status = "available"
            row = (str(qid), name, storyline, status)
            key = str(qid)
            seen.add(key)
            if key in existing:
                self.quests_tree.item(existing[key], values=row)
            else:
                self.quests_tree.insert("", tk.END, values=row)
        for key, iid in existing.items():
            if key not in seen:
                self.quests_tree.delete(iid)

    def _refresh_packets(self) -> None:
        snap = list(self.packets)
        f = self._filter
        hide = self._hide_noise.get()
        self.pkt_list.delete(0, tk.END)
        for arrow, cmd, preview in snap:
            if hide and cmd in NOISE_CMDS:
                continue
            if f and f not in cmd.lower():
                continue
            self.pkt_list.insert(tk.END, f"{arrow} {cmd:<22} {preview}")
        self.pkt_list.yview_moveto(1.0)

    def _refresh_bot(self) -> None:
        # stats label
        if self.loop and self.loop.is_running():
            s = self.loop.stats
            last = s.iter_durations[-1] if s.iter_durations else 0.0
            self.stats_label.config(
                text=f"running · quest {s.target_qid} · "
                f"iter {s.requested}/{self.loop.iterations} · "
                f"ok={s.completed_ok} fail={s.failed} · "
                f"last iter {last:.2f}s"
            )
        elif self.loop:
            s = self.loop.stats
            self.stats_label.config(
                text=f"stopped · quest {s.target_qid} · "
                f"ok={s.completed_ok}/{s.requested} fail={s.failed}"
            )
        # event list
        snap = list(self.bot_events)
        self.bot_log.delete(0, tk.END)
        for line in snap:
            self.bot_log.insert(tk.END, line)
        self.bot_log.yview_moveto(1.0)


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
