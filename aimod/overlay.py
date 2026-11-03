"""In-game pit board: a transparent, always-on-top, click-through window drawn
with a Tk canvas (web views can't be made see-through on Windows; the companion app's
own overlays need NR2003 in windowed/borderless mode, and so does this).

Styled after broadcast "race off pit road" towers: series bar, track and mode
title, drivers who stayed out, then one row per car with number art on the
driver's colour, headshot, name, and what it took / places gained or a live
service clock. Number art, headshots and colours come from the companion app's series
folder (see companion_art)."""
from __future__ import annotations

import ctypes
import time
import tkinter as tk
import tkinter.font as tkfont

try:
    from PIL import Image, ImageTk
except ImportError:          # images are optional; text fallbacks are drawn instead
    Image = ImageTk = None

KEY = "#010203"                 # colour made fully transparent
BLACK, TITLE_BG, ROW_BG, ROW_BG2, TEXT, DIM = "#070707", "#101010", "#141414", "#1b1b1b", "#f4f4f4", "#9a9a9a"
GREEN, YELLOW, RED, BLUE = "#3ddc84", "#ffcc33", "#ff4d4d", "#1d3f8f"

GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x20, 0x80, 0x08000000


def _font_family(root) -> str:
    fams = set(tkfont.families(root))
    for f in ("Bahnschrift SemiBold Condensed", "Bahnschrift Condensed", "Bahnschrift", "Arial Narrow", "Segoe UI"):
        if f in fams:
            return f
    return "Segoe UI"


def _rgb(c: str):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4)) if len(c) == 6 else (58, 58, 58)


def _text_on(c: str) -> str:
    r, g, b = _rgb(c)
    return "#000000" if 0.299 * r + 0.587 * g + 0.114 * b > 150 else "#ffffff"


def _mix(c: str, other: str, t: float) -> str:
    a, b = _rgb(c), _rgb(other)
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


