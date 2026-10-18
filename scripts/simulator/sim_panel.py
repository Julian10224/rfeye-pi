"""Bedienpaneel van de simulator: C2000-voertuigen en het basisstation.

Draait in een eigen proces (tkinter en pygame willen allebei de hoofdthread)
en praat via twee queues met de simulator: opdrachten erheen, status terug.
"""
from __future__ import annotations

import queue
import tkinter as tk
from tkinter import ttk

MODES = {"bursts": "Status-bursts", "call": "Spraak (PTT)", "silent": "Stil"}
MODE_KEYS = {v: k for k, v in MODES.items()}
CELLS = ["eigen cel (besturingskanaal van de site)", "buurcel",
         "onbekende cel (willekeurig kanaal)", "kanaalnummer:"]
CELL_KEYS = dict(zip(CELLS, ("local", "neighbour", "random", "channel")))
CELL_NAMES = {"local": "eigen cel", "neighbour": "buurcel", "random": "onbekend"}


def run_panel(cmd_q, status_q):
    root = tk.Tk()
    root.title("RF Eye simulator - C2000 voertuigen")
    root.geometry("+40+40")
    pad = {"padx": 6, "pady": 3}

    def send(*cmd):
        cmd_q.put(cmd)

    # -- what RF Eye is doing --------------------------------------------
    top = ttk.LabelFrame(root, text="RF Eye")
    top.grid(row=0, column=0, columnspan=2, sticky="ew", **pad)
    state_var = tk.StringVar(value="opstarten...")
    detail_var = tk.StringVar(value="")
    state_lbl = tk.Label(top, textvariable=state_var, font=("Segoe UI", 16, "bold"),
                         width=28, anchor="w")
    state_lbl.grid(row=0, column=0, sticky="w", **pad)
    ttk.Label(top, textvariable=detail_var, justify="left").grid(row=1, column=0, sticky="w", **pad)

    # -- the base station -------------------------------------------------
    site = ttk.LabelFrame(root, text="C2000-basisstations (downlink 390-395 MHz)")
    site.grid(row=1, column=0, sticky="nsew", **pad)
    site_vars = {}
    for row, (key, label, on, db) in enumerate((("local", "Eigen site", True, 22.0),
                                                ("neighbour", "Buursite", False, 12.0))):
        v_on, v_db, v_lbl = tk.BooleanVar(value=on), tk.DoubleVar(value=db), tk.StringVar()
        site_vars[key] = (v_on, v_db, v_lbl)

        def changed(_=None, k=key):
            o, d, _l = site_vars[k]
            send("site", k, bool(o.get()), float(d.get()))

        ttk.Checkbutton(site, text=label, variable=v_on, command=changed).grid(
            row=2 * row, column=0, sticky="w", **pad)
        tk.Scale(site, variable=v_db, from_=0, to=40, orient="horizontal", length=150,
                 command=changed).grid(row=2 * row, column=1, **pad)
        ttk.Label(site, textvariable=v_lbl, foreground="#555").grid(
            row=2 * row + 1, column=0, columnspan=2, sticky="w", padx=6)

    # -- a new vehicle ----------------------------------------------------
    new = ttk.LabelFrame(root, text="Nieuw voertuig")
    new.grid(row=1, column=1, sticky="nsew", **pad)
    name_var = tk.StringVar(value="")
    ch_choice = tk.StringVar(value=CELLS[0])
    ch_var = tk.StringVar(value="")
    mode_var = tk.StringVar(value=MODES["bursts"])
    every_var = tk.DoubleVar(value=4.0)
    db_var = tk.DoubleVar(value=18.0)
    ttk.Label(new, text="Naam").grid(row=0, column=0, sticky="w", **pad)
    ttk.Entry(new, textvariable=name_var, width=18).grid(row=0, column=1, sticky="w", **pad)
    ttk.Label(new, text="Cel / kanaal").grid(row=1, column=0, sticky="w", **pad)
    ttk.Combobox(new, textvariable=ch_choice, state="readonly", width=34,
                 values=CELLS).grid(row=1, column=1, sticky="w", **pad)
    ttk.Entry(new, textvariable=ch_var, width=6).grid(row=1, column=2, sticky="w", **pad)
    ttk.Label(new, text="Gedrag").grid(row=2, column=0, sticky="w", **pad)
    ttk.Combobox(new, textvariable=mode_var, state="readonly", width=20,
                 values=list(MODES.values())).grid(row=2, column=1, sticky="w", **pad)
    ttk.Label(new, text="Burst elke (s)").grid(row=3, column=0, sticky="w", **pad)
    ttk.Spinbox(new, textvariable=every_var, from_=0.5, to=60, increment=0.5,
                width=6).grid(row=3, column=1, sticky="w", **pad)
    ttk.Label(new, text="Sterkte / nabijheid (dB)").grid(row=4, column=0, sticky="w", **pad)
    tk.Scale(new, variable=db_var, from_=-5, to=45, orient="horizontal",
             length=180).grid(row=4, column=1, columnspan=2, sticky="w", **pad)

    def add():
        cell, ch = CELL_KEYS.get(ch_choice.get(), "local"), None
        if cell == "channel":
            try:
                ch = int(ch_var.get())
            except ValueError:
                ch = None
            cell = "random"
        send("add", {"name": name_var.get().strip() or None, "cell": cell, "ch": ch,
                     "mode": MODE_KEYS.get(mode_var.get(), "bursts"),
                     "every": float(every_var.get()), "db": float(db_var.get())})

    ttk.Button(new, text="Toevoegen", command=add).grid(row=5, column=1, sticky="w", **pad)

    # -- scenarios ----------------------------------------------------------
    sc = ttk.LabelFrame(root, text="Scenario's")
    sc.grid(row=2, column=0, columnspan=2, sticky="ew", **pad)
    for i, (label, key) in enumerate((
            ("Politie rijdt voorbij (eigen cel)", "police_driveby"),
            ("Politie rijdt voorbij (buurcel)", "police_neighbour"),
            ("Ambulance belt (eigen cel)", "ambulance_call"),
            ("Brandweer, onbekende cel", "fire_bursts"),
            ("Alles weg", "clear"))):
        ttk.Button(sc, text=label, command=lambda k=key: send("scenario", k)).grid(
            row=0, column=i, **pad)

    # -- the vehicles -------------------------------------------------------
    lst = ttk.LabelFrame(root, text="Voertuigen in de lucht")
    lst.grid(row=3, column=0, columnspan=2, sticky="nsew", **pad)
    cols = ("naam", "cel", "kanaal", "gesprek", "dB", "gedrag", "verzonden",
            "gezien bij", "zendt")
    tree = ttk.Treeview(lst, columns=cols, show="headings", height=8, selectmode="browse")
    for c, w in zip(cols, (110, 85, 60, 60, 50, 120, 70, 130, 55)):
        tree.heading(c, text=c)
        tree.column(c, width=w, anchor="center")
    tree.grid(row=0, column=0, columnspan=7, sticky="nsew", **pad)
    sel_db = tk.DoubleVar(value=18.0)

    def selected():
        s = tree.selection()
        return int(s[0]) if s else None

    def on_sel(_e=None):
        vid = selected()
        if vid is not None and vid in last_rows:
            sel_db.set(last_rows[vid]["db"])

    tree.bind("<<TreeviewSelect>>", on_sel)
    btn = [
        ("Burst nu", lambda v: send("burst", v)),
        ("PTT 3 s", lambda v: send("ptt", v, 3.0)),
        ("Spraak aan/uit", lambda v: send("toggle_call", v)),
        ("Rijdt voorbij (30 s)", lambda v: send("drive", v, 30.0)),
        ("Stil", lambda v: send("mode", v, "silent")),
        ("Verwijderen", lambda v: send("remove", v)),
    ]
    for i, (label, fn) in enumerate(btn):
        ttk.Button(lst, text=label, command=lambda f=fn: (selected() is not None and f(selected()))
                   ).grid(row=1, column=i, **pad)
    ttk.Label(lst, text="Sterkte geselecteerd (dB)").grid(row=2, column=0, columnspan=2, sticky="w", **pad)
    tk.Scale(lst, variable=sel_db, from_=-5, to=45, orient="horizontal", length=260,
             command=lambda _v: selected() is not None and send("snr", selected(), float(sel_db.get()))
             ).grid(row=2, column=2, columnspan=4, sticky="w", **pad)

    last_rows = {}

    def poll():
        st = None
        try:
            while True:
                st = status_q.get_nowait()
        except queue.Empty:
            pass
        if st is not None:
            if st.get("closed"):
                root.destroy()
                return
            rf = st["rfeye"]
            state = rf["state"]
            colour = {"ALERT": "#c62828", "LOCKED": "#2e7d32", "SEARCHING": "#555"}.get(state, "#555")
            if state == "ALERT" and rf["provisional"]:
                state, colour = "ALERT (voorlopig)", "#f9a825"
            state_lbl.config(fg=colour)
            state_var.set(state)
            peaks = ", ".join("CH %d %.0f%%" % (p[0], 100 * p[1]) for p in rf["peaks"]) or "-"
            detail_var.set(
                "modus: %s   venster: %s   gelockte carriers: %d\n"
                "triggers: %d   gezien: %s" % (
                    rf["mode"], rf["window"], rf["locked"], rf["triggers"], peaks))
            air = st["air"]
            for key, s_ in air["sites"].items():
                site_vars[key][2].set("hoofdcarrier CH %d (besturingskanaal), verkeer %s" % (
                    s_["main"], ", ".join("CH %d" % c for c in s_["traffic"])))
            seen = set()
            keep = tree.selection()
            for v in air["vehicles"]:
                vid = v["id"]
                seen.add(vid)
                seen_at = v.get("seen_at")
                row = (v["name"], CELL_NAMES.get(v["cell"], v["cell"]), v["ch"],
                       v["call_ch"] or "", "%.0f" % v["db"],
                       MODES.get(v["mode"], v["mode"]) + (" (rijdt)" if v["driving"] else ""),
                       v["sent"],
                       ("#%d (hoorbaar #%d)" % tuple(seen_at[:2])) if seen_at else "-",
                       "ZENDT" if v["tx"] else "")
                last_rows[vid] = v
                if tree.exists(str(vid)):
                    tree.item(str(vid), values=row)
                else:
                    tree.insert("", "end", iid=str(vid), values=row)
            for iid in tree.get_children():
                if int(iid) not in seen:
                    tree.delete(iid)
                    last_rows.pop(int(iid), None)
            if keep and tree.exists(keep[0]):
                tree.selection_set(keep)
        root.after(250, poll)

    def closing():
        send("quit")
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", closing)
    root.after(300, poll)
    root.mainloop()
