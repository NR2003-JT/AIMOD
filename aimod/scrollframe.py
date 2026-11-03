"""A frame that scrolls up and down when what's in it is taller than the room it gets (tabs, pop-ups).

Kept on its own so app.py and teams.py can both use it without importing each other.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# widgets that scroll themselves: the wheel over them is theirs
_OWN_SCROLL = (tk.Text, tk.Listbox, ttk.Treeview)


def scrolled(holder, bg: str, padding: int = 0, fit_height: int | None = None) -> tk.Frame:
    """Fills `holder` (an empty container) with a scrolling area and returns the frame to put things in.
    fit_height: for a pop-up - the area is as tall as its content, up to this many pixels, then scrolls (the
    buttons packed below it stay on screen)."""
    canvas = tk.Canvas(holder, bg=bg, highlightthickness=0, bd=0)
    sb = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas, bg=bg, padx=padding, pady=padding)
    win = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=sb.set)

    def update(_=None):
        canvas.configure(scrollregion=canvas.bbox("all"))
        if fit_height is not None:
            canvas.configure(height=min(inner.winfo_reqheight(), fit_height), width=inner.winfo_reqwidth())
        fits = inner.winfo_reqheight() <= max(1, canvas.winfo_height())
        if fits:
            if sb.winfo_ismapped():
                sb.pack_forget()
            canvas.yview_moveto(0)
        elif not sb.winfo_ismapped():
            sb.pack(side="right", fill="y", before=canvas)

    inner.bind("<Configure>", update)
    canvas.bind("<Configure>", lambda e: (canvas.itemconfigure(win, width=e.width), update()))
    canvas.pack(side="left", fill="both", expand=True)

    def inside(x, y) -> bool:
        w = canvas.winfo_containing(x, y)
        return w is not None and str(w).startswith(str(canvas))

    def wheel(e):
        if not sb.winfo_ismapped() or not inside(e.x_root, e.y_root):
            return
        if isinstance(canvas.winfo_containing(e.x_root, e.y_root), _OWN_SCROLL):
            return
        canvas.yview_scroll(int(-e.delta / 120), "units")

    # bound while the pointer is over the area (leaving into a child widget is not leaving the area)
    canvas.bind("<Enter>", lambda _: canvas.bind_all("<MouseWheel>", wheel), add="+")
    canvas.bind("<Leave>", lambda e: None if inside(e.x_root, e.y_root) else canvas.unbind_all("<MouseWheel>"),
                add="+")
    inner.bind("<Enter>", lambda _: canvas.bind_all("<MouseWheel>", wheel), add="+")
    return inner
