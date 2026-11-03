"""The driver's radio from inside the helmet: his voice with his own engine roaring underneath.

Engine sound: the real in-car engine loops from the user's NR2003 install (sound\\winston_low/med/high.wav,
the cup car's cockpit samples - read at run time, never shipped with AIMOD). Picked and pitched by the car's
speed. If the files aren't there, a synthetic V8 drone (firing harmonics + rumble) stands in.

Then the helmet: the engine is heard through the boom mic inside a helmet, so it's muffled (low-passed around
1 kHz, a bit of body resonance), loud but under the voice, and the voice gets the close-mic press (a
compressor that pumps against the engine). The radio's own band / squelch (radio.radio_fx) is added after.
Plain Python: AIMOD's build has no numpy.
"""
from __future__ import annotations

import array
import math
import os
import random
import wave

RATE = 22050
FILES = (("winston_low.wav", 25.0), ("winston_med.wav", 45.0), ("winston_high.wav", 65.0))   # (file, m/s it fits)
_cache: dict = {}


def _load(path: str) -> list[float] | None:
    try:
        with wave.open(path, "rb") as w:
            ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
    except (OSError, wave.Error, EOFError):
        return None
    if width == 2:
        data = array.array("h")
        data.frombytes(raw[: len(raw) // 2 * 2])
        scale = 32768.0
    elif width == 1:
        data = array.array("B", raw)
        data = array.array("h", ((b - 128) * 256 for b in data))
        scale = 32768.0
    else:
        return None
    mono = [sum(data[i:i + ch]) / (ch * scale) for i in range(0, len(data) - ch + 1, ch)]
    if rate != RATE and mono:                               # linear resample to the radio's rate
        step = rate / RATE
        mono = [mono[min(len(mono) - 1, int(i * step))] for i in range(int(len(mono) / step))]
    return mono or None


def engine_samples(nr2003_dir: str) -> list[tuple[list[float], float]]:
    key = nr2003_dir or ""
    if key not in _cache:
        out = []
        for name, speed in FILES:
            s = _load(os.path.join(nr2003_dir, "sound", name)) if nr2003_dir else None
            if s and len(s) > RATE // 4:
                out.append((s, speed))
        _cache[key] = out
    return _cache[key]


def _synthetic(n: int, speed: float, rnd: random.Random) -> list[float]:
    """A V8 drone when the game's samples aren't there: 4 firing pulses per crank turn at ~speed-based rpm."""
    rpm = 3500 + max(0.0, min(1.0, speed / 80.0)) * 5500
    f = rpm / 60 * 4                                        # V8: 4 firings per revolution
    out, ph = [], 0.0
    for i in range(n):
        ph += f / RATE
        x = (0.6 * math.sin(2 * math.pi * ph) + 0.3 * math.sin(4 * math.pi * ph + 0.7)
             + 0.15 * math.sin(math.pi * ph) + 0.08 * rnd.uniform(-1, 1))
        out.append(x * 0.6)
    return out


def engine_bed(n: int, speed: float | None, nr2003_dir: str = "", seed: int = 0) -> list[float]:
    """n samples of engine noise for a car going `speed` m/s (None = race pace)."""
    rnd = random.Random(seed)
    speed = 55.0 if speed is None else max(5.0, speed)
    samples = engine_samples(nr2003_dir)
    if not samples:
        return _synthetic(n, speed, rnd)
    loop, ref = min(samples, key=lambda s: abs(s[1] - speed))
    pitch = max(0.8, min(1.25, speed / ref))                # rpm follows speed within the sample's band
    pos = rnd.uniform(0, len(loop))
    out = []
    L = len(loop)
    for _ in range(n):
        i = int(pos) % L
        frac = pos - int(pos)
        out.append(loop[i] * (1 - frac) + loop[(i + 1) % L] * frac)
        pos += pitch
    return out


def _onepole_lp(xs: list[float], fc: float) -> list[float]:
    a = math.exp(-2 * math.pi * fc / RATE)
    y, out = 0.0, []
    for x in xs:
        y = (1 - a) * x + a * y
        out.append(y)
    return out


def helmet_mix(voice: array.array, speed: float | None, nr2003_dir: str = "", engine_level: float = 0.55,
               seed: int = 0) -> array.array:
    """The driver's line as heard on his radio: voice up front, muffled engine under it."""
    v = [s / 32768.0 for s in voice]
    pad = int(0.25 * RATE)                                  # the engine is there before and after he talks
    n = len(v) + 2 * pad
    eng = engine_bed(n, speed, nr2003_dir, seed)
    eng = _onepole_lp(_onepole_lp(eng, 1100.0), 1400.0)     # inside the helmet: muffled
    peak = max(1e-6, max(abs(x) for x in eng))
    eng = [x / peak for x in eng]
    vpeak = max(1e-6, max(abs(x) for x in v)) if v else 1.0
    out = array.array("h")
    env = 0.0
    for i in range(n):
        x = v[i - pad] / vpeak if pad <= i < pad + len(v) else 0.0
        env = max(abs(x), env * 0.9993)                     # voice envelope: the engine ducks a little under it
        e = eng[i] * engine_level * (1.0 - 0.35 * min(1.0, env * 3))
        y = math.tanh(1.6 * (0.8 * x + e)) / math.tanh(1.6)  # the close mic pressed by a loud cockpit
        out.append(max(-32768, min(32767, int(y * 30000))))
    return out