class PitBoardWindow:
    def __init__(self, root, settings: dict, on_moved=None, artdata=None, on_resized=None):
        self.root = root
        self.settings = settings
        self.on_moved = on_moved
        self.on_resized = on_resized
        self._resize = None
        self.artdata = artdata
        self.win = None
        self.canvas = None
        self.unlocked = False
        self.family = _font_family(root)
        self._drag = None
        self._last = None
        self._imgs: dict = {}
        self._live_imgs: list = []
        self._data: dict = {}
        self._ypos: dict = {}
        self._anim_job = None
        self._fronted = 0.0

    # ---------------------------------------------------------------- window
    @property
    def scale(self) -> float:
        return max(0.5, min(3.0, float(self.settings.get("scale", 1.0))))

    @property
    def width(self) -> int:
        return int(max(200, min(700, float(self.settings.get("width", 280)))))

    def show(self):
        if self.win is not None:
            return
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=KEY)
        w.attributes("-transparentcolor", KEY)
        w.attributes("-alpha", max(0.2, min(1.0, float(self.settings.get("opacity", 1.0)))))
        w.geometry(f"+{int(self.settings.get('x', 40))}+{int(self.settings.get('y', 160))}")
        self.canvas = tk.Canvas(w, bg=KEY, highlightthickness=0, bd=0, width=10, height=10)
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<Motion>", self._hover)
        self.win = w
        w.update_idletasks()
        self._apply_click_through()
        self._last = None

    def hide(self):
        if self.win is not None:
            self.win.destroy()
        self.win = self.canvas = None

    def set_unlocked(self, unlocked: bool):
        self.unlocked = unlocked
        self._apply_click_through()
        self._last = None
        if self.win is not None:
            self._draw()                     # show (or leave) the full-size preview right away

    def apply_settings(self):
        if self.win is not None:
            self.win.attributes("-alpha", max(0.2, min(1.0, float(self.settings.get("opacity", 1.0)))))
            self._last = None
            self._imgs.clear()
            self._draw()

    def _hwnd(self):
        return ctypes.windll.user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()

    def _apply_click_through(self):
        if self.win is None:
            return
        u = ctypes.windll.user32
        h = self._hwnd()
        st = u.GetWindowLongW(h, GWL_EXSTYLE) | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        st = st & ~WS_EX_TRANSPARENT if self.unlocked else st | WS_EX_TRANSPARENT
        u.SetWindowLongW(h, GWL_EXSTYLE, st)

    def _front(self):
        """Back on top every 2 s: Tk sets topmost once, and the game or another topmost overlay
        (Race Control, Project SteelFire) shown later covers it (beta tester, 2026-10-28)."""
        now = time.monotonic()
        if self.win is None or now - self._fronted < 2.0:
            return
        self._fronted = now
        ctypes.windll.user32.SetWindowPos(self._hwnd(), -1, 0, 0, 0, 0,
                                          0x0001 | 0x0002 | 0x0010)    # TOPMOST, no move/size/activate

    GRIP = 22                      # px, bottom-right corner handle in Move mode
    SCALE_RANGE, WIDTH_RANGE = (0.5, 3.0), (200, 700)

    def _on_grip(self, e) -> bool:
        c = self.canvas
        return (self.unlocked and e.x >= int(c.cget("width")) - self.GRIP
                and e.y >= int(c.cget("height")) - self.GRIP)

    def _press(self, e):
        if not self.unlocked:
            return
        if self._on_grip(e):                        # resize: right/left = width, down/up = scale
            self._resize = (e.x_root, e.y_root, self.width, self.scale, int(self.canvas.cget("height")))
        else:
            self._drag = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())

    def _move(self, e):
        if self._resize:
            x0, y0, w0, s0, h0 = self._resize
            scale = max(self.SCALE_RANGE[0], min(self.SCALE_RANGE[1], s0 * (1 + (e.y_root - y0) / max(80, h0))))
            width = max(self.WIDTH_RANGE[0], min(self.WIDTH_RANGE[1], w0 + (e.x_root - x0) / scale))
            self.settings["scale"], self.settings["width"] = round(scale, 2), int(width)
            self._last = None
            self._imgs.clear()
            self._draw()
        elif self._drag:
            self.win.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _hover(self, e):
        self.canvas.configure(cursor="size_nw_se" if self._on_grip(e) else ("fleur" if self.unlocked else ""))

    def _release(self, e):
        if self._resize:
            self._resize = None
            if self.on_resized:
                self.on_resized()
            if self.on_moved:
                self.on_moved()
        elif self._drag:
            self._drag = None
            self.settings["x"], self.settings["y"] = self.win.winfo_x(), self.win.winfo_y()
            if self.on_moved:
                self.on_moved()

    # ---------------------------------------------------------------- images
    def _image(self, rel: str | None, box_w: int, box_h: int, cover: bool = False):
        """The companion app art scaled to fit (or cover) a box; cached. None if unavailable."""
        if not rel or Image is None or self.artdata is None:
            return None
        key = (rel, box_w, box_h, cover)
        if key in self._imgs:
            return self._imgs[key]
        img = None
        path = self.artdata.abs_path(rel)
        if path:
            try:
                im = Image.open(path).convert("RGBA")
                r = (max if cover else min)(box_w / im.width, box_h / im.height)
                im = im.resize((max(1, round(im.width * r)), max(1, round(im.height * r))), Image.LANCZOS)
                if cover:
                    left, top = (im.width - box_w) // 2, 0
                    im = im.crop((left, top, left + box_w, top + box_h))
                img = ImageTk.PhotoImage(im)
            except Exception:
                img = None
        self._imgs[key] = img
        return img

    # ---------------------------------------------------------------- render
    def render(self, data: dict | None):
        if self.win is None:
            return
        try:
            if not self.canvas.winfo_exists():
                return                            # window being torn down (closing)
        except tk.TclError:
            return
        self._front()
        data = data or {}
        sig = (repr(data) if not self.unlocked else "preview", self.unlocked, self.scale, self.width,
               self.settings.get("max_rows"),
               self.settings.get("auto_hide"), self.settings.get("compact"))
        if sig == self._last:
            return
        self._last = sig
        self._data = data
        self._draw()

    def _animate(self):
        """Ease rows towards their slots (new rows rise in from below, rows move up into freed slots)."""
        self._anim_job = None
        if self.win is not None:
            self._draw()

    def _preview(self, data: dict) -> dict:
        """Move mode: the board at the largest it can get (stayed-out list, both 'coming in'
        strips, every row slot and the '+N more' line) so it can be positioned for real."""
        max_rows = int(self.settings.get("max_rows", 10))
        palette = ["#c99a2e", "#1f4e9c", "#d7261e", "#b3122e", "#2e8b57", "#6a1b9a", "#ef6c00", "#00838f"]
        head = self.artdata.rel_headshot(0) if self.artdata else None
        states = ["exit", "exit", "stall", "stall", "road"]
        stops = []
        for k in range(max_rows + 1):
            st = states[k] if k < len(states) else "road"
            stops.append({"idx": -100 - k, "stop": 1, "number": k + 1, "last": f"DRIVER {k + 1}", "make": "",
                          "code": "F+L/R", "label": "4 TIRES", "damage": False, "state": st,
                          "pos_in": k + 3, "pos_out": None, "pos_now": k + 3, "gained": None,
                          "service": 12.5 if st != "road" else 0.0, "color": palette[k % len(palette)],
                          "number_img": None, "headshot": head})
        stayed = [{"pos": k + 1, "number": 90 + k, "last": f"STAYED OUT {k + 1}", "color": palette[k],
                   "number_img": None} for k in range(6)]
        coming = [{"number": 50 + k, "last": "", "this_time_by": k < 3, "pos": 20 + k, "color": palette[k]}
                  for k in range(6)]
        return dict(data, stops=stops, stayed_out=stayed, coming_in=coming, mode="race_off",
                    track=data.get("track") or "TRACK", lap=data.get("lap") or 0)

    def _draw(self):
        data = self._preview(self._data) if self.unlocked else self._data
        stops = data.get("stops", [])
        stayed = data.get("stayed_out", [])
        max_rows = int(self.settings.get("max_rows", 10))
        auto_hide = self.settings.get("auto_hide", True)
        compact = bool(self.settings.get("compact", False))
        c = self.canvas
        c.delete("all")
        self._live_imgs = []
        coming = data.get("coming_in", [])
        groups = [(lbl, col, [c for c in coming if c["this_time_by"] == flag])
                  for lbl, col, flag in (("THIS TIME BY", YELLOW, True), ("PITTING SOON", DIM, False))]
        groups = [g for g in groups if g[2]]
        if not stops and not coming and auto_hide and not self.unlocked:
            c.configure(width=1, height=1)
            return
        s = self.scale
        W = int(self.width * s)
        SER, TITLE, SROW, SBAR = int(20 * s), int(46 * s), int(17 * s), int(18 * s)
        logo = self._image(data.get("logo"), int(W * 0.7), int(30 * s)) if data.get("logo") else None
        if logo is not None:
            SER = int(36 * s)                                   # taller series bar for a logo
        RH = int((24 if compact else 40) * s)
        shown = stops[:max_rows]
        extra = len(stops) - len(shown)
        stayed_shown = stayed[:6]
        CROW = int(18 * s)
        H = SER + TITLE + (len(stayed_shown) * SROW + SBAR if stayed_shown else 0) + CROW * len(groups) \
            + RH * max(1, len(shown)) + (int(18 * s) if extra > 0 else 0)
        c.configure(width=W, height=H)
        f = self.family
        fz = lambda n, w="bold italic": (f, max(6, int(n * s)), w)

        # series bar
        c.create_rectangle(0, 0, W, SER, fill=BLACK, width=0)
        if logo is not None:
            c.create_image(W / 2, SER / 2, image=logo)
            self._live_imgs.append(logo)
        else:
            c.create_text(W / 2, SER / 2, text=(data.get("series_name") or "AIMOD").upper(), fill=TEXT,
                          font=fz(10))
        # title: track + mode
        y = SER
        caution = data.get("flag") != "green"
        c.create_rectangle(0, y, W, y + TITLE, fill=TITLE_BG, width=0)
        x = int(10 * s)
        for k, col in enumerate(("#e10600", "#ffffff", "#0b3fa5")):
            c.create_polygon(x + k * 6 * s, y + 21 * s, x + k * 6 * s + 4 * s, y + 7 * s,
                             x + k * 6 * s + 8 * s, y + 7 * s, x + k * 6 * s + 4 * s, y + 21 * s, fill=col, width=0)
        track = data.get("track") or ""
        c.create_text(x + 24 * s, y + 14 * s, text=track, fill=TEXT, font=fz(15), anchor="w")
        lap = data.get("lap")
        if lap:
            c.create_text(W - 8 * s, y + 14 * s, text=f"LAP {lap}/{data.get('total')}", fill=DIM, font=fz(9, "bold"),
                          anchor="e")
        mode = "RACE OFF PIT ROAD" if data.get("mode") == "race_off" else "PIT ROAD"
        c.create_text(W / 2, y + 33 * s, text=mode, fill=TEXT, font=fz(14))
        c.create_rectangle(0, y + TITLE - 3 * s, W, y + TITLE, fill=YELLOW if caution else GREEN, width=0)
        y += TITLE

        # drivers who stayed out
        if stayed_shown:
            for d in stayed_shown:
                c.create_rectangle(0, y, W, y + SROW, fill=BLACK, width=0)
                c.create_text(8 * s, y + SROW / 2, text=f"{d['pos']}.", fill=DIM, font=fz(9, "bold"), anchor="w")
                img = self._image(d.get("number_img"), int(26 * s), SROW - 3)
                if img:
                    self._live_imgs.append(img)
                    c.create_image(42 * s, y + SROW / 2, image=img)
                else:
                    col_bg = d.get("color") or "#3a3a3a"
                    c.create_rectangle(30 * s, y + 1, 54 * s, y + SROW - 1, fill=col_bg, width=0)
                    c.create_text(42 * s, y + SROW / 2, text=str(d["number"]), fill=_text_on(col_bg), font=fz(10))
                c.create_text(62 * s, y + SROW / 2, text=d["last"], fill=TEXT, font=fz(10), anchor="w")
                y += SROW
            n = len(stayed)
            c.create_rectangle(0, y, W, y + SBAR, fill=BLUE, width=0)
            c.create_text(W / 2, y + SBAR / 2, text=f"{n} DRIVER{'S' if n != 1 else ''} STAYED OUT", fill=TEXT,
                          font=fz(10))
            y += SBAR

        # cars asked to pit but not on pit road yet: small strip; full rows once they're on pit road
        for label, col, cars in groups:
            c.create_rectangle(0, y, W, y + CROW, fill=BLACK, width=0)
            lid = c.create_text(8 * s, y + CROW / 2, text=label, fill=col, font=fz(9, "bold"), anchor="w")
            bx = c.bbox(lid)
            x = (bx[2] if bx else 80 * s) + 8 * s
            for d in cars:
                if x > W - 20 * s:
                    c.create_text(W - 6 * s, y + CROW / 2, text="...", fill=DIM, font=fz(9, "bold"), anchor="e")
                    break
                col_bg = d.get("color") or "#3a3a3a"     # number on a chip of the driver's colour
                tid = c.create_text(x + 4 * s, y + CROW / 2, text=str(d["number"]), fill=_text_on(col_bg),
                                    font=fz(11), anchor="w")
                bx = c.bbox(tid)
                if bx:
                    rid = c.create_rectangle(bx[0] - 4 * s, y + 2 * s, bx[2] + 4 * s, y + CROW - 2 * s,
                                             fill=col_bg, width=0)
                    c.tag_lower(rid, tid)
                x = (bx[2] if bx else x + 16 * s) + 10 * s
            y += CROW

        if not shown:
            c.create_rectangle(0, y, W, y + RH, fill=ROW_BG, width=0)
            c.create_text(W / 2, y + RH / 2, text="Pit road empty - drag to move" if self.unlocked else "",
                          fill=DIM, font=fz(10, "normal"))
        nw = int(46 * s) if compact else int(W * 0.30)
        hs = RH - 2
        top = y
        moving = False
        keys = []
        for n, d in enumerate(shown):
            key = (d["idx"], d["stop"])
            keys.append(key)
            target = top + n * RH
            cur = self._ypos.get(key, target + RH)          # new rows rise in from below
            if abs(target - cur) > 0.6:
                cur += (target - cur) * 0.35
                moving = True
            else:
                cur = target
            self._ypos[key] = cur
        self._ypos = {k: v for k, v in self._ypos.items() if k in keys}
        for n, d in reversed(list(enumerate(shown))):       # lower rows first, so a rising row slides under
            row = self._row_compact if compact else self._row
            row(c, d, self._ypos[keys[n]], W, RH, nw, hs, s, fz, data.get("mode"), n)
        y = top + RH * len(shown)
        if extra > 0:
            c.create_rectangle(0, y, W, y + 18 * s, fill=BLACK, width=0)
            c.create_text(W / 2, y + 9 * s, text=f"+{extra} more", fill=DIM, font=fz(9, "bold"))
        if self.unlocked:
            c.create_rectangle(1, 1, W - 1, H - 1, outline="#4aa8ff", width=2, dash=(4, 3))
            c.create_rectangle(W / 2 - 92 * s, H - 26 * s, W / 2 + 92 * s, H - 6 * s, fill="#4aa8ff", width=0)
            c.create_text(W / 2, H - 16 * s, text="FULL SIZE PREVIEW - drag, then Lock", fill="#000000",
                          font=fz(10, "bold"))
            g = self.GRIP                               # resize grip, bottom-right
            c.create_polygon(W - 2, H - g, W - 2, H - 2, W - g, H - 2, fill="#4aa8ff", width=0)
            for k in (6, 11, 16):
                c.create_line(W - 3, H - k, W - k, H - 3, fill="#0b2a4a", width=1)
            c.create_text(W - g - 4, H - 8, text=f"{self.width}px  x{self.scale:.2f}", fill="#4aa8ff",
                          font=(self.family, 8, "bold"), anchor="e")
        if moving and self._anim_job is None:
            self._anim_job = self.win.after(30, self._animate)

    def _row_compact(self, c, d, y, W, RH, nw, hs, s, fz, mode, n):
        """One-line row: number on the driver's colour, position, name, places gained, service / clock."""
        retired = d["state"] == "retired"
        color = d.get("color") or "#3a3a3a"
        if retired:
            color = _mix(color, "#202020", 0.75)
        c.create_rectangle(0, y, nw, y + RH, fill=color, width=0)
        c.create_rectangle(nw, y, W, y + RH, fill=ROW_BG2 if n % 2 else ROW_BG, width=0)
        c.create_line(0, y + RH - 1, W, y + RH - 1, fill="#000000")
        img = self._image(d.get("number_img"), nw - int(6 * s), RH - int(4 * s))
        if img:
            self._live_imgs.append(img)
            c.create_image(nw / 2, y + RH / 2, image=img)
        else:
            c.create_text(nw / 2, y + RH / 2, text=str(d["number"]), fill="#ffffff", font=fz(13))
        st = d["state"]
        cy = y + RH / 2
        pos = d.get("pos_out") if st == "out" else (d.get("pos_now") or d.get("pos_in"))
        c.create_text(nw + 6 * s, cy, text=f"P{pos}" if pos and not retired else "", fill=DIM, font=fz(10, "bold"),
                      anchor="w")
        nid = c.create_text(nw + 36 * s, cy, text=d["last"], fill=DIM if retired else TEXT, font=fz(12), anchor="w")
        g = d.get("gained") if st == "out" else None
        if g:
            bx = c.bbox(nid)
            c.create_text((bx[2] if bx else nw + 120 * s) + 6 * s, cy, text=f"+{g} ▲" if g > 0 else f"{g} ▼",
                          fill=GREEN if g > 0 else RED, font=fz(10), anchor="w")
        if retired:
            right, rcol = "RETIRED", DIM
        elif st == "stall":
            right, rcol = f"{d['code']}  {d['service']:.1f}", YELLOW
        elif st in ("exit", "out"):
            right, rcol = d["code"] + (f"  {d['service']:.1f}s" if d["service"] else ""), TEXT
        else:
            right, rcol = "PIT ROAD", DIM
        c.create_text(W - 8 * s, cy, text=right, fill=rcol, font=fz(10, "bold"), anchor="e")

    def _row(self, c, d, y, W, RH, nw, hs, s, fz, mode, n):
        retired = d["state"] == "retired"
        color = d.get("color") or "#3a3a3a"
        if retired:
            color = _mix(color, "#202020", 0.75)
        # number cell: driver colour, brighter on the left
        steps = 10
        for k in range(steps):
            x0, x1 = nw * k / steps, nw * (k + 1) / steps + 1
            c.create_rectangle(x0, y, x1, y + RH, fill=_mix(_mix(color, "#ffffff", 0.35), color, k / (steps - 1)),
                               width=0)
        # name area
        c.create_rectangle(nw, y, W, y + RH, fill=ROW_BG2 if n % 2 else ROW_BG, width=0)
        c.create_line(0, y + RH - 1, W, y + RH - 1, fill="#000000")
        img = self._image(d.get("number_img"), nw - int(10 * s), RH - int(8 * s))
        if img:
            self._live_imgs.append(img)
            c.create_image(nw / 2, y + RH / 2, image=img)
        else:
            c.create_text(nw / 2 + 1, y + RH / 2 + 1, text=str(d["number"]), fill="#000000", font=fz(22))
            c.create_text(nw / 2, y + RH / 2, text=str(d["number"]), fill="#ffffff", font=fz(22))
        head = self._image(d.get("headshot"), hs, hs, cover=True)
        if head:
            self._live_imgs.append(head)
            c.create_image(nw + 2 + hs / 2, y + RH / 2, image=head)
        name_x = W - 8 * s
        nid = c.create_text(name_x, y + 13 * s, text=d["last"], fill=DIM if retired else TEXT, font=fz(14),
                            anchor="e")
        g = d.get("gained") if d["state"] == "out" else None
        if g:                                        # places gained/lost, just before the name
            bx = c.bbox(nid)
            c.create_text((bx[0] if bx else name_x) - 6 * s, y + 14 * s, text=f"+{g} ▲" if g > 0 else f"{g} ▼",
                          fill=GREEN if g > 0 else RED, font=fz(11), anchor="e")
        y2 = y + 29 * s
        st = d["state"]
        right, rcol, left, lcol = "", TEXT, "", TEXT
        if retired:
            right, rcol = "RETIRED", DIM
        elif st == "stall":
            right, rcol = f"{d['service']:.1f}", YELLOW
            left, lcol = d["label"], DIM
        elif st == "exit":
            right = d["label"]
            left, lcol = (f"{d['service']:.1f}s" if d["service"] else ""), DIM
        elif st == "out":
            right = d["label"]
            if d["service"]:
                left, lcol = f"{d['service']:.1f}s", DIM
        else:
            right, rcol = ("THIS TIME BY" if st == "in" else "PIT ROAD"), (YELLOW if st == "in" else DIM)
        pos = d.get("pos_out") if st == "out" else (d.get("pos_now") or d.get("pos_in"))
        if pos and not retired:
            c.create_text(nw + hs + 8 * s, y2, text=f"P{pos}", fill=DIM, font=fz(10, "bold"), anchor="w")
        rid = c.create_text(name_x, y2, text=right, fill=rcol, font=fz(10, "bold"), anchor="e")
        if left:
            bx = c.bbox(rid)
            c.create_text((bx[0] if bx else name_x) - 6 * s, y2, text=left, fill=lcol, font=fz(10, "bold"),
                          anchor="e")
