"""Teams window: car numbers grouped into team boxes, no names.

Drag a number into a box (or click a number, then click a box). Numbers not in
any team sit in "Unassigned". Saved per NR2003 mod by the caller.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .carno import CarNo, label
from .scrollframe import scrolled

BG, BOX, CHIP_FG, SEL, DIM, ACCENT = "#252525", "#1b1b1b", "#ffffff", "#ffd54f", "#8a8a8a", "#00d26a"
PER_ROW = 8


def normalize(teams) -> list[dict]:
    """Teams as [{"name": str, "cars": [numbers]}]; older saves were plain lists of numbers."""
    out = []
    for k, t in enumerate(teams or []):
        if isinstance(t, dict):
            cars = [CarNo(int(n)) for n in t.get("cars", [])]
            name = str(t.get("name") or "")
        else:
            cars, name = [CarNo(int(n)) for n in t], ""
        out.append({"name": name, "cars": sorted(set(cars))})
    return out


class TeamsDialog:
    def __init__(self, root, numbers: list[int], teams, colors: dict[int, str], on_save, title: str = "Teams",
                 from_car_files=None):
        teams = normalize(teams)
        self.numbers = sorted(set(numbers) | {n for t in teams for n in t["cars"]})
        self.teams = [list(t["cars"]) for t in teams]
        self.names = [t["name"] for t in teams]
        self.colors = colors
        self.on_save = on_save
        self.selected: int | None = None
        self._drag = None
        self.win = tk.Toplevel(root)
        self.win.title(title)
        self.win.configure(bg=BG, padx=12, pady=10)
        self.win.transient(root)
        ttk.Label(self.win, style="Panel.TLabel", wraplength=620, justify="left", text=(
            "Name each team in its box. Drag car numbers into team boxes (or click a number, then click a box). "
            "Teammates split their "
            "pit plans, follow their lead car's caution call, and pit together at drafting tracks.")
                  ).pack(anchor="w")
        # the buttons first, at the bottom: with many teams the boxes scroll instead of pushing them off screen
        bar = tk.Frame(self.win, bg=BG)
        bar.pack(side="bottom", fill="x")
        holder = tk.Frame(self.win, bg=BG)
        holder.pack(fill="both", expand=True, pady=(8, 8))
        self.body = scrolled(holder, BG, fit_height=max(200, int(self.win.winfo_screenheight() * 0.65)))
        ttk.Button(bar, text="+ Team", command=self.add_team).pack(side="left")
        ttk.Button(bar, text="Clear all", command=self.clear).pack(side="left", padx=(6, 0))
        self.from_car_files = from_car_files
        if from_car_files is not None:
            b = ttk.Button(bar, text="Fill from car files", command=self.fill_from_car_files)
            b.pack(side="left", padx=(6, 0))
            _tip(b, "Builds the teams from each car's team name in its .car file (Opponent Manager), for "
                       "the cars in this session - or every car in the mod's cars folder when NR2003 isn't in a "
                       "session. Replaces the teams shown here; nothing is saved until you press Save.")
        self.lbl_fill = ttk.Label(bar, style="Dim.TLabel")
        self.lbl_fill.pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Save", style="Accent.TButton", command=self.save).pack(side="right")
        self.render()

    # ------------------------------------------------------------- actions
    def add_team(self):
        self._grab_names()
        self.teams.append([])
        self.names.append("")
        self.render()

    def fill_from_car_files(self):
        teams, how = self.from_car_files()
        if not teams:
            self.lbl_fill.configure(text=how or "No team names found in the car files.")
            return
        self.numbers = sorted(set(self.numbers) | {n for t in teams for n in t["cars"]})
        self.teams = [list(t["cars"]) for t in teams]
        self.names = [t["name"] for t in teams]
        self.lbl_fill.configure(text=f"{len(teams)} teams from {how}")
        self.render()

    def clear(self):
        self.teams, self.names = [], []
        self.render()

    def remove_team(self, k):
        self._grab_names()
        del self.teams[k]
        del self.names[k]
        self.render()

    def _grab_names(self):
        for k, v in enumerate(getattr(self, "_name_vars", [])):
            if k < len(self.names):
                self.names[k] = v.get().strip()

    def move(self, number: int, target: int | None):
        """target = team index, or None for Unassigned."""
        self._grab_names()
        for t in self.teams:
            if number in t:
                t.remove(number)
        if target is not None and 0 <= target < len(self.teams):
            self.teams[target].append(number)
            self.teams[target].sort()
        self.selected = None
        self.render()

    def save(self):
        self._grab_names()
        self.on_save([{"name": n, "cars": sorted(t)} for t, n in zip(self.teams, self.names) if t or n])
        self.win.destroy()

    # ------------------------------------------------------------- drawing
    def render(self):
        for w in self.body.winfo_children():
            w.destroy()
        self._name_vars = []
        assigned = {n for t in self.teams for n in t}
        boxes = [(f"Team {k + 1}", k, t) for k, t in enumerate(self.teams)]
        boxes.append(("Unassigned", None, [n for n in self.numbers if n not in assigned]))
        cols = 2
        for b, (label, key, nums) in enumerate(boxes):
            frame = tk.Frame(self.body, bg=BOX, highlightthickness=1, highlightbackground="#3a3a3a", padx=6, pady=4)
            r, c = divmod(b, cols)
            if key is None:
                r, c = (len(boxes) + cols - 1) // cols, 0
            frame.grid(row=r, column=c, columnspan=1 if key is not None else cols, sticky="nsew", padx=4, pady=4)
            frame.team_key = key
            head = tk.Frame(frame, bg=BOX)
            head.pack(fill="x")
            head.team_key = key
            if key is not None:
                v = tk.StringVar(value=self.names[key])
                self._name_vars.append(v)
                ent = tk.Entry(head, textvariable=v, bg="#111111", fg=ACCENT, insertbackground=ACCENT, relief="flat",
                               font=("Segoe UI", 9, "bold"), width=24)
                ent.pack(side="left")
                if not v.get():
                    ent.insert(0, "")
                ent.bind("<FocusIn>", lambda e: None)
                tk.Label(head, text=f"  {label}", bg=BOX, fg=DIM, font=("Segoe UI", 8)).pack(side="left")
            else:
                tk.Label(head, text=label, bg=BOX, fg=DIM, font=("Segoe UI", 9, "bold")).pack(side="left")
            if key is not None:
                x = tk.Label(head, text="✕", bg=BOX, fg=DIM, cursor="hand2", font=("Segoe UI", 9))
                x.pack(side="right")
                x.bind("<Button-1>", lambda e, k=key: self.remove_team(k))
            grid = tk.Frame(frame, bg=BOX, height=28)
            grid.pack(fill="x", pady=(4, 0))
            grid.team_key = key
            if not nums:
                hint = tk.Label(grid, text="drop numbers here", bg=BOX, fg="#555555", font=("Segoe UI", 8))
                hint.grid(row=0, column=0, sticky="w")
                hint.team_key = key
                hint.bind("<Button-1>", lambda e, k=key: self._box_click(k))
            for i, n in enumerate(nums):
                self._chip(grid, n).grid(row=i // PER_ROW, column=i % PER_ROW, padx=2, pady=2)
            for w in (frame, head, grid):
                w.bind("<Button-1>", lambda e, k=key: self._box_click(k))
        for c in range(cols):
            self.body.columnconfigure(c, weight=1)

    def _chip(self, parent, n: int):
        color = self.colors.get(n, "#3a3a3a")
        try:
            r, g, b = (int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
            fg = "#000000" if 0.299 * r + 0.587 * g + 0.114 * b > 160 else CHIP_FG
        except ValueError:
            fg = CHIP_FG
        chip = tk.Label(parent, text=label(n), width=4, bg=color, fg=fg, font=("Bahnschrift SemiBold", 11),
                        relief="solid", bd=2 if self.selected == n else 0,
                        highlightthickness=2 if self.selected == n else 0, highlightbackground=SEL, cursor="hand2")
        chip.bind("<ButtonPress-1>", lambda e, n=n: self._press(e, n))
        chip.bind("<ButtonRelease-1>", lambda e, n=n: self._release(e, n))
        return chip

    def _press(self, e, n):
        self._drag = (n, e.x_root, e.y_root)

    def _release(self, e, n):
        drag, self._drag = self._drag, None
        moved = drag and (abs(e.x_root - drag[1]) > 6 or abs(e.y_root - drag[2]) > 6)
        if moved:
            target = self.win.winfo_containing(e.x_root, e.y_root)
            while target is not None and not hasattr(target, "team_key"):
                target = target.master
            if target is not None:
                self.move(n, target.team_key)
                return
        self.selected = None if self.selected == n else n
        self.render()

    def _box_click(self, key):
        if self.selected is not None:
            self.move(self.selected, key)


PRIORITY_LABELS = {"team": "Teammates first, then same make", "make": "Same make first, then teammates",
                   "team_only": "Teammates only", "make_only": "Same make only"}
TEAM_PITS_LABELS = {"split": "Split up: different strategies", "together": "Pit together (same lap)"}
RULE_FIELDS = [  # key, label, tooltip
    ("priority", "Who works together", "Which tie wins when a car has both a teammate and a same-make partner "
                                       "nearby: lining up green-flag stops and following caution calls."),
    ("team_pits", "Teammates on green-flag stops", "Split up = teammates spread over short-pit, split and "
                                                   "stretch plans. Pit together = they line up on the same lap."),
    ("team_gap", "Teammate gap (s)", "Teammates this many seconds apart or closer line up their stops. "
                                     "0 = wherever they are running."),
    ("team_follow", "Follow teammate under caution (%)", "Chance a car copies its best-placed teammate's "
                                                         "caution call (pit or stay out)."),
    ("make_gap", "Same-make gap (s)", "Same-make cars this many seconds apart or closer count as drafting "
                                      "partners."),
    ("make_pits", "Same make lines up green stops (%)", "Chance a same-make partner moves its green-flag stop "
                                                        "to pit with its partner (less the further apart)."),
    ("make_follow", "Follow same make under caution (%)", "Chance a car copies the caution call of a same-make "
                                                          "car just ahead of it."),
    ("window", "Line-up window (laps)", "Plans at most this many laps apart can be lined up (teammates get 2 "
                                        "more)."),
]


class RulesDialog:
    """Team and manufacturer strategy, one column for drafting tracks and one for the rest."""

    def __init__(self, root, rules: dict, defaults: dict, on_save, title: str = "Team & manufacturer strategy"):
        self.defaults, self.on_save = defaults, on_save
        self.win = tk.Toplevel(root)
        self.win.title(title)
        self.win.configure(bg=BG, padx=12, pady=10)
        self.win.transient(root)
        ttk.Label(self.win, style="Panel.TLabel", wraplength=560, justify="left", text=(
            "How teammates and same-make cars work together. Drafting tracks (Daytona, Talladega or the "
            "preset's drafting setting) use the left column, every other track the right. Saved per NR2003 mod."
        )).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        for c, head in ((1, "Drafting tracks"), (2, "Other tracks")):
            tk.Label(self.win, text=head, bg=BG, fg=ACCENT, font=("Segoe UI", 9, "bold")).grid(row=1, column=c,
                                                                                            padx=6, sticky="w")
        self.vars = {"draft": {}, "other": {}}
        for r, (key, label, tip) in enumerate(RULE_FIELDS, start=2):
            lbl = ttk.Label(self.win, text=label, style="Panel.TLabel")
            lbl.grid(row=r, column=0, sticky="w", pady=2)
            _tip(lbl, tip)
            for c, kind in ((1, "draft"), (2, "other")):
                var = tk.StringVar()
                if key in ("priority", "team_pits"):
                    labels = PRIORITY_LABELS if key == "priority" else TEAM_PITS_LABELS
                    w = ttk.Combobox(self.win, textvariable=var, values=list(labels.values()), state="readonly",
                                     width=30)
                else:
                    w = ttk.Entry(self.win, textvariable=var, width=8, justify="right")
                w.grid(row=r, column=c, sticky="w", padx=6, pady=2)
                self.vars[kind][key] = var
        bar = tk.Frame(self.win, bg=BG)
        bar.grid(row=len(RULE_FIELDS) + 2, column=0, columnspan=3, sticky="we", pady=(10, 0))
        ttk.Button(bar, text="Defaults", command=lambda: self.fill(self.defaults)).pack(side="left")
        ttk.Button(bar, text="Save", style="Accent.TButton", command=self.save).pack(side="right")
        self.fill(rules)

    def fill(self, rules: dict):
        for kind in ("draft", "other"):
            r = rules[kind]
            for key, *_ in RULE_FIELDS:
                v = getattr(r, key)
                if key == "priority":
                    v = PRIORITY_LABELS[v]
                elif key == "team_pits":
                    v = TEAM_PITS_LABELS[v]
                self.vars[kind][key].set(f"{v:g}" if isinstance(v, float) else str(v))

    def save(self):
        out = {}
        for kind in ("draft", "other"):
            d = {}
            for key, *_ in RULE_FIELDS:
                v = self.vars[kind][key].get().strip()
                if key == "priority":
                    d[key] = next(k for k, t in PRIORITY_LABELS.items() if t == v)
                elif key == "team_pits":
                    d[key] = next(k for k, t in TEAM_PITS_LABELS.items() if t == v)
                else:
                    try:
                        d[key] = max(0.0, float(v))
                    except ValueError:
                        d[key] = getattr(self.defaults[kind], key)
            out[kind] = d
        self.on_save(out)
        self.win.destroy()


def _tip(widget, text):
    """Small hover tip (the app's Tooltip lives in app.py; kept separate to avoid a circular import)."""
    state = {}

    def show(_):
        x, y = widget.winfo_rootx() + 12, widget.winfo_rooty() + widget.winfo_height() + 4
        t = state["t"] = tk.Toplevel(widget)
        t.wm_overrideredirect(True)
        t.wm_geometry(f"+{x}+{y}")
        tk.Label(t, text=text, bg="#333333", fg="#e6e6e6", justify="left", wraplength=340, font=("Segoe UI", 9),
                 padx=8, pady=5, relief="solid", bd=1).pack()

    def hide(_):
        if state.get("t"):
            state.pop("t").destroy()
    widget.bind("<Enter>", show, add="+")
    widget.bind("<Leave>", hide, add="+")
