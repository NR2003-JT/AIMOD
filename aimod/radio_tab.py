"""The Radio tab: team radio on/off, which car, voices, volume, what gets said, and the lines said so far."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import carno
from .radio import (CATEGORIES, CREW, DRIVER, DRIVER_CHOICES, SPOTTER, TRIGGERS, VOICE_CHOICES, WINDOWS_VOICES, Radio, parse_voice, team_label,
                    voice_label)
from .scrollframe import scrolled

YOUR_CAR = "Your car"


class RadioTab:
    def __init__(self, nb: ttk.Notebook, radio: Radio, save, colors: dict, tooltip=None):
        self.radio, self.save = radio, save
        cfg = radio.cfg
        tip = tooltip or (lambda w, t: None)
        outer = ttk.Frame(nb)
        nb.add(outer, text="Scanner")
        tab = scrolled(outer, colors.get("bg", "#1a1a1a"))   # scrolls when the window is shorter than the tab

        top = ttk.Frame(tab)
        top.pack(fill="x", pady=(4, 6))
        self.var_on = tk.BooleanVar(value=cfg["on"])
        cb = ttk.Checkbutton(top, text="Team radio on", variable=self.var_on, command=self._changed)
        cb.pack(side="left")
        tip(cb, "Crew chief and spotter calls for one car, spoken with Windows' voices (test build).")
        ttk.Label(top, text="   Car:").pack(side="left")
        car = str(cfg["car"])
        self.var_car = tk.StringVar(value=YOUR_CAR if car in ("", "player") else car)
        ent = ttk.Combobox(top, textvariable=self.var_car, values=[YOUR_CAR], width=10)
        ent.pack(side="left", padx=(4, 0))
        ent.bind("<<ComboboxSelected>>", lambda e: self._changed())
        ent.bind("<FocusOut>", lambda e: self._changed())
        ent.bind("<Return>", lambda e: self._changed())
        tip(ent, "Your car (the one you drive), or type a car number to hear the radio for that car while you "
                 "spectate.")
        ttk.Label(top, text="   Volume").pack(side="left")
        self.var_vol = tk.IntVar(value=int(cfg["volume"]))
        ttk.Scale(top, from_=0, to=150, variable=self.var_vol, length=120,
                  command=lambda v: self._changed()).pack(side="left", padx=(4, 0))
        ttk.Label(top, text="   Talk speed").pack(side="left")
        self.var_rate = tk.IntVar(value=int(cfg["rate"]))
        sp = ttk.Spinbox(top, from_=-5, to=6, width=4, textvariable=self.var_rate, command=self._changed)
        sp.pack(side="left", padx=(4, 0))
        sp.bind("<FocusOut>", lambda e: self._changed())
        self.var_fx = tk.BooleanVar(value=cfg["radio_fx"])
        cb = ttk.Checkbutton(top, text="Radio sound", variable=self.var_fx, command=self._changed)
        cb.pack(side="left", padx=(12, 0))
        tip(cb, "Narrow-band radio voice with a squelch at the end. Off = the plain voice.")
        ttk.Button(top, text="Test", command=self.radio.test).pack(side="left", padx=(12, 0))
        self.var_phone = tk.BooleanVar(value=bool(cfg.get("phone_only", False)))
        cb = ttk.Checkbutton(top, text="Phone only", variable=self.var_phone, command=self._changed)
        cb.pack(side="left", padx=(12, 0))
        tip(cb, "While a phone on your network is playing the scanner, this PC stays quiet, so you don't hear every "
                "call twice. The phone can switch this too.")
        self.lbl_phone = ttk.Label(top, text="", style="Dim.TLabel")
        self.lbl_phone.pack(side="left", padx=(6, 0))
        tab.after(1000, self._phone_poll)

        voices = ttk.Frame(tab)
        voices.pack(fill="x", pady=(0, 6))
        self.found = radio.voice.piper_files() is not None
        choices = ([voice_label(n) for n, _d in VOICE_CHOICES] if self.found else []) + \
            [f"Windows {n}" for n in WINDOWS_VOICES]
        self.var_vc = tk.StringVar(value=self._voice_text(cfg["crew_voice"], 600))
        self.var_vs = tk.StringVar(value=self._voice_text(cfg["spotter_voice"], 615))
        self.var_vd = tk.StringVar(value=self._voice_text(cfg["driver_voice"], 21))
        driver_choices = ([voice_label(n) for n, _d in DRIVER_CHOICES] if self.found else []) + \
            [f"Windows {n}" for n in WINDOWS_VOICES]
        for label, var, role in (("Crew chief voice", self.var_vc, CREW), ("   Spotter", self.var_vs, SPOTTER),
                                 ("   Driver", self.var_vd, DRIVER)):
            ttk.Label(voices, text=label).pack(side="left")
            box = ttk.Combobox(voices, textvariable=var, values=driver_choices if role == DRIVER else choices,
                               width=22)
            box.pack(side="left", padx=(4, 0))
            box.bind("<<ComboboxSelected>>", lambda e: self._changed())
            box.bind("<FocusOut>", lambda e: self._changed())
            box.bind("<Return>", lambda e: self._changed())
            tip(box, "Piper voices are natural voices (the voice pack in AIMOD's voices folder); you can also type any "
                     "Piper speaker number, 0-903. Windows voices always work." +
                     ("" if self.found else "  No Piper voice pack found, so only Windows voices are listed."))
            b = ttk.Button(voices, text="\u25b6", width=3, command=lambda r=role: self._preview(r))
            b.pack(side="left", padx=(2, 0))
            tip(b, "Hear this voice")
        self.var_swear = tk.BooleanVar(value=bool(cfg.get("driver_swears", False)))
        cb = ttk.Checkbutton(voices, text="Driver swears", variable=self.var_swear, command=self._changed)
        cb.pack(side="left", padx=(10, 0))
        tip(cb, "The drivers curse on the radio (damn, hell, shit ...) when the car's a handful or they hit something.")
        b = ttk.Button(voices, text="Custom lines...", command=self._custom_lines)
        b.pack(side="left", padx=(6, 0))
        tip(b, "Add your own lines to any radio moment: the spotter's flags, the crew chief's calls, the AI "
               "drivers' shouts. They're mixed in with the built-in lines and said exactly as you write them "
               "(the Driver swears setting doesn't filter them).")
        self.var_helmet = tk.BooleanVar(value=bool(cfg.get("helmet", True)))
        cb = ttk.Checkbutton(voices, text="Helmet engine", variable=self.var_helmet, command=self._changed)
        cb.pack(side="left", padx=(10, 0))
        tip(cb, "The driver sounds like he's talking from inside his helmet: his engine (NR2003's own in-car sound, "
                "at his speed) muffled under his voice.")
        self.var_hlevel = tk.IntVar(value=int(cfg.get("helmet_engine", 55)))
        sc = ttk.Scale(voices, from_=0, to=100, variable=self.var_hlevel, length=70,
                       command=lambda v: self._changed())
        sc.pack(side="left", padx=(2, 0))
        tip(sc, "How loud the engine is under the driver's voice")
        ttk.Label(voices, text="   Position report every").pack(side="left")
        self.var_rep = tk.IntVar(value=int(cfg["report_laps"]))
        sp = ttk.Spinbox(voices, from_=0, to=100, width=4, textvariable=self.var_rep, command=self._changed)
        sp.pack(side="left", padx=(4, 0))
        sp.bind("<FocusOut>", lambda e: self._changed())
        ttk.Label(voices, text="green laps").pack(side="left", padx=(4, 0))

        scan = ttk.LabelFrame(tab, text="Listen to")
        scan.pack(fill="x", pady=(0, 6))
        self.var_mode = tk.StringVar(value=cfg["mode"])
        r1 = ttk.Radiobutton(scan, text="One car (above)", value="car", variable=self.var_mode, command=self._changed)
        r1.grid(row=0, column=0, sticky="w", padx=6, pady=2)
        r2 = ttk.Radiobutton(scan, text="Scanner:", value="scanner", variable=self.var_mode, command=self._changed)
        r2.grid(row=0, column=1, sticky="w", padx=(18, 2), pady=2)
        tip(r2, "Every scanned car has its own crew chief; the scanner airs one transmission at a time, like a race "
                "scanner. Urgent calls, the car on TV, your favourites and the front of the field come first. "
                "Flag calls and what the leader does come from one car only (the TV car, else yours, else the "
                "leader).")
        self.var_scan = tk.StringVar(value=self.SCAN_LABELS.get(cfg["scan"], "All cars"))
        box = ttk.Combobox(scan, textvariable=self.var_scan, values=list(self.SCAN_LABELS.values()), width=13,
                           state="readonly")
        box.grid(row=0, column=2, sticky="w")
        box.bind("<<ComboboxSelected>>", lambda e: self._changed())
        tip(box, "All cars; All team cars = every car in AIMOD's Teams window; One team = the team picked on the "
                 "right; These numbers = the car numbers typed on the right.")
        ttk.Label(scan, text="Team").grid(row=0, column=3, sticky="e", padx=(10, 2))
        self.var_team = tk.StringVar(value=cfg["scan_team"])
        tb = ttk.Combobox(scan, textvariable=self.var_team, width=24, state="readonly",
                          postcommand=lambda: tb.configure(values=self._team_choices()))
        tb.configure(values=self._team_choices())
        tb.grid(row=0, column=4, sticky="w")
        tb.bind("<<ComboboxSelected>>", lambda e: self._team_picked())
        tip(tb, "Teams from AIMOD's Teams window (for the loaded mod). Picking one scans just that team.")
        self.lbl_team_cars = ttk.Label(scan, text="", style="Dim.TLabel")
        self.lbl_team_cars.grid(row=1, column=4, sticky="w")
        self._show_team_cars()
        ttk.Label(scan, text="Numbers").grid(row=0, column=5, sticky="e", padx=(10, 2))
        self.var_nums = tk.StringVar(value=cfg["scan_numbers"])
        ent = ttk.Entry(scan, textvariable=self.var_nums, width=18)
        ent.grid(row=0, column=6, sticky="w")
        ent.bind("<FocusOut>", lambda e: self._changed())
        ent.bind("<Return>", lambda e: self._changed())
        tip(ent, "Car numbers to scan, e.g. 24, 48, 9")
        ttk.Label(scan, text="Favourites").grid(row=1, column=5, sticky="e", padx=(10, 2))
        self.var_fav = tk.StringVar(value=cfg["favourites"])
        ent = ttk.Entry(scan, textvariable=self.var_fav, width=18)
        ent.grid(row=1, column=6, sticky="w", pady=2)
        ent.bind("<FocusOut>", lambda e: self._changed())
        ent.bind("<Return>", lambda e: self._changed())
        tip(ent, "Cars you'd like to hear more of, e.g. 24, 48")
        self.var_tv = tk.BooleanVar(value=cfg["follow_tv"])
        cb = ttk.Checkbutton(scan, text="Follow the TV car", variable=self.var_tv, command=self._changed)
        cb.grid(row=1, column=2, sticky="w")
        tip(cb, "The car the game's camera is showing gets priority, and its spotter calls the flags.")

        ind = ttk.LabelFrame(tab, text="Who's talking (on-screen number card)")
        ind.pack(fill="x", pady=(0, 6))
        icfg = cfg["indicator"]
        self.var_ind = tk.BooleanVar(value=icfg["on"])
        cb = ttk.Checkbutton(ind, text="Show", variable=self.var_ind, command=self._changed)
        cb.pack(side="left", padx=6)
        tip(cb, "The talking car's number card on a faint plate, over the game (borderless / windowed NR2003). "
                "Fades in when the car keys up, out when it's done.")
        ttk.Label(ind, text="   Strength").pack(side="left")
        self.var_iop = tk.DoubleVar(value=float(icfg["opacity"]))
        ttk.Scale(ind, from_=0.1, to=1.0, variable=self.var_iop, length=110,
                  command=lambda v: self._changed(redraw=True)).pack(side="left", padx=(4, 0))
        ttk.Label(ind, text="   Size").pack(side="left")
        self.var_isz = tk.DoubleVar(value=float(icfg["size"]))
        ttk.Scale(ind, from_=0.5, to=2.5, variable=self.var_isz, length=110,
                  command=lambda v: self._changed(redraw=True)).pack(side="left", padx=(4, 0))
        self.var_imove = tk.BooleanVar(value=False)
        cb = ttk.Checkbutton(ind, text="Move", variable=self.var_imove, command=self._move_mode)
        cb.pack(side="left", padx=(12, 0))
        tip(cb, "Shows a sample card at full strength: drag it where you want it, then untick.")
        self.indicator = None                       # set by the app once the window exists

        pv = ttk.LabelFrame(tab, text="Natural voices (Piper)")
        pv.pack(fill="x", pady=(0, 6))
        found = self.found
        ttk.Label(pv, text="Scanner voices").pack(side="left", padx=(6, 0))
        self.var_pool = tk.StringVar(value=cfg["piper_pool"])
        ent = ttk.Entry(pv, textvariable=self.var_pool, width=26)
        ent.pack(side="left", padx=(2, 0))
        ent.bind("<FocusOut>", lambda e: self._changed())
        ent.bind("<Return>", lambda e: self._changed())
        tip(ent, "Piper speaker numbers the other cars' crew chiefs are drawn from on the scanner; each car keeps "
                 "the same voice every race. Picked for the radio: " +
                 ", ".join(f"{n} ({d})" for n, d in VOICE_CHOICES))
        ttk.Label(pv, text="   Drivers").pack(side="left")
        self.var_dpool = tk.StringVar(value=cfg["driver_pool"])
        ent = ttk.Entry(pv, textvariable=self.var_dpool, width=10)
        ent.pack(side="left", padx=(2, 0))
        ent.bind("<FocusOut>", lambda e: self._changed())
        ent.bind("<Return>", lambda e: self._changed())
        tip(ent, "Piper speaker numbers the other cars' drivers are drawn from on the scanner (each car keeps the "
                 "same one).")
        ttk.Label(pv, text="   Pace").pack(side="left")
        self.var_pspeed = tk.DoubleVar(value=float(cfg["piper_speed"]))
        sp = ttk.Spinbox(pv, from_=0.7, to=1.3, increment=0.02, width=5, textvariable=self.var_pspeed,
                         command=self._changed)
        sp.pack(side="left", padx=(2, 0))
        sp.bind("<FocusOut>", lambda e: self._changed())
        tip(sp, "Under 1 = quicker, over 1 = slower (0.92 = a touch quick, like a race radio)")
        ttk.Label(pv, text=("  voice pack found" if found else "  no voice pack (voices folder)"),
                  style="Dim.TLabel").pack(side="left", padx=(8, 0))

        cats = ttk.LabelFrame(tab, text="What gets said")
        cats.pack(fill="x", pady=(0, 6))
        self.cat_vars = {}
        for n, (key, label, text) in enumerate(CATEGORIES):
            v = tk.BooleanVar(value=cfg["cats"].get(key, True))
            self.cat_vars[key] = v
            c = ttk.Checkbutton(cats, text=label, variable=v, command=self._changed)
            c.grid(row=n // 3, column=n % 3, sticky="w", padx=6, pady=2)
            tip(c, text)

        self.var_mine = tk.BooleanVar(value=bool(cfg["alongside_mine"]))
        c = ttk.Checkbutton(cats, text="Inside / outside / clear for your own car too", variable=self.var_mine,
                            command=self._changed)
        c.grid(row=len(CATEGORIES) // 3 + 1, column=0, columnspan=2, sticky="w", padx=6, pady=2)
        tip(c, "NR2003's own spotter already calls cars alongside for the car you drive; tick this to hear the "
               "radio's spotter do it as well.")

        ttk.Label(tab, text="Said so far (also in aimod.log):").pack(anchor="w")
        self.lst = tk.Listbox(tab, bg=colors.get("field", "#111111"), fg=colors.get("fg", "#e6e6e6"),
                              font=("Consolas", 9), relief="flat", highlightthickness=0, activestyle="none")
        self.lst.pack(fill="both", expand=True)
        for line in radio.lines:
            self.lst.insert("end", line)
        radio.on_line = self.add_line

    def add_line(self, line: str):
        self.lst.insert("end", line)
        if self.lst.size() > 300:
            self.lst.delete(0)
        self.lst.see("end")

    def _custom_lines(self):
        """pick a radio moment, write lines for it (one per row); saved to the radio settings"""
        cfg = self.radio.cfg
        mine = {k: list(v) for k, v in (cfg.get("custom_lines") or {}).items()}
        win = tk.Toplevel(self.lst)
        win.title("Custom radio lines")
        win.transient(self.lst.winfo_toplevel())
        labels = [f"{who}: {what}" + (f"   ({len(mine[k])})" if mine.get(k) else "") for k, who, what, _ph in TRIGGERS]
        ttk.Label(win, text="Moment").grid(row=0, column=0, sticky="w", padx=10, pady=(10, 4))
        box = ttk.Combobox(win, values=labels, state="readonly", width=60)
        box.current(0)
        box.grid(row=0, column=1, columnspan=3, sticky="w", padx=(0, 10), pady=(10, 4))
        hint = ttk.Label(win, text="", style="Dim.TLabel")
        hint.grid(row=1, column=0, columnspan=4, sticky="w", padx=10)
        text = tk.Text(win, width=70, height=8, bg="#111111", fg="#e6e6e6", insertbackground="#e6e6e6",
                       relief="flat", wrap="word", font=("Segoe UI", 10))
        text.grid(row=2, column=0, columnspan=4, sticky="nsew", padx=10, pady=6)
        state = {"key": None}

        def keep():                                    # the box's lines back into the moment they belong to
            k = state["key"]
            if k is not None:
                lines = [x.strip() for x in text.get("1.0", "end").splitlines() if x.strip()]
                if lines:
                    mine[k] = lines
                else:
                    mine.pop(k, None)

        def show(_e=None):
            keep()
            i = box.current()
            k, who, what, ph = TRIGGERS[i]
            state["key"] = k
            text.delete("1.0", "end")
            text.insert("1.0", "\n".join(mine.get(k, [])))
            hint.configure(text="One line per row. Placeholders: {car} = car number, {pos} = position"
                                + (f", {ph}" if ph else "") + ("   Only AI drivers talk; your own car's driver never "
                                                             "does." if k.startswith("driver_") else ""))

        box.bind("<<ComboboxSelected>>", show)
        var_only = win.var_only = tk.BooleanVar(value=bool(cfg.get("custom_only", False)))   # kept with the window
        ttk.Checkbutton(win, text="Only my lines (for moments that have some)", variable=var_only).grid(
            row=3, column=0, columnspan=2, sticky="w", padx=10, pady=(0, 10))

        def hear():
            lines = [x.strip() for x in text.get("1.0", "end").splitlines() if x.strip()]
            if lines:
                k = state["key"]
                role = SPOTTER if TRIGGERS[box.current()][1] == "Spotter" else DRIVER if k.startswith("driver_") else CREW
                self.radio.say(None, role, lines[-1].replace("{car}", "24").replace("{pos}", "5"), 1, 8)

        def save():
            keep()
            cfg["custom_lines"] = mine
            cfg["custom_only"] = bool(var_only.get())
            self.save()
            win.destroy()

        ttk.Button(win, text="Hear the last line", command=hear).grid(row=3, column=2, sticky="e", padx=4, pady=(0, 10))
        ttk.Button(win, text="Save", command=save).grid(row=3, column=3, sticky="e", padx=10, pady=(0, 10))
        show()

    def _phone_poll(self):
        """a phone can switch "Phone only" (web server thread): show it here and save it"""
        on = bool(self.radio.cfg.get("phone_only", False))
        if on != bool(self.var_phone.get()):
            self.var_phone.set(on)
            self.save()
        self.lbl_phone.configure(text="phone listening" if self.radio.voice.phone_listening() else "")
        self.lbl_phone.after(1000, self._phone_poll)

    def _voice_text(self, saved, default) -> str:
        v = parse_voice(saved, default)
        if isinstance(v, int) and not self.found:
            return "Windows David"                   # no Piper voice pack: what will actually speak
        return voice_label(v) if isinstance(v, int) else "Windows " + v.split(":", 1)[1]

    def _preview(self, role):
        self._changed()
        if role == SPOTTER:
            self.radio.say(None, SPOTTER, "Car inside. Still there. Clear!", 0, 8)
        elif role == DRIVER:
            self.radio.say(None, DRIVER, "Driver voice check. This is how the AI drivers sound on the radio.", 1, 8)
        else:
            self.radio.say(None, CREW, "Bring it in this time. Four tires and full fuel. You're P 8, 1.2 to the 24.",
                           1, 10)


    SCAN_LABELS = {"all": "All cars", "teams": "All team cars", "team": "One team", "numbers": "These numbers"}

    def _team_choices(self) -> list[str]:
        names = [team_label(t, k) for k, t in enumerate(self.radio.teams() or [])]
        return names or ["(no teams yet - set them up in the Teams window)"]

    def _show_team_cars(self):
        want = self.var_team.get()
        cars = next((t["cars"] for k, t in enumerate(self.radio.teams() or []) if team_label(t, k) == want), [])
        self.lbl_team_cars.configure(text=" ".join(f"#{carno.label(n)}" for n in cars))

    def _team_picked(self):
        if not self.var_team.get().startswith("(no teams"):
            self.var_scan.set(self.SCAN_LABELS["team"])
        self._show_team_cars()
        self._changed()

    def _move_mode(self):
        if self.indicator is not None:
            self.indicator.set_moving(bool(self.var_imove.get()))
        if not self.var_imove.get():
            self.save()

    def _changed(self, redraw=False):
        cfg = self.radio.cfg
        cfg["mode"] = self.var_mode.get()
        cfg["scan"] = next((k for k, v in self.SCAN_LABELS.items() if v == self.var_scan.get()), "all")
        if not self.var_team.get().startswith("(no teams"):
            cfg["scan_team"] = self.var_team.get()
        cfg["scan_numbers"], cfg["favourites"] = self.var_nums.get().strip(), self.var_fav.get().strip()
        cfg["follow_tv"] = bool(self.var_tv.get())
        for key, var, default in (("crew_voice", self.var_vc, 600), ("spotter_voice", self.var_vs, 615),
                                  ("driver_voice", self.var_vd, 21)):
            v = parse_voice(var.get(), None)
            if v is not None:
                cfg[key] = f"Piper {v}" if isinstance(v, int) else "Windows " + v.split(":", 1)[1]
        cfg["alongside_mine"] = bool(self.var_mine.get())
        cfg["helmet"] = bool(self.var_helmet.get())
        cfg["driver_swears"] = bool(self.var_swear.get())
        try:
            cfg["helmet_engine"] = int(float(self.var_hlevel.get()))
        except (tk.TclError, ValueError):
            pass
        cfg["piper_pool"] = self.var_pool.get().strip()
        cfg["driver_pool"] = self.var_dpool.get().strip()
        try:
            cfg["piper_speed"] = round(max(0.6, min(1.5, float(self.var_pspeed.get()))), 2)
        except (tk.TclError, ValueError):
            pass
        icfg = cfg["indicator"]
        icfg["on"] = bool(self.var_ind.get())
        try:
            icfg["opacity"] = round(float(self.var_iop.get()), 2)
            icfg["size"] = round(float(self.var_isz.get()), 2)
        except (tk.TclError, ValueError):
            pass
        if redraw and self.indicator is not None:
            self.indicator.refresh()
        cfg["on"] = bool(self.var_on.get())
        car = self.var_car.get().strip().lstrip("#")
        cfg["car"] = "player" if car in ("", YOUR_CAR, "player") else car
        try:
            cfg["volume"] = int(float(self.var_vol.get()))
            cfg["rate"] = max(-5, min(6, int(self.var_rate.get())))
            cfg["report_laps"] = max(0, int(self.var_rep.get())) or 9999
        except (tk.TclError, ValueError):
            pass
        cfg["radio_fx"] = bool(self.var_fx.get())
        cfg["phone_only"] = bool(self.var_phone.get())
        for k, v in self.cat_vars.items():
            cfg["cats"][k] = bool(v.get())
        self.save()
