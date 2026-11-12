"""The TV-style subtitle box: a slanted number card on the left, a black bar with the speaker's title in small italics
and, under it, exactly what's being said. See-through and click-through, like the number card (radio_overlay.py),
whose window code it uses; shown only for transmissions the SubtitleGate (subtitles.py) lets through - the number
card steps aside while this is up (the card is in it)."""
from __future__ import annotations

import time

from .radio_overlay import (FADE_IN, FADE_OUT, Image, ImageDraw, ImageFont, TalkIndicator, _font, _number_card,
                            u32)

BASE_H = 76                        # px at size 1.0 (the whole box; two lines of text)
MAX_TEXT_W = 700                   # px at size 1.0
MIN_READ_S, READ_PER_CHAR = 2.2, 0.06   # stays up at least this long: a floor, plus time per character
LINGER = 0.9                       # s after the voice stops
SAMPLE = (24, "KYLE LARSON - DRIVER", "Tight in the middle, I need some help on the next stop.")


def _italic(size: int):
    for name in ("arialbi.ttf", "seguisbi.ttf", "segoeuiz.ttf", "bahnschrift.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return _font(size)


def wrap(text: str, width_of, max_w: float) -> list[str]:
    """words into lines no wider than max_w (a single word wider than that stays whole)"""
    lines, cur = [], ""
    for word in str(text).split():
        trial = f"{cur} {word}".strip()
        if cur and width_of(trial) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def fit_text(text: str, size: float, font=_italic) -> tuple[list[str], object]:
    """at most two lines: the text size shrinks a little for a long line, then it's cut with ..."""
    max_w = MAX_TEXT_W * size
    base = max(10, round(25 * size))
    f = font(base)
    for k in range(5):
        f = font(max(9, round(base * (1 - 0.08 * k))))
        lines = wrap(text, f.getlength, max_w)
        if len(lines) <= 2:
            return lines, f
    lines = lines[:2]
    while lines[1] and f.getlength(lines[1] + "...") > max_w:
        lines[1] = lines[1][:-1].rstrip()
    lines[1] += "..."
    return lines, f


def build_bug(number, title: str, text: str, size: float, artdata=None) -> "Image.Image":
    h = max(40, round(BASE_H * size))
    pad = max(6, round(h * 0.15))
    slant = round(h * 0.2)
    card_w = round(h * 1.2)
    f_title = _italic(max(9, round(h * 0.2)))
    lines, f_text = fit_text(text, size)
    text_w = max([f_text.getlength(x) for x in lines] + [f_title.getlength(title)])
    bar_w = max(round(330 * size), round(text_w) + 2 * pad + slant)
    W = card_w + bar_w
    img = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.polygon([(card_w, 0), (W, 0), (W - slant, h), (card_w - slant, h)], fill=(8, 9, 12, 238))   # the bar
    # the number card, cut to a parallelogram
    art = artdata is not None and number is not None and artdata.abs_path(artdata.rel_number(number))
    if art:                                              # the active art's number card
        card = _number_card(number, h, artdata).resize((card_w, h), Image.LANCZOS)
    else:                                                # no art: a plain plate in the car's colour, the number big
        color = (artdata.color(number) if artdata is not None and number is not None else None) or "#26335f"
        card = Image.new("RGBA", (card_w, h), color)
        r, g, b = (int(color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        ink = (0, 0, 0, 255) if 0.299 * r + 0.587 * g + 0.114 * b > 150 else (255, 255, 255, 255)
        ImageDraw.Draw(card).text((card_w / 2 + slant / 4, h / 2), str(number if number is not None else "--"),
                                  font=_italic(round(h * 0.7)), fill=ink, anchor="mm")
    mask = Image.new("L", (card_w, h), 0)
    ImageDraw.Draw(mask).polygon([(slant, 0), (card_w, 0), (card_w - slant, h), (0, h)], fill=255)
    img.paste(card, (0, 0), mask)
    x = card_w + pad
    d.text((x, pad * 0.55), title, font=f_title, fill=(205, 207, 214, 255))
    y = pad * 0.55 + f_title.size * 1.25
    for ln in lines:
        d.text((x, y), ln, font=f_text, fill=(150, 206, 255, 255))
        y += f_text.size * 1.22
    return img


class SubtitleBug(TalkIndicator):
    """Polls the radio's voice for a transmission the gate let through and shows / holds / fades the box."""

    def __init__(self, root, radio, artdata=None, on_moved=None):
        self.root, self.radio, self.artdata, self.on_moved = root, radio, artdata, on_moved
        self.cfg = radio.cfg["subtitles"]
        if self.cfg.get("x") is None or self.cfg.get("y") is None:      # low and left of centre, like on TV
            sw, sh = u32.GetSystemMetrics(0), u32.GetSystemMetrics(1)
            self.cfg["x"], self.cfg["y"] = int(sw * 0.14), int(sh * 0.80)
        self.win = None
        self.img = None
        self.key = None
        self.alpha = 0.0
        self.shown = 0.0
        self.moving = False
        self._drag = None
        self._info = None
        self._hold = 0.0
        self._last_end = 0.0
        if Image is not None:
            self.root.after(600, self._tick)

    def _image_key(self):
        info = self._info or (SAMPLE if self.moving else (None, "", ""))
        return tuple(info) + (round(float(self.cfg.get("size", 1.0)), 2),)

    def _image(self, key):
        number, title, text, size = key
        return build_bug(number, title, text, size, self.artdata)

    def _step(self):
        now = time.monotonic()
        on = bool(self.cfg.get("on")) and bool(self.radio.cfg.get("on"))
        cur = self.radio.voice.current
        peak = max(0.05, min(1.0, float(self.cfg.get("opacity", 0.95))))
        sub = cur.get("sub") if cur else None
        if on and sub:
            info = (cur.get("number"), sub["title"], cur.get("text", ""))
            if info != self._info:
                self._info = info
                self._front()
            read = max(MIN_READ_S, len(info[2]) * READ_PER_CHAR)
            self._hold = max(cur["end"] + LINGER, cur["start"] + read)
        target = peak if (on and self._info is not None and now < self._hold) else 0.0
        if self.moving:
            self._ensure()
            self._push()
            return
        if target == 0.0 and self.alpha == 0.0:
            return
        step = (0.04 / FADE_IN) * peak if target > self.alpha else (0.04 / FADE_OUT) * peak
        self.alpha = min(target, self.alpha + step) if target > self.alpha else max(target, self.alpha - step)
        if self.alpha == 0.0:
            self._info = None
        self._ensure()
        self._push()
