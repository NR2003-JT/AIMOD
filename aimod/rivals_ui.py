"""Rivals window: rivalries as boxes of two car numbers, like the Teams window.

Drag a number from the car pool into a rivalry box (or click a number, then click a box). A driver can be in
several rivalries, so the pool never runs out. Drag a number out of a box onto the pool (or click it, then the pool)
to take it out. Saved per NR2003 mod by the caller, as pairs of car numbers.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .carno import CarNo, label
from .scrollframe import scrolled
from .teams import ACCENT, BG, BOX, CHIP_FG, DIM, SEL

PER_ROW = 12
POOL = "pool"


def normalize(pairs) -> list[list[int]]:
    """Saved pairs -> [[a, b], ...] (CarNo), duplicates and self-pairs dropped, order kept."""
    out, seen = [], set()
    for p in pairs or []:
        try:
            a, b = (CarNo(int(x)) for x in list(p)[:2])
        except (TypeError, ValueError):
            continue
        key = (min(a, b), max(a, b))
        if a == b or key in seen:
            continue
        seen.add(key)
        out.append([a, b])
    return out


class RivalsDialog:
    def __init__(self, root, numbers: list[int], pairs, colors: dict[int, str], on_save, title: str = "Rivals"):
        self.boxes: list[list[int]] = [list(p) for p in normalize(pairs)]
        self.numbers = sorted(set(CarNo(int(n)) for n in numbers) | {n for p in self.boxes for n in p})
        self.colors = colors
        self.on_save = on_save
        self.selected: tuple | None = None          # (number, box index or POOL)
        self._drag = None
        self.win = tk.Toplevel(root)
        self.win.title(title)
        self.win.configure(bg=BG, padx=12, pady=10)
        self.win.transient(root)
        ttk.Label(self.win, style="Panel.TLabel", wraplength=640, justify="left", text=(
            "Each box is a rivalry: two cars that race each other harder from the start of every race (rivals "
            "level 1); their run-ins raise it from there. Drag car numbers from the pool into a box (or click a "
            "number, then a box). A driver can have several rivals. Drag a number back to the pool to take it "
            "out.")).pack(anchor="w")
        bar = tk.Frame(self.win, bg=BG)
        bar.pack(side="bottom", fill="x")
        ttk.Button(bar, text="+ Rivalry", command=self.add_box).pack(side="left")
        ttk.Button(bar, text="Clear all", command=self.clear).pack(side="left", padx=(6, 0))
        self.lbl = ttk.Label(bar, style="Dim.TLabel")
        self.lbl.pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Save", style="Accent.TButton", command=self.save).pack(side="right")
        holder = tk.Frame(self.win, bg=BG)
        holder.pack(fill="both", expand=True, pady=(8, 8))
        self.body = scrolled(holder, BG, fit_height=max(200, int(self.win.winfo_screenheight() * 0.65)))
        if not self.boxes:
            self.boxes.append([])
        self.render()

    # ------------------------------------------------------------- actions
    def add_box(self):
        self.boxes.append([])
        self.render()

    def clear(self):
        self.boxes = [[]]
        self.render()

    def remove_box(self, k):
        del self.boxes[k]
        if not self.boxes:
            self.boxes.append([])
        self.render()

    def move(self, number: int, src, dst):
        """src / dst: box index or POOL. From the pool a number is copied; between boxes it moves."""
        self.selected = None
        note = ""
        if dst == POOL:
            if src != POOL and number in self.boxes[src]:
                self.boxes[src].remove(number)
        elif 0 <= dst < len(self.boxes) and dst != src:
            box = self.boxes[dst]
            if number in box:
                note = "That car is already in this rivalry."
            elif len(box) >= 2:
                note = "A rivalry is two cars - start a new one with + Rivalry."
            elif self._exists(number, box):
                note = f"#{label(number)} and #{label(box[0])} are already rivals."
            else:
                box.append(number)
                if src != POOL and number in self.boxes[src]:
                    self.boxes[src].remove(number)
        self.lbl.configure(text=note)
        self.render()

    def _exists(self, number, box) -> bool:
        if len(box) != 1:
            return False
        key = {number, box[0]}
        return any(set(p) == key for p in self.boxes if len(p) == 2)

    def save(self):
        pairs = [[int(a), int(b)] for a, b in (p for p in self.boxes if len(p) == 2)]
        self.on_save(pairs)
        self.win.destroy()

    # ------------------------------------------------------------- drawing
    def render(self):
        for w in self.body.winfo_children():
            w.destroy()
        pool = self._frame(0, 0, 2, POOL, "All cars (drag into a rivalry - a driver can have several)")
        grid = tk.Frame(pool, bg=BOX)
        grid.pack(fill="x", pady=(4, 0))
        grid.drop_key = POOL
        grid.bind("<Button-1>", lambda e: self._box_click(POOL))
        for i, n in enumerate(self.numbers):
            self._chip(grid, n, POOL).grid(row=i // PER_ROW, column=i % PER_ROW, padx=2, pady=2)
        if not self.numbers:
            tk.Label(grid, text="(load a session to see its cars)", bg=BOX, fg="#555555",
                     font=("Segoe UI", 8)).grid(row=0, column=0, sticky="w")
        for k, box in enumerate(self.boxes):
            r, c = divmod(k, 2)
            f = self._frame(r + 1, c, 1, k, f"Rivalry {k + 1}")
            row = tk.Frame(f, bg=BOX, height=34)
            row.pack(fill="x", pady=(4, 0))
            row.drop_key = k
            row.bind("<Button-1>", lambda e, k=k: self._box_click(k))
            for i in range(2):
                if i < len(box):
                    self._chip(row, box[i], k).pack(side="left", padx=2)
                else:
                    hint = tk.Label(row, text="drop a number", bg="#222222", fg="#555555", width=12,
                                    font=("Segoe UI", 8))
                    hint.pack(side="left", padx=2)
                    hint.drop_key = k
                    hint.bind("<Button-1>", lambda e, k=k: self._box_click(k))
                if i == 0:
                    vs = tk.Label(row, text="vs", bg=BOX, fg=DIM, font=("Segoe UI", 9, "bold"))
                    vs.pack(side="left", padx=4)
                    vs.drop_key = k
        for c in range(2):
            self.body.columnconfigure(c, weight=1)

    def _frame(self, r, c, span, key, title):
        frame = tk.Frame(self.body, bg=BOX, highlightthickness=1, highlightbackground="#3a3a3a", padx=6, pady=4)
        frame.grid(row=r, column=c, columnspan=span, sticky="nsew", padx=4, pady=4)
        frame.drop_key = key
        head = tk.Frame(frame, bg=BOX)
        head.pack(fill="x")
        head.drop_key = key
        tk.Label(head, text=title, bg=BOX, fg=ACCENT if key != POOL else DIM,
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        if key != POOL:
            x = tk.Label(head, text="✕", bg=BOX, fg=DIM, cursor="hand2", font=("Segoe UI", 9))
            x.pack(side="right")
            x.bind("<Button-1>", lambda e, k=key: self.remove_box(k))
        for w in (frame, head):
            w.bind("<Button-1>", lambda e, k=key: self._box_click(k))
        return frame

    def _chip(self, parent, n: int, where):
        color = self.colors.get(n, "#3a3a3a")
        try:
            r, g, b = (int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
            fg = "#000000" if 0.299 * r + 0.587 * g + 0.114 * b > 160 else CHIP_FG
        except ValueError:
            fg = CHIP_FG
        sel = self.selected == (n, where)
        chip = tk.Label(parent, text=label(n), width=4, bg=color, fg=fg, font=("Bahnschrift SemiBold", 11),
                        relief="solid", bd=2 if sel else 0, highlightthickness=2 if sel else 0,
                        highlightbackground=SEL, cursor="hand2")
        chip.bind("<ButtonPress-1>", lambda e: self._press(e))
        chip.bind("<ButtonRelease-1>", lambda e, n=n, w=where: self._release(e, n, w))
        return chip

    def _press(self, e):
        self._drag = (e.x_root, e.y_root)

    def _release(self, e, n, where):
        drag, self._drag = self._drag, None
        if drag and (abs(e.x_root - drag[0]) > 6 or abs(e.y_root - drag[1]) > 6):
            target = self.win.winfo_containing(e.x_root, e.y_root)
            while target is not None and not hasattr(target, "drop_key"):
                target = target.master
            if target is not None:
                self.move(n, where, target.drop_key)
                return
        self.selected = None if self.selected == (n, where) else (n, where)
        self.render()

    def _box_click(self, key):
        if self.selected is not None:
            n, src = self.selected
            self.move(n, src, key)
