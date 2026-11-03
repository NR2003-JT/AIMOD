"""Canvas-drawn table: per-cell text colours, per-cell hover text, click-to-sort
headers, drag-to-resize columns, click-to-highlight rows and a sticky header
row. ttk.Treeview can only colour whole rows."""
from __future__ import annotations

import re
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass, field
from typing import Any, Callable

NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


@dataclass
class Column:
    id: str
    head: str
    width: int
    anchor: str = "w"            # "w" left, "e" right, "center"
    tip: str = ""


@dataclass
class Row:
    key: Any
    cells: list[str]
    colors: list[str | None] = field(default_factory=list)   # per cell; None = row colour
    fg: str = "#e6e6e6"
    sort: dict[str, Any] = field(default_factory=dict)        # column id -> sort value override
    banner: str = ""                                          # full-width title row (cells ignored)


def sort_value(text: str):
    """Numbers sort as numbers (first number in the cell), blanks last."""
    t = text.strip()
    if not t:
        return (2, 0, "")
    m = NUM_RE.search(t)
    if m and m.start() <= 2:
        return (0, float(m.group()), t.lower())
    return (1, 0, t.lower())


class ColorTable(tk.Frame):
    def __init__(self, parent, columns: list[Column], *, bg="#111111", stripe="#171717", head_bg="#2c2c2c",
                 head_fg="#00d26a", grid="#262626", font=("Consolas", 9), head_font=("Segoe UI", 9, "bold"),
                 row_h=20, hover: Callable[[Any, str], str | None] | None = None,
                 tip_bg="#333333", tip_fg="#e6e6e6", sortable=True, sort_col: str | None = None):
        super().__init__(parent, bg=bg)
        self.bg, self.stripe, self.head_bg, self.head_fg, self.grid_c = bg, stripe, head_bg, head_fg, grid
        self.font = tkfont.Font(font=font)
        self.head_font = tkfont.Font(font=head_font)
        self.char_w = max(1, self.font.measure("0"))
        self.row_h = row_h
        self.hover = hover
        self.tip_bg, self.tip_fg = tip_bg, tip_fg
        self.sortable = sortable
        self.sort_col, self.sort_rev = sort_col, False
        self.on_sort: Callable[[str, bool], None] | None = None
        self.on_resize: Callable[[dict[str, int]], None] | None = None
        self.selected = None
        self.select_bg = "#1f4f3a"
        self._drag = None
        self.rows: list[Row] = []
        self._drawn = None
        self._tip = None
        self._tip_cell = None

        self.head = tk.Canvas(self, height=row_h + 4, bg=head_bg, highlightthickness=0, bd=0)
        self.body = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.vsb = tk.Scrollbar(self, orient="vertical", command=self.body.yview)
        self.hsb = tk.Scrollbar(self, orient="horizontal", command=self._xview)
        self.body.configure(yscrollcommand=self.vsb.set, xscrollcommand=self.hsb.set)
        self.head.configure(xscrollcommand=self.hsb.set)
        self.head.grid(row=0, column=0, sticky="ew")
        self.body.grid(row=1, column=0, sticky="nsew")
        self.vsb.grid(row=1, column=1, sticky="ns")
        self.hsb.grid(row=2, column=0, sticky="ew")
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        self.head.bind("<Button-1>", self._head_press)
        self.head.bind("<B1-Motion>", self._head_drag)
        self.head.bind("<ButtonRelease-1>", self._head_release)
        self.body.bind("<Button-1>", self._body_click)
        self.head.bind("<Motion>", self._head_motion)
        self.head.bind("<Leave>", lambda e: self._hide_tip())
        self.body.bind("<Motion>", self._body_motion)
        self.body.bind("<Leave>", lambda e: self._hide_tip())
        for w in (self.body, self.head):
            w.bind("<Enter>", lambda e: self.body.bind_all("<MouseWheel>", self._wheel))
            w.bind("<Leave>", lambda e: (self.body.unbind_all("<MouseWheel>"), self._hide_tip()), add="+")
        self.body.bind("<Configure>", lambda e: self._redraw(force=True))
        self.set_columns(columns)

    # ------------------------------------------------------------- columns
    def set_columns(self, columns: list[Column]):
        self.columns = list(columns)
        self.col_x = []
        x = 0
        for c in self.columns:
            self.col_x.append(x)
            x += c.width
        self.total_w = x
        self._draw_head()
        self._redraw(force=True)

    def _xview(self, *args):
        self.body.xview(*args)
        self.head.xview(*args)

    def _wheel(self, e):
        self.body.yview_scroll(int(-e.delta / 120) * 3, "units")
        self._hide_tip()

    def _draw_head(self):
        h = self.head
        h.delete("all")
        for c, x in zip(self.columns, self.col_x):
            label = c.head
            if c.id == self.sort_col:
                label += " ▼" if self.sort_rev else " ▲"
            tx = {"w": x + 5, "e": x + c.width - 5}.get(c.anchor, x + c.width / 2)
            anchor = {"w": "w", "e": "e"}.get(c.anchor, "center")
            h.create_text(tx, (self.row_h + 4) / 2, text=label, fill=self.head_fg, font=self.head_font,
                          anchor=anchor)
            h.create_line(x + c.width - 1, 3, x + c.width - 1, self.row_h + 1, fill="#3a3a3a")
        h.configure(scrollregion=(0, 0, self.total_w, self.row_h + 4))

    # ---------------------------------------------------------------- rows
    def set_rows(self, rows: list[Row]):
        self.rows = rows
        self._apply_sort()
        self._redraw()

    def sort_by(self, col: str, rev: bool | None = None):
        if rev is None:
            rev = (not self.sort_rev) if col == self.sort_col else False
        self.sort_col, self.sort_rev = col, rev
        self._draw_head()
        self._apply_sort()
        self._redraw()
        if self.on_sort:
            self.on_sort(col, rev)

    def _apply_sort(self):
        if not (self.sortable and self.sort_col):
            return
        ids = [c.id for c in self.columns]
        if self.sort_col not in ids:
            return
        k = ids.index(self.sort_col)

        def key(r: Row):
            if self.sort_col in r.sort:
                v = r.sort[self.sort_col]
                return (2, 0, "") if v is None else (0, v, "")
            return sort_value(r.cells[k] if k < len(r.cells) else "")
        blanks = [r for r in self.rows if key(r)[0] == 2]
        rest = sorted((r for r in self.rows if key(r)[0] != 2), key=key, reverse=self.sort_rev)
        self.rows = rest + blanks

    def _fit(self, text: str, width: int) -> str:
        n = max(1, (width - 8) // self.char_w)
        return text if len(text) <= n else text[:max(1, n - 1)] + "…"

    def _redraw(self, force: bool = False):
        sig = (tuple((r.key, tuple(r.cells), tuple(r.colors), r.fg, r.banner) for r in self.rows),
               tuple((c.id, c.width) for c in self.columns), self.body.winfo_width(), self.selected)
        if sig == self._drawn and not force:
            return
        self._drawn = sig
        b = self.body
        b.delete("all")
        width = max(self.total_w, b.winfo_width())
        rh = self.row_h
        for n, r in enumerate(self.rows):
            y = n * rh
            if r.banner:
                b.create_rectangle(0, y, width, y + rh, fill="#202020", width=0)
                b.create_text(8, y + rh / 2, text=r.banner, fill=r.fg, font=self.head_font, anchor="w")
                continue
            fill = self.select_bg if r.key == self.selected else (self.stripe if n % 2 else self.bg)
            b.create_rectangle(0, y, width, y + rh, fill=fill, width=0)
            for k, (c, x) in enumerate(zip(self.columns, self.col_x)):
                text = r.cells[k] if k < len(r.cells) else ""
                if not text:
                    continue
                color = (r.colors[k] if k < len(r.colors) else None) or r.fg
                tx = {"w": x + 5, "e": x + c.width - 5}.get(c.anchor, x + c.width / 2)
                anchor = {"w": "w", "e": "e"}.get(c.anchor, "center")
                b.create_text(tx, y + rh / 2, text=self._fit(str(text), c.width), fill=color, font=self.font,
                              anchor=anchor)
        for x in self.col_x[1:]:
            b.create_line(x - 1, 0, x - 1, max(len(self.rows) * rh, 1), fill=self.grid_c)
        b.configure(scrollregion=(0, 0, self.total_w, len(self.rows) * rh))

    # --------------------------------------------------------------- hover
    def _cell_at(self, e):
        x = self.body.canvasx(e.x)
        y = self.body.canvasy(e.y)
        n = int(y // self.row_h)
        if not (0 <= n < len(self.rows)):
            return None
        for c, cx in zip(self.columns, self.col_x):
            if cx <= x < cx + c.width:
                return n, c.id
        return None

    def _col_at_head(self, e):
        x = self.head.canvasx(e.x)
        for c, cx in zip(self.columns, self.col_x):
            if cx <= x < cx + c.width:
                return c
        return None

    def _border_at(self, e):
        """Index of the column whose right edge is under the pointer, if any."""
        x = self.head.canvasx(e.x)
        for k, (c, cx) in enumerate(zip(self.columns, self.col_x)):
            if abs(cx + c.width - x) <= 4:
                return k
        return None

    def _head_press(self, e):
        k = self._border_at(e)
        if k is not None:
            self._drag = (k, self.head.canvasx(e.x), self.columns[k].width)
            return
        c = self._col_at_head(e)
        if c and self.sortable:
            self.sort_by(c.id)

    def _head_drag(self, e):
        if self._drag is None:
            return
        k, x0, w0 = self._drag
        self.columns[k].width = max(24, int(w0 + self.head.canvasx(e.x) - x0))
        self.set_columns(self.columns)

    def _head_release(self, e):
        if self._drag is not None:
            self._drag = None
            if self.on_resize:
                self.on_resize({c.id: c.width for c in self.columns})

    def set_widths(self, widths: dict[str, int]):
        for c in self.columns:
            if c.id in widths:
                c.width = max(24, int(widths[c.id]))
        self.set_columns(self.columns)

    def _body_click(self, e):
        cell = self._cell_at(e)
        key = self.rows[cell[0]].key if cell else None
        self.selected = None if key == self.selected else key
        self._redraw(force=True)

    def _head_motion(self, e):
        self.head.configure(cursor="sb_h_double_arrow" if self._border_at(e) is not None else "")
        c = self._col_at_head(e)
        cell = ("head", c.id) if c else None
        if cell != self._tip_cell:
            self._hide_tip()
            if c and c.tip:
                self._show_tip(e, c.tip, cell)

    def _body_motion(self, e):
        cell = self._cell_at(e)
        if cell == self._tip_cell:
            return
        self._hide_tip()
        if cell and self.hover:
            text = self.hover(self.rows[cell[0]].key, cell[1])
            if text:
                self._show_tip(e, text, cell)

    def _show_tip(self, e, text, cell):
        self._tip_cell = cell
        self._tip = tk.Toplevel(self)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f"+{e.x_root + 14}+{e.y_root + 12}")
        tk.Label(self._tip, text=text, bg=self.tip_bg, fg=self.tip_fg, justify="left", font=("Segoe UI", 9),
                 padx=8, pady=5, relief="solid", bd=1, wraplength=380).pack()

    def _hide_tip(self):
        if self._tip is not None:
            self._tip.destroy()
        self._tip = None
        self._tip_cell = None
