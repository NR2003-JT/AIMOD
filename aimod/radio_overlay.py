"""Who's on the radio: the talking car's number card on a faint plate with radio waves, in a see-through,
click-through, always-on-top window over the game.

Drawn with PIL and shown with UpdateLayeredWindow (real per-pixel transparency, so the card's soft edges and
the faint plate look right; Tk's colour-key transparency would leave hard fringes). It fades in when a car
keys up, slowly gets more see-through while the car talks, and fades out after."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import time
import tkinter as tk

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFont
except ImportError:          # no art without PIL: the indicator just stays off
    Image = None

GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x20, 0x80, 0x08000000
ULW_ALPHA, AC_SRC_ALPHA = 2, 1
BASE_H = 56                       # px at size 1.0
FADE_IN, FADE_OUT, LINGER = 0.25, 0.7, 0.4      # seconds
TALK_FADE = 0.3                   # gets this much more see-through over a long transmission

u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


g32.CreateCompatibleDC.restype = wt.HDC
g32.CreateCompatibleDC.argtypes = [wt.HDC]
g32.CreateDIBSection.restype = wt.HBITMAP
g32.CreateDIBSection.argtypes = [wt.HDC, ctypes.c_void_p, wt.UINT, ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
g32.SelectObject.restype = wt.HGDIOBJ
g32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
g32.DeleteObject.argtypes = [wt.HGDIOBJ]
g32.DeleteDC.argtypes = [wt.HDC]
u32.GetDC.restype = wt.HDC
u32.GetDC.argtypes = [wt.HWND]
u32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
u32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE), wt.HDC,
                                    ctypes.POINTER(wt.POINT), wt.DWORD, ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
u32.GetWindowLongW.restype = ctypes.c_long
u32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
u32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
u32.GetParent.restype = wt.HWND
u32.GetParent.argtypes = [wt.HWND]


def _font(size: int):
    for name in ("bahnschrift.ttf", "arialbd.ttf", "segoeuib.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def build_card(number, size: float, artdata=None) -> "Image.Image":
    """The indicator image (RGBA): faint dark plate, the car's number card (or its number in bold on the
    driver's colour), three radio-wave arcs."""
    h = max(24, round(BASE_H * size))
    pad = max(4, h // 9)
    card_h = h - 2 * pad
    card = None
    if artdata is not None and number is not None:
        path = artdata.abs_path(artdata.rel_number(number))
        if path and os.path.isfile(path):
            try:
                card = Image.open(path).convert("RGBA")
                card = card.resize((max(1, round(card.width * card_h / card.height)), card_h), Image.LANCZOS)
            except Exception:
                card = None
    if card is None:                                   # no art: the number on the driver's colour
        txt = str(number if number is not None else "--")
        f = _font(round(card_h * 0.78))
        w = max(card_h, round(f.getlength(txt)) + 2 * pad)
        card = Image.new("RGBA", (w, card_h), (0, 0, 0, 0))
        d = ImageDraw.Draw(card)
        color = (artdata.color(number) if artdata is not None and number is not None else None) or "#2a2f3a"
        d.rounded_rectangle((0, 0, w - 1, card_h - 1), radius=card_h // 6, fill=color)
        r, g, b = (int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        ink = (0, 0, 0, 255) if 0.299 * r + 0.587 * g + 0.114 * b > 150 else (255, 255, 255, 255)
        d.text((w / 2, card_h / 2), txt, font=f, fill=ink, anchor="mm")
    wave_w = round(h * 0.55)
    W = pad + card.width + pad + wave_w + pad
    img = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, W - 1, h - 1), radius=h // 5, fill=(8, 10, 14, 120))
    img.alpha_composite(card, (pad, pad))
    cx, cy = pad + card.width + pad, h / 2                   # waves coming off the card
    for k in range(3):
        r = h * (0.14 + 0.13 * k)
        d.arc((cx - r, cy - r, cx + r, cy + r), -45, 45, fill=(255, 255, 255, 210 - 50 * k),
              width=max(2, round(h / 18)))
    return img


def _premultiplied_bgra(img) -> bytes:
    r, g, b, a = img.split()
    r, g, b = (ImageChops.multiply(c, a) for c in (r, g, b))
    return Image.merge("RGBA", (b, g, r, a)).tobytes()


class TalkIndicator:
    """Polls the radio's voice for the transmission on air and shows / fades the card."""

    def __init__(self, root, radio, artdata=None, on_moved=None):
        self.root, self.radio, self.artdata, self.on_moved = root, radio, artdata, on_moved
        self.cfg = radio.cfg["indicator"]
        self.win = None
        self.img = None
        self.key = None
        self.alpha = 0.0
        self.shown = 0.0                                   # alpha last pushed to the window
        self.moving = False
        self._drag = None
        self._last_end = 0.0
        self._number = None
        if Image is not None:
            self.root.after(500, self._tick)

    # ------------------------------------------------------------------ window
    def _ensure(self):
        if self.win is not None:
            return
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.geometry(f"1x1+{int(self.cfg['x'])}+{int(self.cfg['y'])}")
        w.bind("<ButtonPress-1>", self._press)
        w.bind("<B1-Motion>", self._move)
        w.bind("<ButtonRelease-1>", self._release)
        w.update_idletasks()
        self.win = w
        self.hwnd = u32.GetParent(w.winfo_id()) or w.winfo_id()
        self._styles()

    def _styles(self):
        st = u32.GetWindowLongW(self.hwnd, GWL_EXSTYLE) | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        st = st & ~WS_EX_TRANSPARENT if self.moving else st | WS_EX_TRANSPARENT
        u32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, st)

    def _front(self):
        if self.win is not None:
            u32.SetWindowPos(self.hwnd, wt.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # TOPMOST, no move/size/activate

    def set_moving(self, on: bool):
        """Move mode: shown at full strength with a sample card; drag it into place."""
        self.moving = on
        self._ensure()
        self._styles()
        self.key = None
        self._push(force=True)

    def refresh(self):
        """settings changed (size / opacity): redraw"""
        self.key = None
        self._push(force=True)

    # ------------------------------------------------------------------ drawing
    def _push(self, force=False):
        if self.win is None or Image is None:
            return
        number = 24 if (self.moving and self._number is None) else self._number
        key = (number, round(float(self.cfg.get("size", 1.0)), 2))
        if key != self.key:
            self.key = key
            self.img = build_card(number, float(self.cfg.get("size", 1.0)), self.artdata)
            self.bgra = _premultiplied_bgra(self.img)
        a = 1.0 if self.moving else self.alpha
        if not force and abs(a - self.shown) < 0.01:
            return
        self.shown = a
        w, h = self.img.size
        screen = u32.GetDC(None)
        mem = g32.CreateCompatibleDC(screen)
        bmi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        hbmp = g32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
        try:
            ctypes.memmove(bits, self.bgra, len(self.bgra))
            old = g32.SelectObject(mem, hbmp)
            blend = BLENDFUNCTION(0, 0, max(0, min(255, round(a * 255))), AC_SRC_ALPHA)
            u32.UpdateLayeredWindow(self.hwnd, screen, ctypes.byref(wt.POINT(int(self.cfg["x"]), int(self.cfg["y"]))),
                                    ctypes.byref(wt.SIZE(w, h)), mem, ctypes.byref(wt.POINT(0, 0)), 0,
                                    ctypes.byref(blend), ULW_ALPHA)
            g32.SelectObject(mem, old)
        finally:
            g32.DeleteObject(hbmp)
            g32.DeleteDC(mem)
            u32.ReleaseDC(None, screen)

    def _tick(self):
        try:
            self._step()
        except Exception:
            pass
        self.root.after(40, self._tick)

    def _step(self):
        now = time.monotonic()
        on = bool(self.cfg.get("on", True)) and bool(self.radio.cfg.get("on"))
        cur = self.radio.voice.current
        peak = max(0.05, min(1.0, float(self.cfg.get("opacity", 0.6))))
        target = 0.0
        if on and cur is not None and cur.get("number") is not None:
            if cur["number"] != self._number or cur["end"] != self._last_end:
                self._number = cur["number"]
                self._front()                  # other overlays are topmost too: come to the front to be seen
            self._last_end = cur["end"]
            talked = now - cur["start"]
            target = peak * (1 - TALK_FADE * min(1.0, talked / 4.0))      # a little more see-through as it goes
        elif on and now - self._last_end < LINGER and self._number is not None:
            target = self.alpha                                           # hold a moment after the last word
        if self.moving:
            self._ensure()
            self._push()
            return
        if target == 0.0 and self.alpha == 0.0:
            return
        step = (0.04 / FADE_IN) * peak if target > self.alpha else (0.04 / FADE_OUT) * peak
        self.alpha = min(target, self.alpha + step) if target > self.alpha else max(target, self.alpha - step)
        self._ensure()
        self._push()

    # ------------------------------------------------------------------ move mode
    def _press(self, e):
        if self.moving:
            self._drag = (e.x_root - int(self.cfg["x"]), e.y_root - int(self.cfg["y"]))

    def _move(self, e):
        if self._drag:
            self.cfg["x"], self.cfg["y"] = e.x_root - self._drag[0], e.y_root - self._drag[1]
            self._push(force=True)

    def _release(self, e):
        if self._drag:
            self._drag = None
            if self.on_moved:
                self.on_moved()

    def close(self):
        if self.win is not None:
            self.win.destroy()
            self.win = None
