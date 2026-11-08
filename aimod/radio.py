"""Team radio: crew chief and spotter calls for one car, from the data AIMOD already tracks.

The car is yours (the player slot), or any car number while you spectate (for testing).

What it says:
- spotter (flags):  caution, pit road open / closed, one to go, green, white flag
- crew chief:       laps to go and stage ends; position reports; closing on / being caught (gaps measured at
                    the line from NR2003's scoring table); fuel left, the pit window, short / good to the end
                    (your car's real fuel and mileage); tires going away (lap times on this run); your pit
                    stops (time in the box, what was changed); a call when pit road opens under caution; the
                    leader and the cars around you pitting; free pass, wave around, black flag, damage, laps down

Voices: Piper (natural neural voices, offline) when its voice pack is in AIMOD's "voices" folder -
voices\\piper\\piper.exe plus a model such as en_US-libritts_r-medium.onnx (904 speakers, so every crew chief
on the scanner can sound different) - else Windows' own text-to-speech (SAPI, through comtypes). Each line is
rendered to memory, given a radio sound (narrow band, a little grit, a squelch at the end) and played. Everything runs on a worker thread, so a slow
voice never holds up AIMOD's tick. Recorded clips can replace the voice later.
"""
from __future__ import annotations

import array
import io
import logging
import math
import os
import random
import tempfile
import threading
import time
import wave
from collections import deque

from . import carno
from . import memory_map as mm
from .strategy import FOUR_TIRES, LEFT_SIDE, RIGHT_SIDE, STAGE_BREAK

FUEL_SPOKEN = {1: "a splash of fuel", 2: "half a can of fuel", 3: "one can of fuel", 4: "a can and a half of fuel",
               5: "full fuel"}
STAY_OUT_WAIT = 6.0      # an AI car's "stay out" must hold this long (s) before it's said: plans can still flip

log = logging.getLogger("aimod.radio")

SPOTTER, CREW, DRIVER = "spotter", "crew", "driver"
URGENT, HIGH, NORMAL, LOW = 0, 1, 2, 3
FLAG_ONE_TO_GO = 5

CATEGORIES = [
    ("alongside", "Car inside / outside / clear (spotter)", "Cars alongside, three wide, still there, clear - for "
                                                            "the radio car (on the scanner: the TV car). Off for "
                                                            "your own car unless ticked below: NR2003's spotter "
                                                            "already calls those."),
    ("flags", "Flags and pit road (spotter)", "Caution, pit road open / closed, one to go, green, white flag."),
    ("countdown", "Laps to go and stage ends", "50 / 25 / 10 / 5 / 2 to go, five to go in the stage, end of stage "
                                               "position."),
    ("position", "Position reports", "Your position and the gaps ahead and behind every few green laps, and spots "
                                     "gained or lost at the line."),
    ("gaps", "Closing / being caught", "Gaining on the car ahead, or the car behind gaining on you, in tenths a "
                                       "lap (measured at the line)."),
    ("fuel", "Fuel", "Laps of fuel left, the pit window, short on fuel or good to the end (your car's real fuel "
                     "and mileage)."),
    ("tires", "Tires and pace", "Tires going away (lap times on this run against its best), fastest car on "
                                "track."),
    ("pits", "Your stops and caution calls", "Time in the box and what was changed after your stops; pit or "
                                             "stay out when pit road opens under caution."),
    ("others", "Leader and nearby cars pitting", "The leader or the car just ahead / behind going to pit road, "
                                                 "and what the leader took."),
    ("status", "Free pass, wave around, black flag, damage", "Also going a lap down or getting back on the lead "
                                                             "lap."),
    ("team", "Your teammates' plans", "Your crew chief passes on what your AI teammates (Teams window) are doing: "
                                      "when each plans to pit and when that changes, who pits or stays out under "
                                      "caution, and a teammate's pace against yours now and then."),
    ("behavior", "Driver behavior calls", "With AIMOD's driver behavior on: the crew chief tells an AI driver to save "
                                          "fuel, take care of his tires, cool them off, ride in the draft or go - and "
                                          "why (laps short, a battle for the lead, the end of the run, late in the "
                                          "race); the driver answers, and calls it first when his tires go away."),
    ("driver", "AI drivers talking", "The AI driver of the car you listen to (on the scanner: the TV car and your "
                                     "favourites) shouts when his tires are gone, he passes or gets passed, takes "
                                     "the lead or wins."),
]

DEFAULTS = {"on": False, "car": "player", "volume": 80, "rate": 1, "voice_crew": "David", "voice_spotter": "David",
            "report_laps": 15, "radio_fx": True,
            "mode": "car",            # car = one car's radio | scanner = every car's radio, one transmission at a time
            "scan": "all",            # scanner: all | teams (every team car) | team (scan_team) | numbers (scan_numbers)
            "scan_team": "", "scan_numbers": "", "favourites": "", "follow_tv": True,
            "engine": "auto",         # auto (Piper if its voice pack is there, else Windows) | piper | windows
            "piper_model": "en_US-libritts_r-medium.onnx",
            "crew_voice": "Piper 600", "spotter_voice": "Piper 615",   # "Piper <speaker>" or "Windows <name>"
            "driver_voice": "Piper 21",                                # the driver (one car / the TV car)
            "driver_pool": "21, 92",                                   # the other drivers on the scanner
            "driver_swears": False,   # the driver's lines with cursing
            "custom_lines": {},       # the user's own driver lines: kind -> [lines] (said as written, never filtered)
            "custom_only": False,     # only the user's lines for a kind that has some
            "piper_pool": "332, 523, 217, 31, 496, 410",             # scanner: crew chiefs of the other cars
            "piper_speed": 0.92,      # Piper length scale: under 1 = quicker
            "alongside_mine": False,  # alongside calls for your own car too (NR2003's spotter already does them)
            "phone_only": False,      # while a phone plays the scanner, the PC stays quiet
            "crew_scan": True}        # cars run from a phone (crew chief beta) are always on the radio

# Piper voices picked for the radio (libritts_r speakers: deep, quick, firm - from a scan of all 904)
VOICE_CHOICES = [(600, "deep, quick"), (615, "deep, steady"), (332, "deep, firmest"), (523, "firm, quick"),
                 (217, "deep, quick"), (31, "deep"), (496, "firm"), (410, "firm, a touch slower")]
# the drivers: stressed but natural-sounding (user's picks from a scan for raised, quick, hard-edged voices)
DRIVER_CHOICES = [(21, "stressed"), (92, "stressed")]
# drivers who always get their own voice, whatever number they run (hard-wired, not a choice)
NAMED_VOICES = [("larson", 670)]
WINDOWS_VOICES = ["David", "Zira"]


def voice_label(n: int) -> str:
    return next((f"Piper {s} - {d}" for s, d in VOICE_CHOICES + DRIVER_CHOICES if s == n), f"Piper {n}")


def parse_voice(text, default):
    """'Piper 600 - deep' / '600' -> 600 (a Piper speaker); 'Windows David' -> 'windows:David'"""
    t = str(text or "").strip()
    if t.lower().startswith("windows"):
        return "windows:" + (t[7:].strip() or "David")
    digits = "".join(ch for ch in t.split("-")[0] if ch.isdigit())
    return int(digits) if digits and int(digits) < 904 else default

# alongside: another car overlapping us within this many metres along the track, and this far to the side
ALONG_M, SIDE_MIN_M, SIDE_MAX_M = 5.0, 0.9, 4.3
SIDE_ON_TICKS, SIDE_OFF_TICKS = 2, 3          # ticks (~0.15 s) a side must hold before it's called / cleared
STILL_THERE_S = 3.0
TURN_LEARNED = 0.6                            # radians of turning seen before inside / outside is known
INDICATOR_DEFAULTS = {"on": True, "x": 60, "y": 300, "opacity": 0.6, "size": 1.0}
MAX_WAITING = 4               # transmissions waiting to be aired; more = the least important ones are dropped
TEAM_PACE_LAPS = 25          # green laps between "the 5 is two tenths quicker than you" reports
PHONE_GONE_S = 4.0            # a phone that hasn't asked for scanner calls this long has stopped listening

# the TV camera's car (the game's camera object): a pointer to that car's record
CAM_FOCUS_PTR, CAR_REC0, CAR_REC_SIZE = 0xC069E3, 0xC12030, 5560


def tv_slot(pm) -> int | None:
    """car slot the game's TV camera is on, or None"""
    try:
        off = pm.read_uint(CAM_FOCUS_PTR) - CAR_REC0
    except Exception:
        return None
    return off // CAR_REC_SIZE if 0 <= off < CAR_REC_SIZE * mm.NUM_CARS and off % CAR_REC_SIZE == 0 else None


def parse_numbers(text) -> set[int]:
    return {int(t) for t in str(text or "").replace(",", " ").replace("#", " ").split() if t.isdigit()}


def parse_cars(text) -> set[str]:
    """typed car numbers ("24, 01, #00") as painted labels, so "01" finds the car NR2003 stores as 2001"""
    out = set()
    for t in str(text or "").replace(",", " ").replace("#", " ").split():
        if t.isdigit():
            out.add(carno.label(carno.raw(t)))
    return out


def car_in(number, labels: set[str]) -> bool:
    return number is not None and carno.label(number) in labels


def team_label(team: dict, k: int) -> str:
    return team.get("name") or f"Team {k + 1}"


def voice_tweak(number) -> tuple[int, int]:
    """(pitch, rate) offsets so each crew chief on the scanner sounds a little different (same every race)"""
    n = int(number or 0) * 2654435761 % 2 ** 32
    return n % 7 - 3, (n >> 8) % 3 - 1

SAMPLE_RATE = 22050
SAFT22K16MONO = 22


# ----------------------------------------------------------------------------------------------- voice
class _Msg:
    __slots__ = ("text", "role", "prio", "born", "ttl", "key", "number", "tweak", "speaker")

    def __init__(self, text, role, prio, ttl, key, number=None, tweak=(0, 0), speaker=None):
        self.text, self.role, self.prio, self.born, self.ttl, self.key = text, role, prio, time.monotonic(), ttl, key
        self.number, self.tweak, self.speaker = number, tweak, speaker


class Voice(threading.Thread):
    """Speaks queued lines one at a time, most urgent first; an urgent line cuts off a less urgent one."""

    def __init__(self, cfg: dict, voices_dir: str = ""):
        super().__init__(daemon=True, name="aimod-radio")
        self.cfg = cfg
        self.voices_dir = voices_dir
        self.pipers: dict = {}               # Piper processes (model loaded once), calm and driver delivery
        self.piper_args: dict = {}
        self.engine = "windows"
        self.q: list[_Msg] = []
        self.cv = threading.Condition()
        self.cut = threading.Event()
        self.quit = False
        self.playing_prio = 99
        self.ok: bool | None = None          # None until the speech engine has been tried
        self.voices: list[str] = []
        self._file = 0
        self.current: dict | None = None     # the transmission on air: {"number", "role", "start", "end"} (indicator)
        self.speeds: dict[int, float] = {}   # car number -> speed (m/s): the driver's engine under his voice
        self.nr2003_dir = ""                 # for the game's in-car engine samples (helmet.py)
        # a phone playing the scanner (through the local web server): the last few calls as wav files
        self.clips: deque = deque(maxlen=8)
        self.clip_no = 0
        self.clip_lock = threading.Lock()
        self.phone_at = -1e9                 # when a phone last asked for calls (monotonic)

    # ---- the phone (web server thread)
    def phone_listening(self) -> bool:
        return time.monotonic() - self.phone_at < PHONE_GONE_S

    def clips_info(self, listen: bool) -> dict:
        now = time.monotonic()
        if listen:
            self.phone_at = now
        with self.clip_lock:
            clips = [{"id": c["id"], "number": None if c["number"] is None else str(c["number"]), "role": c["role"], "secs": round(c["secs"], 2),
                      "age": round(now - c["start"], 2), "cut": c["cut"]} for c in self.clips]
        return {"on": bool(self.cfg.get("on")), "ok": self.ok is not False,
                "phone_only": bool(self.cfg.get("phone_only")), "clips": clips}

    def clip_wav(self, no: int) -> bytes | None:
        with self.clip_lock:
            return next((c["wav"] for c in self.clips if c["id"] == no), None)

    # called from AIMOD's thread
    def say(self, text: str, role: str, prio: int, ttl: float, key: str | None, number=None, tweak=(0, 0),
            speaker=None):
        with self.cv:
            if key:
                self.q = [m for m in self.q if m.key != key]
            self.q.append(_Msg(text, role, prio, ttl, key, number, tweak, speaker))
            while len(self.q) > MAX_WAITING:                # the scanner can't air everything: drop the least
                self.q.remove(max(self.q, key=lambda m: (m.prio, -m.born)))
            if prio == URGENT and self.playing_prio > URGENT:
                self.cut.set()
            self.cv.notify()

    def stop(self):
        with self.cv:
            self.quit = True
            self.cut.set()
            self.cv.notify()
        self._piper_stop()

    # ---- Piper
    def piper_files(self) -> tuple[str, str] | None:
        exe = os.path.join(self.voices_dir, "piper", "piper.exe")
        model = os.path.join(self.voices_dir, str(self.cfg.get("piper_model", "")))
        return (exe, model) if os.path.isfile(exe) and os.path.isfile(model) else None

    def _piper_stop(self, kind: str | None = None):
        for k in [kind] if kind else list(self.pipers):
            p = self.pipers.pop(k, None)
            if p is not None:
                try:
                    p.stdin.close()
                    p.wait(2)
                except Exception:
                    p.kill()

    def _piper_render(self, text: str, speaker, role: str = CREW) -> array.array:
        import json
        import subprocess
        files = self.piper_files()
        if files is None:
            raise RuntimeError("Piper voice pack not found")
        if role == DRIVER:
            kind, flags = "driver", DRIVER_PIPER
        else:
            kind, flags = "calm", {"length_scale": max(0.6, min(1.5, float(self.cfg.get("piper_speed", 0.92))))}
        args = (files, tuple(sorted(flags.items())))
        p = self.pipers.get(kind)
        if p is None or p.poll() is not None or args != self.piper_args.get(kind):
            self._piper_stop(kind)
            cmd = [files[0], "-m", files[1], "--json-input"]
            for k, v in flags.items():
                cmd += [f"--{k}", f"{v:.2f}"]
            p = self.pipers[kind] = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                     stderr=subprocess.DEVNULL, cwd=os.path.dirname(files[0]),
                                                     creationflags=0x08000000)  # no console
            self.piper_args[kind] = args
        self._file ^= 1
        out = os.path.join(tempfile.gettempdir(), f"aimod_piper_{os.getpid()}_{self._file}.wav")
        line = {"text": text, "output_file": out}
        if speaker is not None:
            line["speaker_id"] = int(speaker)
        p.stdin.write((json.dumps(line) + "\n").encode())
        p.stdin.flush()
        if not p.stdout.readline():                     # Piper prints the file name when it's written
            raise RuntimeError("Piper stopped")
        with wave.open(out) as w:
            pcm = array.array("h")
            pcm.frombytes(w.readframes(w.getnframes()))
            rate = w.getframerate()
        if rate != SAMPLE_RATE:                                  # the radio chain runs at 22.05 kHz
            pcm = array.array("h", (pcm[int(i * rate / SAMPLE_RATE)] for i in range(int(len(pcm) * SAMPLE_RATE / rate))))
        return pcm

    # worker thread
    def _next(self) -> _Msg | None:
        with self.cv:
            while not self.quit:
                now = time.monotonic()
                self.q = [m for m in self.q if now - m.born <= m.ttl]          # stale news is no news
                if self.q:
                    m = min(self.q, key=lambda m: (m.prio, m.born))
                    self.q.remove(m)
                    return m
                self.cv.wait(0.5)
        return None

    def run(self):
        try:
            import comtypes
            import comtypes.client as cc
            comtypes.CoInitialize()
            self.sapi = cc.CreateObject("SAPI.SpVoice", dynamic=True)
            tokens = self.sapi.GetVoices()
            self.tokens = [tokens.Item(i) for i in range(tokens.Count)]
            self.voices = [t.GetDescription() for t in self.tokens]
            self.cc = cc
            self.ok = True
            log.info("Radio: voices %s", ", ".join(self.voices) or "none")
        except Exception as e:
            self.ok = False
            log.warning("Radio: no Windows speech (%s) - radio lines are only logged", e)
        want = self.cfg.get("engine", "auto")
        if want != "windows" and self.piper_files() is not None:
            self.engine, self.ok = "piper", True
            log.info("Radio: Piper voices (%s)", self.cfg.get("piper_model"))
        elif want == "piper":
            log.warning("Radio: Piper voice pack not found in %s - using Windows' voice", self.voices_dir)
        while True:
            m = self._next()
            if m is None:
                break
            if not self.ok:
                continue
            try:
                self.playing_prio = m.prio
                self.cut.clear()
                pcm = None
                if (isinstance(m.speaker, int) and self.cfg.get("engine", "auto") != "windows"
                        and self.piper_files() is not None):
                    try:
                        pcm = self._piper_render(m.text, m.speaker, m.role)
                    except Exception as e:
                        log.warning("Radio: Piper failed (%s) - Windows' voice for this line", e)
                        self._piper_stop()
                if pcm is None:
                    if not getattr(self, "sapi", None):
                        raise RuntimeError("no Windows speech")
                    name = m.speaker[8:] if isinstance(m.speaker, str) and m.speaker.startswith("windows:") else None
                    pcm = self._render(m.text, m.role, m.tweak, name)
                if m.role == DRIVER and self.cfg.get("helmet", True):
                    try:                              # from inside his helmet: muffled engine under the voice
                        from .helmet import helmet_mix
                        pcm = helmet_mix(pcm, self.speeds.get(m.number), self.nr2003_dir,
                                         engine_level=max(0.0, min(1.0, int(self.cfg.get("helmet_engine", 55)) / 100)),
                                         seed=len(m.text))
                    except Exception as e:
                        log.warning("Radio: helmet sound failed (%s)", e)
                self._play(pcm, m)
            except Exception as e:
                log.warning("Radio: could not speak %r: %s", m.text, e)
            finally:
                self.playing_prio = 99
            time.sleep(0.35)                                          # a breath between calls

    def _render(self, text: str, role: str, tweak=(0, 0), name: str | None = None) -> array.array:
        want = name or self.cfg.get("voice_spotter" if role == SPOTTER else "voice_crew", "") or "David"
        for desc, tok in zip(self.voices, self.tokens):
            if want and want.lower() in desc.lower():
                self.sapi.Voice = tok
                break
        stream = self.cc.CreateObject("SAPI.SpMemoryStream", dynamic=True)
        fmt = self.cc.CreateObject("SAPI.SpAudioFormat", dynamic=True)
        fmt.Type = SAFT22K16MONO
        stream.Format = fmt
        self.sapi.AudioOutputStream = stream
        rate = int(self.cfg.get("rate", 1)) + (1 if role == SPOTTER else 0)     # the spotter talks a bit quicker
        pitch = 2 if role == SPOTTER else 0 if role == DRIVER else -2           # ...and a bit higher
        pitch, rate = pitch + tweak[0], rate + tweak[1]
        safe = text.replace("&", "and").replace("<", " ").replace(">", " ")
        self.sapi.Speak(f'<pitch absmiddle="{pitch}"><rate absspeed="{rate}">{safe}</rate></pitch>', 8)  # 8 = XML
        pcm = array.array("h")
        pcm.frombytes(bytes(stream.GetData()))
        return pcm

    def _play(self, pcm: array.array, m: _Msg | None = None):
        import winsound
        vol = max(0.0, min(1.5, int(self.cfg.get("volume", 80)) / 100))
        out = radio_fx(pcm, vol) if self.cfg.get("radio_fx", True) else array.array(
            "h", (max(-32768, min(32767, int(s * vol))) for s in pcm))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(out.tobytes())
        data = buf.getvalue()
        phone = self.phone_listening()
        quiet = phone and self.cfg.get("phone_only")         # phone only: the PC stays quiet (no echo in the room)
        if not quiet:
            self._file ^= 1                                 # two files in turn: the last one may still be open
            path = os.path.join(tempfile.gettempdir(), f"aimod_radio_{os.getpid()}_{self._file}.wav")
            with open(path, "wb") as f:
                f.write(data)
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        start = time.monotonic()
        end = start + len(out) / SAMPLE_RATE
        clip = None
        if phone:
            with self.clip_lock:
                self.clip_no += 1
                clip = {"id": self.clip_no, "wav": data, "number": m.number if m else None,
                        "role": m.role if m else CREW, "start": start, "secs": end - start, "cut": False}
                self.clips.append(clip)
        self.current = {"number": m.number if m else None, "role": m.role if m else CREW, "start": start, "end": end}
        try:
            while time.monotonic() < end:
                if self.cut.wait(0.05):
                    if not quiet:
                        winsound.PlaySound(None, winsound.SND_PURGE)
                    if clip is not None:
                        clip["cut"] = True                  # the phone stops it too
                    break
        finally:
            self.current = None


def radio_fx(pcm: array.array, vol: float) -> array.array:
    """Two-way radio sound: a click, the voice through a narrow band (about 350 Hz to 3 kHz) with a little
    grit and hiss, then the squelch tail. Plain Python (AIMOD's build has no numpy)."""
    rnd = random.Random(len(pcm))

    def biquad(xs, b0, b1, b2, a1, a2):
        out, x1 = [], 0.0
        x2 = y1 = y2 = 0.0
        for x in xs:
            y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2, x1, y2, y1 = x1, x, y1, y
            out.append(y)
        return out

    def coeffs(kind, f0, q=0.707):
        w = 2 * math.pi * f0 / SAMPLE_RATE
        alpha, c = math.sin(w) / (2 * q), math.cos(w)
        if kind == "hp":
            b0, b1, b2 = (1 + c) / 2, -(1 + c), (1 + c) / 2
        else:
            b0, b1, b2 = (1 - c) / 2, 1 - c, (1 - c) / 2
        a0 = 1 + alpha
        return b0 / a0, b1 / a0, b2 / a0, -2 * c / a0, (1 - alpha) / a0

    click = [rnd.uniform(-0.5, 0.5) * math.exp(-i / 40) for i in range(int(0.012 * SAMPLE_RATE))]
    gap = [0.0] * int(0.04 * SAMPLE_RATE)
    voice = [s / 32768.0 for s in pcm]
    tail_n = int(0.16 * SAMPLE_RATE)
    tail = [rnd.uniform(-0.35, 0.35) * (1 - i / tail_n) for i in range(tail_n)]
    xs = click + gap + voice + tail
    xs = biquad(xs, *coeffs("hp", 350))
    xs = biquad(xs, *coeffs("lp", 3000))
    peak = max(1e-6, max(abs(x) for x in xs))
    out = array.array("h")
    for x in xs:
        y = math.tanh(2.2 * x / peak) / math.tanh(2.2) + rnd.uniform(-0.012, 0.012)
        out.append(max(-32768, min(32767, int(y * 0.85 * vol * 32767))))
    return out


# ------------------------------------------------------------------------------------------ wording
def tenths(sec: float) -> str:
    """0.3 -> '3 tenths', 0.1 -> 'a tenth', 1.24 -> '1.2'"""
    n = round(sec * 10)
    if sec < 0.95:
        return "a tenth" if n <= 1 else f"{n} tenths"
    return f"{sec:.1f}"


def secs(sec: float) -> str:
    return f"{sec:.1f}"


def pick(*options: str) -> str:
    return random.choice(options)


# The driver on the radio is hanging onto a car at nearly 200 mph: short, shouted, stressed. Clean / cursing.
DRIVER_LINES = {
    "tight": (("It's tight! Won't turn in the center at all!", "Pushing up the track, I can't get it to turn!",
               "Tight, tight! It just won't turn!", "It's plowing! Front end's got nothing!"),
              ("This damn thing won't turn! It's tight as hell!", "Pushing like crazy, it's a damn dump truck!",
               "Tight! I can't get the damn thing to turn, I'm all over the wall!",
               "Front end's shit, it won't turn!")),
    "loose": (("Loose! Loose off! I can't put the power down!", "Rear's coming around on me off the corner!",
               "It's loose in, loose off, I'm just hanging on!", "Real loose! I'm about to spin it!"),
              ("Loose as hell! I can't put the damn power down!", "Shit, the rear's coming around on me every lap!",
               "This thing's trying to kill me off the corner!", "Damn it, it's loose! I'm about to wreck it!")),
    "tires": (("Tires are gone! I've got nothing left!", "I'm sliding everywhere, the tires are done!"),
              ("Tires are shot, I've got nothing!", "Hell, I'm sliding all over, these tires are junk!")),
    "damage": (("I got into the wall! The right front's hurt!", "I hit something! It's not right!",
                "Got into the fence! Something's broke!"),
               ("Shit! Got into the wall, the right front's hurt!", "Damn it, I hit the wall! It's not right!",
                "Son of a bitch, I'm in the fence! Something's broke!")),
    "pass": (("Got him! Got him!", "That's one more!", "Made it stick!", "There we go, got by him!"),
             ("Got that son of a bitch!", "Hell yeah, got him!", "Get the hell out of my way! Got him!")),
    "passed": (("He got me, he got me.", "Couldn't hold him off.", "He's by me, damn it."),
               ("Son of a bitch got me!", "Damn it, he just drove right by me!", "Shit, couldn't hold him!")),
    "lead": (("We're leading! Let's go!", "Out front, baby!", "Got the lead! Let's keep it!"),
             ("Hell yeah, we're leading this damn thing!", "Out front! Let's go, boys!")),
    "ack_save": (("Ten-four.", "Copy, I'll save it.", "Alright, saving.", "Okay, I got it."),
                 ("Ten-four.", "Yeah, yeah, saving.", "Copy. Hate it, but copy.")),
    "ack_push": (("Let's go!", "Copy, going!", "Here we go!", "Alright, hang on!"),
                 ("Hell yeah, let's go!", "About damn time!", "Copy, let's go get 'em!")),
    "win": (("We won! We won this thing! Thank you guys!", "Victory lane, baby! What a car!",
             "Yes! Yes! We did it!"),
            ("We won this damn thing! Hell yeah!", "Victory lane, baby! Hell of a car!")),
}
# Every radio moment a user can add their own lines to (Scanner tab > Custom lines): key, who says it, the moment,
# and the {placeholders} its lines may use ({car} = the car's number and {pos} = its position work everywhere).
TRIGGERS = [
    ("caution", "Spotter", "Caution comes out", ""),
    ("pits_closed", "Spotter", "Pit road closed", ""),
    ("pits_open", "Spotter", "Pit road open", ""),
    ("one_to_go", "Spotter", "One to go", ""),
    ("green", "Spotter", "Green flag", ""),
    ("white", "Spotter", "White flag", ""),
    ("laps_to_go", "Crew chief", "Laps to go (50, 25, 10, 5, 2)", "{laps}"),
    ("stage_end", "Crew chief", "End of a stage", "{stage}"),
    ("checkered", "Crew chief", "Checkered flag", ""),
    ("pit_call", "Crew chief", "Calls the car in under caution / box this lap", ""),
    ("stay_out", "Crew chief", "Stays out under caution", ""),
    ("stop_done", "Crew chief", "After the car's stop", ""),
    ("fuel_low", "Crew chief", "Fuel getting low (10, 5, 2 laps)", "{laps}"),
    ("fuel_ok", "Crew chief", "Good on fuel to the end", ""),
    ("fuel_short", "Crew chief", "Short on fuel for the finish", "{laps}"),
    ("tires_going", "Crew chief", "Tires going away", ""),
    ("gained", "Crew chief", "Gained a position", ""),
    ("lost", "Crew chief", "Lost a position", ""),
    ("catching", "Crew chief", "Catching the car ahead", "{other}"),
    ("caught", "Crew chief", "Car behind closing", "{other}"),
    ("fastest", "Crew chief", "Fastest car on track", ""),
    ("lap_down", "Crew chief", "Went a lap down", ""),
    ("lead_lap", "Crew chief", "Back on the lead lap", ""),
    ("free_pass", "Crew chief", "Free pass", ""),
    ("wave", "Crew chief", "Wave around", ""),
    ("black_flag", "Crew chief", "Black flag", ""),
    ("damage", "Crew chief", "After contact / damage", ""),
    ("behave_fuel", "Crew chief", "Behavior: start saving fuel (short of the finish / stage)", "{laps}"),
    ("behave_tires", "Crew chief", "Behavior: take care of the tires (long run / tires going away)", "{laps}"),
    ("behave_cool", "Crew chief", "Behavior: stuck in dirty air, back off and cool the tires", "{other}"),
    ("behave_push", "Crew chief", "Behavior: go (end of the run, a run after cooling off)", "{other} {laps}"),
    ("behave_battle", "Crew chief", "Behavior: battling for the lead / top 3", "{other}"),
    ("behave_lapping", "Crew chief", "Behavior: the leader is about to lap us", "{other}"),
    ("behave_late", "Crew chief", "Behavior: late in the race, everything you've got", "{laps}"),
    ("behave_draft", "Crew chief", "Behavior: ride / go with the pack (drafting tracks)", "{other} {laps}"),
    ("behave_done", "Crew chief", "Behavior: done saving, back to racing", ""),
    ("driver_ack_save", "Driver (AI cars only)", "Answers a save call", ""),
    ("driver_ack_push", "Driver (AI cars only)", "Answers a go call", ""),
    ("driver_tight", "Driver (AI cars only)", "Car is tight", ""),
    ("driver_loose", "Driver (AI cars only)", "Car is loose", ""),
    ("driver_tires", "Driver (AI cars only)", "Tires are gone", ""),
    ("driver_damage", "Driver (AI cars only)", "Hit something", ""),
    ("driver_pass", "Driver (AI cars only)", "Passes a car", ""),
    ("driver_passed", "Driver (AI cars only)", "Gets passed", ""),
    ("driver_lead", "Driver (AI cars only)", "Takes the lead", ""),
    ("driver_win", "Driver (AI cars only)", "Wins the race", ""),
]


# The crew chief's behavior calls (driver behavior on): one entry per reason key from behavior.py (Switch.code).
# {laps} / {other} are filled in; (trigger, category priority) per key below.
BEHAVIOR_LINES = {
    "fuel_finish": ("We're about {laps} laps short to the finish. Start saving fuel, lift early and short shift.",
                    "Gotta save fuel now, we're {laps} short. Feather it, roll the corners.",
                    "Fuel's tight, about {laps} short. Save every drop you can."),
    "fuel_stage": ("Save some fuel, we're about {laps} short of making the stage.",
                   "We're stretching it to the stage. Lift early, save fuel."),
    "late": ("{laps} to go. No more saving, give it everything you've got.",
             "Time to go. Run it hard to the end, use it all up.",
             "{laps} to go. Empty the tank, leave nothing out there."),
    "lead_attack": ("You're right on the {other} for the lead. Go get him!",
                    "There's the leader. Go race him!"),
    "lead_defend": ("The {other}'s right on you for the lead. Hit your marks, hold him off.",
                    "Leader, but the {other}'s all over you. Run your line, don't give it up."),
    "top3_attack": ("You're on the {other}'s bumper. Go get that spot.",
                    "{other} right in front of you. Go race him."),
    "top3_defend": ("The {other}'s right behind you. Hold him off.",
                    "Got the {other} on your bumper. Protect that spot."),
    "lapping": ("Leader's right behind us. Run hard, keep us on the lead lap.",
                "Here comes the leader. Don't let him lap us, go!"),
    "cool": ("You're stuck in his air. Back off a few car lengths and let the tires cool down.",
             "Drop back off the {other} a bit, cool the tires off, then we'll make a run."),
    "run": ("Tires are cooled off. Go get the {other} now.",
            "Okay, that's enough. Go run him down."),
    "tires_gone": ("Copy. Take care of 'em, save what you can till we pit.",
                   "Ten-four, ease up on 'em. Save the tires for the end of the run."),
    "manage": ("Long run. Take care of your tires through the middle, we'll go at the end.",
               "Manage your tires, don't burn 'em up yet. Long way to go on this run."),
    "attack": ("We're short-pitting this run. Go hard now, we'll get you fresh ones early.",
               "Push it while they're fresh. We're coming in early this run."),
    "end_run": ("{laps} laps left in this run. Use up what you saved.",
                "End of the run. Go, use those tires up."),
    "ride": ("Ride in line and save fuel. We'll go at the end.",
             "Settle in and save fuel. No need to lead right now."),
    "stage_push": ("{laps} to go in the stage. Time to go!",
                   "Stage is almost over. Go get some points!"),
    "draft_end": ("End of the run. Go to the front.",
                  "Time to go. Work the draft, get up there."),
    "follow_save": ("Stay with the {other}, he's saving too.",
                    "Hook up with the {other} and ride. Save fuel."),
    "follow_go": ("Go with the {other}, we're going now!",
                  "Stay hooked to the {other}, here we go!"),
    "done_fuel": ("That's enough saving, we're good on fuel. Go race.",
                  "Fuel's good now. Run your pace."),
    "done_tires": ("Okay, run your pace again.",
                   "That's enough, go ahead and run."),
}
BEHAVIOR_TRIGGERS = {"fuel_finish": "behave_fuel", "fuel_stage": "behave_fuel", "manage": "behave_tires",
                     "tires_gone": "behave_tires", "cool": "behave_cool", "run": "behave_push", "end_run": "behave_push", "attack": "behave_push",
                     "lead_attack": "behave_battle", "lead_defend": "behave_battle", "top3_attack": "behave_battle",
                     "top3_defend": "behave_battle", "lapping": "behave_lapping", "late": "behave_late",
                     "ride": "behave_draft", "stage_push": "behave_draft", "draft_end": "behave_draft",
                     "follow_save": "behave_draft", "follow_go": "behave_draft", "done_fuel": "behave_done",
                     "done_tires": "behave_done"}
BEHAVIOR_URGENT = {"lead_attack", "lead_defend", "lapping", "late", "fuel_finish"}   # said whatever the spacing

# Rivals (racing intensity): (crew lines, driver lines) per event, for the car the event is about. {other} = the
# other car's number, {level} = the feud level. "retaliate_target" / "feud" / "settled" are also said to the other car.
RIVAL_LINES = {
    "grudge": (("That was the {other} that got into you. Remember that number.",
                "The {other} roughed you up there. Don't let him do it again."),
               ("He did that on purpose. I won't forget it.", "Okay. I've got a long memory.")),
    "feud": (("That's {level} times now with the {other}. Don't give him an inch.",
              "The {other} again. Race him hard, he's earned it."),
             ("Same guy again. I'm done being nice to him.",)),
    "wrecked": (("The {other} wrecked us. Bring it in, let's see what we can fix.",
                 "That's the {other}'s doing. Get it to pit road, we'll fix what we can."),
                ()),
    "retaliate": (("Car's not right after what the {other} did. Do what you gotta do.",
                   "We can't fix all of it. The {other} is out there, you know what he did."),
                  ("He wrecked me. He's gonna get it back.", "I'm gonna find the {other}.")),
    "retaliate_target": (("Watch the {other}. He's not happy with you, he's coming.",
                          "Heads up, the {other} is out for you."),
                         ()),
    "settled": (("That's enough with the {other}. Let it go, we've got a race to run.",
                 "Okay, that one's settled. Focus on your own race now."),
                ()),
}


class _Blank(dict):
    def __missing__(self, key):
        return ""


def custom_text(cfg: dict, trig: str | None, built_in: str, vals: dict) -> str:
    """the built-in line, or one of the user's own lines for this moment ({placeholders} filled in)"""
    mine = [t.strip() for t in (cfg.get("custom_lines") or {}).get(trig or "", []) if t and t.strip()]
    if not mine:
        return built_in
    choice = pick(*mine) if cfg.get("custom_only") else pick(built_in, *mine)
    if choice is built_in:
        return built_in
    try:
        return choice.format_map(_Blank({k: v for k, v in vals.items() if v is not None}))
    except (ValueError, IndexError):
        return choice                                   # a stray brace: say it as written


# Piper for the driver: quicker and with more pitch / timing variation than the calm crew chief and spotter
DRIVER_PIPER = {"length_scale": 0.84, "noise_scale": 0.9, "noise_w": 1.1}


def first_person(text: str) -> str:
    """the crew's account of the driver ("He's tight in the center") as the driver says it ("I'm tight ...")"""
    import re
    subs = [(r"\bHe's\b", "I'm"), (r"\bhe's\b", "I'm"), (r"\bHe is\b", "I am"), (r"\bhe is\b", "I am"),
            (r"\bHe was\b", "I was"), (r"\bhe was\b", "I was"), (r"\bHe\b", "I"), (r"\bhe\b", "I"),
            (r"\bhimself\b", "myself"), (r"\bhim\b", "me"), (r"\bhis\b", "my"), (r"\bHis\b", "My")]
    for a, b in subs:
        text = re.sub(a, b, text)
    # verbs after "I" ("I can't" is fine; "I wants" -> "I want")
    return re.sub(r"\bI (\w+?)s\b", lambda m: "I " + m.group(1) if m.group(1) not in ("i", "ha", "wa", "doe")
                  else m.group(0), text)


def service_words(left: bool, right: bool) -> str:
    if left and right:
        return "four tires"
    if right:
        return "two right sides"
    if left:
        return "two left sides"
    return "fuel only"


# ----------------------------------------------------------------------------------------------- brain
class Radio:
    """The radio you listen to: one car's team radio, or a scanner across many cars' radios that airs one
    transmission at a time, like a race scanner (it favours urgent calls, the car on TV, your favourites
    and the front of the field)."""

    def __init__(self, cfg: dict, on_line=None, voices_dir: str = ""):
        for k, v in DEFAULTS.items():
            cfg.setdefault(k, v)
        cats = cfg.setdefault("cats", {})
        for k, _label, _tip in CATEGORIES:
            cats.setdefault(k, True)
        if cfg.get("driver_voice") == "Piper 523":
            cfg["driver_voice"] = DEFAULTS["driver_voice"]          # the first default, before the user's picks
        ind = cfg.setdefault("indicator", {})
        for k, v in INDICATOR_DEFAULTS.items():
            ind.setdefault(k, v)
        self.cfg = cfg
        self.on_line = on_line                 # UI callback(text) for each line said
        self.voice = Voice(cfg, voices_dir)
        self.lines: deque[str] = deque(maxlen=300)
        self.by_car: dict[str, deque] = {}     # car number (as painted) -> its last lines (crew chief on a phone)
        self._started = False
        self.session = None
        self.teams = lambda: []                # AIMOD's teams for the loaded mod: [{"name", "cars"}] (set by the app)
        self.reset()

    def start(self):
        if not self._started:
            self._started = True
            self.voice.start()

    def stop(self):
        if self._started:
            self.voice.stop()

    def reset(self):
        self.cars: dict[int, CarRadio] = {}
        self.cross: dict[int, dict[int, int]] = {}     # slot -> {laps done: game ms it crossed the line}
        self.target_idx = None                          # one-car mode: the car listened to
        self.focus_slot = None                          # the car whose spotter calls the flags
        self.tv_locked = False                          # scanner tuned to the car the TV camera is showing
        self.pos: dict[int, int] = {}
        self.crew_slots: set[int] = set()               # cars a phone is running now (crew chief beta)

    @property
    def scanner(self) -> bool:
        return self.cfg.get("mode") == "scanner"

    # ----------------------------------------------------------------------- output
    def say(self, cat, role, text, prio=NORMAL, ttl=12, key=None, car: "CarRadio | None" = None):
        if not self.cfg.get("on") or (cat and not self.cfg["cats"].get(cat, True)):
            return
        number = car.number if car else None
        tweak = (0, 0)
        if car is not None and (self.scanner or car.slot != self.focus_slot):
            # (one-car mode: a car a phone runs is the only other car on the radio, scanned like a favourite)
            focus = car.slot == self.focus_slot
            fav = car_in(number, parse_cars(self.cfg.get("favourites", ""))) or car.slot in self.crew_slots
            pos = self.pos.get(car.slot, 99)
            if cat in ("flags", "countdown", "others") and not focus:
                return                                   # one spotter calls the flags, not forty
            if cat == "position" and not (focus or fav or pos <= 3):
                return
            if focus:
                # following the TV car: the scanner is tuned to its channel, everything it says goes out first
                prio = max(URGENT, prio - (2 if self.tv_locked else 1))
            elif self.tv_locked:
                if prio > (NORMAL if fav else HIGH):
                    return                               # tuned to the TV car: others only break in when it matters
            elif fav:
                prio = max(URGENT, prio - 1)
            elif pos > 10 and prio >= NORMAL and random.random() > 0.3:
                return                                   # the scanner was on another channel
            ttl = min(ttl, 8.0)
            key = f"{number}:{key}" if key else None
            tweak = voice_tweak(number)
        who = {SPOTTER: "SPOTTER", DRIVER: "DRIVER"}.get(role, "CREW") + (
            f" #{number}" if number is not None and (self.scanner or car.slot != self.focus_slot) else "")
        line = f"{time.strftime('%H:%M:%S')}  {who}: {text}"
        self.lines.append(line)
        if number is not None:
            self.by_car.setdefault(str(number), deque(maxlen=8)).append(line)
        log.info("RADIO %s: %s", who.lower(), text)
        if self.on_line:
            try:
                self.on_line(line)
            except Exception:
                pass
        self.start()
        self.voice.say(text, role, prio, ttl, key, number, tweak, self.speaker(role, car))

    def speaker(self, role, car):
        """The voice: a Piper speaker number, or 'windows:<name>'. The spotter's for spotter calls, the crew chief's
        for yours (or the one car's / the TV car's); on the scanner the other cars' crew chiefs come from the
        pool, the same voice for a car every race."""
        if role == SPOTTER:
            return parse_voice(self.cfg.get("spotter_voice"), 615)
        if role == DRIVER:
            named = NAMED_VOICES
            who = (getattr(car, "name", "") or "").lower()
            for name, spk in named:                            # e.g. Larson always sounds like Larson
                if who and name in who:
                    return spk
            drv = parse_voice(self.cfg.get("driver_voice"), 21)
            if car is None or car.slot == self.focus_slot or not isinstance(drv, int):
                return drv
            reserved = {spk for _n, spk in named}
            pool = sorted(n for n in parse_numbers(self.cfg.get("driver_pool", "")) if n < 904 and n not in reserved)
            return (pool or [drv])[int(car.number or 0) * 2654435761 % 2 ** 32 % len(pool or [drv])]
        crew = parse_voice(self.cfg.get("crew_voice"), 600)
        if car is None or car.slot == self.focus_slot or not isinstance(crew, int):
            return crew
        pool = sorted(n for n in parse_numbers(self.cfg.get("piper_pool", "")) if n < 904) or [crew]
        return pool[int(car.number or 0) * 2654435761 % 2 ** 32 % len(pool)]

    def test(self):
        self.say(None, SPOTTER, "Radio check. Clear high, clear low.", HIGH)
        self.say(None, CREW, "Loud and clear. P 8, 1.2 to the 24 ahead. Fuel's good to the end.", NORMAL)

    # ----------------------------------------------------------------------- input
    def target(self, snap):
        want = str(self.cfg.get("car", "player")).strip().lstrip("#")
        for c in snap.cars:
            if not c.valid:
                continue
            if want in ("", "player"):
                if not c.controlled:
                    return c
            elif str(c.number) == want:
                return c
        return None

    def scan_targets(self, snap) -> list:
        how = self.cfg.get("scan", "all")
        nums = parse_cars(self.cfg.get("scan_numbers", ""))
        if how == "team":                                        # one team, by its name in the Teams window
            want = self.cfg.get("scan_team", "")
            nums = next(({carno.label(n) for n in t["cars"]} for k, t in enumerate(self.teams() or [])
                         if team_label(t, k) == want), set())
            how = "numbers"
        out = []
        for c in snap.cars:
            if not c.valid or c.reason_out:
                continue
            if how == "teams" and c.team < 0:
                continue
            if how == "numbers" and not car_in(c.number, nums):
                continue
            out.append(c)
        return out

    def update(self, snap, res, engine, p, tv_slot: int | None = None):
        """Once per AIMOD tick, after the strategy engine has run. tv_slot = the car the TV camera is on."""
        if not self.cfg.get("on"):
            return
        if snap.session != self.session:
            self.reset()
            self.session = snap.session
        if snap.session != mm.SESSION_RACE:
            return
        self.voice.speeds = {c.number: c.speed for c in snap.cars if c.valid and c.speed is not None}
        for c in snap.cars:                                    # line crossings, for gaps measured at the line
            if c.valid and c.cross_ms and c.laps_done > 0:
                d = self.cross.setdefault(c.idx, {})
                d[c.laps_done] = c.cross_ms
                for old in [k for k in d if k < c.laps_done - 6]:
                    del d[old]
        rows = {r.idx: r for r in res.rows}
        self.pos = {i: r.pos for i, r in rows.items() if r.pos}
        if self.scanner:
            targets = self.scan_targets(snap)
            follow = bool(self.cfg.get("follow_tv", True))
            tv_car = next((c for c in snap.cars if c.idx == tv_slot and c.valid and not c.reason_out), None)
            if follow and tv_car is not None and tv_car not in targets:
                targets.append(tv_car)                   # the car on screen is always scanned
            ids = {c.idx for c in targets}
            focus, why = None, ""
            if follow and tv_car is not None:
                focus, why = tv_car.idx, "the TV car"
            if focus is None:
                focus = next((c.idx for c in targets if not c.controlled), None)        # your car
                why = "your car"
            if focus is None:
                focus = min(ids, key=lambda i: self.pos.get(i, 99), default=None)      # the leader
                why = "the leader"
            self.tv_locked = focus is not None and why == "the TV car"
            if focus != self.focus_slot and focus is not None:
                num = next((c.number for c in targets if c.idx == focus), "?")
                log.info("Scanner: tuned to #%s (%s)", num, why)
            self.focus_slot = focus
            self.target_idx = None
        else:
            car = self.target(snap)
            targets = [car] if car is not None else []
            self.target_idx = self.focus_slot = car.idx if car is not None else None
        # cars run from a phone: always on the radio, so their crew chief hears them (only phones that are awake)
        self.crew_slots = set(getattr(engine, "human_owner", None) or {}) if self.cfg.get("crew_scan", True) else set()
        for c in snap.cars:
            if c.idx in self.crew_slots and c.valid and not c.reason_out and c not in targets:
                targets.append(c)
        keep = {c.idx for c in targets}
        for idx in [i for i in self.cars if i not in keep]:
            del self.cars[idx]
        for c in targets:
            cr = self.cars.get(c.idx)
            if cr is None or cr.number != c.number:
                cr = self.cars[c.idx] = CarRadio(self, c.idx, c.number)
            cr.update(snap, res, engine, p, c, rows)
            if c.idx == self.focus_slot and (c.controlled or self.cfg.get("alongside_mine")):
                cr.alongside(snap, c)


class CarRadio:
    """One car's team radio: its crew chief (and its spotter's flag calls)."""

    def __init__(self, mgr: Radio, slot: int, number: int):
        self.mgr, self.slot, self.number = mgr, slot, number
        self.ai = False                                      # an AI car (set each tick)
        self.name = ""
        self.cfg = mgr.cfg
        self.cross = mgr.cross
        self.reset()

    def say(self, cat, role, text, prio=NORMAL, ttl=12, key=None, trig=None, **vals):
        if trig:
            vals.setdefault("car", self.number)
            vals.setdefault("pos", self.my_pos)
            text = custom_text(self.cfg, trig, text, vals)
        self.mgr.say(cat, role, text, prio, ttl, key, self)

    def reset(self):
        self.flag = None
        self.pits_open = None
        self.pits_closed = False
        self.my_laps = None
        self.my_pos = None
        self.lap_dirty = True
        self.fuel_mark: tuple[int, float] | None = None
        self.fuel_rates: list[float] = []
        self.run_laps: list[float] = []
        self.run_said: set[str] = set()
        self.gaps_ahead: list[tuple[int, int, float]] = []    # (lap, slot ahead, gap)
        self.gaps_behind: list[tuple[int, int, float]] = []
        self.behind_lap_seen = -1
        self.gap_said: dict[str, int] = {}                   # what -> lap it was last said
        self.green_laps_since_report = 0
        self.fastest_said = -99
        self.countdown_said: set = set()
        self.stage_said: set = set()
        self.white_said = self.checker_said = False
        self.status = {}
        self.dmg_ref = None
        self.dmg_said = 0
        self.pit = None                                      # own stop in progress
        self.after_stop = False
        self.last_stop_laps = 0
        self.caution_no = 0
        self.caution_call = -1
        self.stay_since = None
        self.stay_said = -1
        self.pit_said = -1
        self.caution_pitting_said = -1
        self.leader_service_said = -1
        self.wave_said = -1
        self.adjust_ev = None                                # stop with a chassis adjustment, until the box
        self.down = None                                     # laps down, judged when this car crosses the line
        self.mate_plan: dict[int, tuple] = {}                # teammate slot -> (race lap or STAGE, lap it was said)
        self.mate_call: dict[int, int] = {}                  # teammate slot -> caution its call was passed on
        self.mate_stay: dict[int, float] = {}                # teammate slot -> when its stay-out decision began
        self.mate_first: float | None = None                 # this caution: when the first teammate decided
        self.mate_said_caution = -1                          # this caution's teammate summary has been said
        self.mate_green = 0                                  # clean green laps in a row (for pace comparisons)
        self.mate_pace_lap = 0
        self.mate_pace_turn = 0
        self.side = [False, False]                         # car alongside: inside, outside (as called)
        self.side_ticks = [0, 0]
        self.side_said_at = 0.0
        self.turn = 0.0                                      # heading change seen: which way is inside
        self.prev_head = None
        self.behave_lap = -99                                # car lap of the last behavior call (spacing)

    def update(self, snap, res, engine, p, car, rows):
        self.name = car.name or self.name
        self.ai = car.controlled
        me = rows.get(car.idx)
        green = snap.flag == mm.FLAG_GREEN
        total = res.laps_total or snap.laps_total
        if self.down is None or (self.my_laps is not None and car.laps_done > self.my_laps):
            # laps down as scoring has it: the leader's completed laps when this car crosses the line. AIMOD's
            # running gap to the leader rounds down, and flips back and forth while a car runs almost exactly a
            # whole number of laps behind (it spammed "5 down / got one back" on the radio)
            leader = max((c.laps_done for c in snap.cars if c.valid and not c.reason_out), default=car.laps_done)
            self.down = max(0, leader - car.laps_done)
        self._flags(snap, engine, total, green)
        self._status(car, me, p)
        if car.controlled:
            self._behavior(car, engine, green)
            self._rivals(car, engine)
        self._own_stop(car, me, engine, snap)
        self._others(res, me, car, green)
        if car.controlled:
            self._plan_call(car, me, engine, snap, res)     # an AI car: AIMOD's own plan
        else:
            self._caution_call(car, me, engine, snap, total)
        self._countdown(engine, total, green)
        if not green or car.on_pit_road:
            self.lap_dirty = True
        if self.my_laps is not None and car.laps_done > self.my_laps:
            self._at_line(car, me, rows, engine, total, green, snap)
        if not car.controlled and car.team >= 0:
            self._teammates(car, engine, snap, rows, green,
                            self.my_laps is not None and car.laps_done > self.my_laps, res)
        self.my_laps = car.laps_done
        self._caught(car, me, rows, green)
        self.flag = snap.flag

    # ----------------------------------------------------------------------- spotter: flags and pit road
    def _flags(self, snap, engine, total, green):
        prev = self.flag
        if prev is None:
            self.pits_open = engine.pits_open_at
            return
        started = engine.race_started
        if prev == mm.FLAG_GREEN and not green and started:
            self.caution_no += 1
            self.say("flags", SPOTTER, pick("Caution, caution, caution. Slow it down.",
                                            "Caution's out, caution's out. Slow down."), URGENT, 8, "flag", trig="caution")
        if not green and snap.pits_closed and not self.pits_closed and started:
            self.say("flags", SPOTTER, "Pit road is closed. Stay out.", HIGH, 10, "pitroad", trig="pits_closed")
        self.pits_closed = snap.pits_closed
        if not green and engine.pits_open_at is not None and self.pits_open is None and started:
            self.say("flags", SPOTTER, "Pit road is open. Pit road is open.", HIGH, 10, "pitroad", trig="pits_open")
        self.pits_open = engine.pits_open_at
        if snap.flag == FLAG_ONE_TO_GO and prev != FLAG_ONE_TO_GO:
            self.say("flags", SPOTTER, "One to go. One to go.", HIGH, 10, "flag", trig="one_to_go")
        if green and prev != mm.FLAG_GREEN:
            self.say("flags", SPOTTER, "Green, green, green!", URGENT, 6, "flag", trig="green")
        if green and total and engine.lap_now == total and not self.white_said and started:
            self.white_said = True
            self.say("flags", SPOTTER, "White flag. White flag. Bring it home.", HIGH, 8, "flag", trig="white")

    def _countdown(self, engine, total, green):
        lap = engine.lap_now
        if not green or not total or not engine.race_started:
            return
        to_go = total - lap + 1
        if to_go in (50, 25, 10, 5, 2) and to_go not in self.countdown_said and total > to_go + 5:
            self.countdown_said.add(to_go)
            self.say("countdown", CREW, f"{to_go} to go." if to_go > 2 else "Two to go. Everything you got.",
                     NORMAL, 15, trig="laps_to_go", laps=to_go)
        end = engine.next_stage_end()
        if end is not None:
            left = end - lap + 1
            n = engine.stage_no(end)
            if left == 5 and (end, 5) not in self.stage_said:
                self.stage_said.add((end, 5))
                self.say("countdown", CREW, f"Five to go in stage {n}.", NORMAL, 15)
            elif left == 1 and (end, 1) not in self.stage_said:
                self.stage_said.add((end, 1))
                self.say("countdown", CREW, f"Last lap of stage {n}.", NORMAL, 10)

    # ----------------------------------------------------------------------- spotter: alongside / clear
    def alongside(self, snap, car):
        """Inside / outside / three wide / still there / clear, from the cars' world positions: each other car's
        offset along this car's direction of travel and to its side. Which side is inside is learned from the
        way this car turns (ovals turn one way), so it doesn't depend on the game's axis conventions."""
        green = snap.flag == mm.FLAG_GREEN
        if (not green or car.pos is None or car.vel is None or car.on_pit_road or (car.speed or 0) < 15
                or not self.cfg["cats"].get("alongside", True)):
            self.side, self.side_ticks, self.prev_head = [False, False], [0, 0], None
            return
        sp = math.hypot(*car.vel)
        hx, hy = car.vel[0] / sp, car.vel[1] / sp
        if self.prev_head is not None:
            self.turn += self.prev_head[0] * hy - self.prev_head[1] * hx      # + = turning toward +side
        self.prev_head = (hx, hy)
        if abs(self.turn) < TURN_LEARNED:
            return                                             # don't know which side is inside yet
        inside_sign = 1 if self.turn > 0 else -1
        raw = [False, False]
        for o in snap.cars:
            if not o.valid or o.idx == car.idx or o.pos is None or o.on_pit_road or o.reason_out:
                continue
            dx, dy = o.pos[0] - car.pos[0], o.pos[1] - car.pos[1]
            along, lat = dx * hx + dy * hy, hx * dy - hy * dx
            if abs(along) < ALONG_M and SIDE_MIN_M < abs(lat) < SIDE_MAX_M:
                raw[0 if lat * inside_sign > 0 else 1] = True
        new = list(self.side)
        for k in (0, 1):                                       # a side must hold a moment to be called / cleared
            if raw[k] != self.side[k]:
                self.side_ticks[k] += 1
                if self.side_ticks[k] >= (SIDE_ON_TICKS if raw[k] else SIDE_OFF_TICKS):
                    new[k], self.side_ticks[k] = raw[k], 0
            else:
                self.side_ticks[k] = 0
        now = time.monotonic()
        old, self.side = self.side, new
        text = None
        if new != old:
            if new == [True, True]:
                text = "Three wide, you're in the middle!"
            elif new == [True, False]:
                text = "Clear outside, still inside." if old == [True, True] else "Car inside."
            elif new == [False, True]:
                text = "Clear inside, still outside." if old == [True, True] else "Car outside."
            else:
                text = pick("Clear.", "Clear, clear.", "You're clear.")
        elif any(new) and now - self.side_said_at >= STILL_THERE_S:
            text = "Still there." if new != [True, True] else "Still three wide."
        if text:
            self.side_said_at = now
            self.say("alongside", SPOTTER, text, URGENT, 1.5, "side")

    # ----------------------------------------------------------------------- crew chief: status
    def _status(self, car, me, p):
        if me is None:
            return
        now = {"free_pass": me.free_pass, "waved": me.waved, "black_flag": me.black_flag, "down": self.down or 0}
        was = self.status
        self.status = now
        if not was:
            self.dmg_ref = car.damage
            return
        if now["free_pass"] and not was["free_pass"]:
            self.say("status", CREW, "You're the free pass! Stay out, you get your lap back.", HIGH, 20, trig="free_pass")
        if now["waved"] and not was["waved"] and self.wave_said != self.caution_no:
            self.wave_said = self.caution_no                  # said once a caution (the stay-out call may have)
            self.say("status", CREW, "We're getting the wave around. Stay out, stay out.", HIGH, 20, trig="wave")
        if now["black_flag"] and not was["black_flag"]:
            self.say("status", CREW, "Black flag. You've got to come in and serve it.", URGENT, 30, trig="black_flag")
        if now["down"] > was["down"]:
            self.say("status", CREW, "We just went a lap down." if now["down"] == 1
                     else f"We're {now['down']} laps down now.", NORMAL, 20, trig="lap_down")
        elif now["down"] < was["down"]:
            self.say("status", CREW, "We're back on the lead lap!" if now["down"] == 0
                     else f"Got one back, {now['down']} down.", NORMAL, 20,
                     trig="lead_lap" if now["down"] == 0 else None)
        if self.dmg_ref is None:
            self.dmg_ref = car.damage
        hit = car.damage - self.dmg_ref
        if hit > 0.15 and self.dmg_said < 1 and car.controlled:     # the driver first (not you: you're driving)
            self.say("status", DRIVER, self.driver_line("damage"), URGENT, 15)
        if hit > p.damage_threshold and self.dmg_said < 2:
            self.dmg_said = 2
            self.say("status", CREW, "That's a lot of damage. Bring it to pit road.", URGENT, 20, trig="damage")
        elif hit > 0.15 and self.dmg_said < 1:
            self.dmg_said = 1
            self.say("status", CREW, "Looks like you got some damage. How's it driving?", HIGH, 20, trig="damage")

    def _behavior(self, car, engine, green):
        """The crew chief calls the car's behavior switch (driver behavior): why it saves, cools or goes. The
        driver answers (or, when his tires go, calls it first). One call every two laps at most, unless it is
        urgent (a lead battle, the leader lapping us, fuel short, late in the race)."""
        for sw in getattr(engine, "behavior_switches", ()) or ():
            if sw.idx != self.slot:
                continue
            key = sw.code
            if sw.new == "":                                     # back to normal: only when a save ends under green
                if not green or car.on_pit_road or sw.old not in ("SAVE FUEL", "SAVE TIRES"):
                    continue
                key = "done_fuel" if sw.old == "SAVE FUEL" else "done_tires"
            elif key == "follow":
                key = "follow_go" if sw.new == "PUSH" else "follow_save"
            if key not in BEHAVIOR_LINES:
                continue
            urgent = key in BEHAVIOR_URGENT
            if not urgent and car.laps_done - self.behave_lap < 2:
                continue
            self.behave_lap = car.laps_done
            vals = {"laps": max(1, int(round(sw.value))) if sw.value is not None else "",
                    "other": sw.other if sw.other is not None else ""}
            text = pick(*BEHAVIOR_LINES[key]).format(**vals).replace("the  ", "the ").strip()
            prio = HIGH if urgent else (LOW if key.startswith("follow") else NORMAL)
            if key == "tires_gone":
                self.say("behavior", DRIVER, self.driver_line("tires"), HIGH, 12, "behave_drv")
            self.say("behavior", CREW, text, prio, 15, "behave", trig=BEHAVIOR_TRIGGERS[key], **vals)
            if key != "tires_gone" and self.chatty() and random.random() < 0.5:
                ack = "ack_push" if sw.new == "PUSH" else "ack_save" if sw.new in ("SAVE FUEL", "SAVE TIRES",
                                                                                     "COOL TIRES") else None
                if ack:
                    self.say("behavior", DRIVER, self.driver_line(ack), prio, 10, "behave_ack")

    def _rivals(self, car, engine):
        """Racing intensity & rivals: the crew (and the driver) on a grudge, a feud, a wreck, retaliation or a
        settled feud of this AI car. Only AI cars: the player's driver never speaks."""
        for idx, e in getattr(engine, "rival_events", ()) or ():
            if idx != self.slot:
                continue
            mine = int(e.holder) == int(car.number)
            key = e.kind if mine else {"retaliate": "retaliate_target", "feud": "feud",
                                       "settled": "settled"}.get(e.kind)
            if key not in RIVAL_LINES:
                continue
            crew, driver = RIVAL_LINES[key]
            other = e.other if mine else e.holder
            vals = {"other": carno.label(carno.CarNo(int(other))), "level": e.level}
            self.say("behavior", CREW, pick(*crew).format(**vals), HIGH, 15, "rival")
            if driver and mine:
                self.say("behavior", DRIVER, pick(*driver).format(**vals), NORMAL, 12, "rival_drv")

    def driver_line(self, kind: str) -> str:
        built_in = pick(*DRIVER_LINES[kind][1 if self.cfg.get("driver_swears") else 0])
        return custom_text(self.cfg, "driver_" + kind, built_in, {"car": self.number, "pos": self.my_pos})

    def chatty(self) -> bool:
        """an AI driver who talks beyond his car's problems: the car you listen to, or on the scanner the TV car,
        your favourites and cars run from a phone (forty drivers shouting would drown the scanner)"""
        if not self.ai:
            return False                                      # you're the driver of your own car
        if not self.mgr.scanner:
            return True
        return (self.slot == self.mgr.focus_slot or self.slot in self.mgr.crew_slots
                or car_in(self.number, parse_cars(self.cfg.get("favourites", ""))))

    def chatter(self, kind: str, prio=NORMAL, chance=1.0):
        if self.chatty() and random.random() < chance:
            self.say("driver", DRIVER, self.driver_line(kind), prio, 10, "chatter")

    def _driver_adjust_line(self, adj) -> str:
        """the driver's complaint before an adjustment stop: stressed lines by what's wrong with the car, else
        the crew chief's account of it turned into his own words"""
        diag = str(getattr(adj, "diagnosis", "") or "").lower()
        for kind in ("tight", "loose"):
            if kind in diag:
                return self.driver_line(kind)
        if "fall" in diag or "tire" in diag:
            return self.driver_line("tires")
        text = getattr(adj, "driver", "") or ""
        return first_person(text) if text else ""

    # ----------------------------------------------------------------------- crew chief: your own stops
    def _own_stop(self, car, me, engine, snap):
        on_road = bool(car.on_pit_road)
        wl = (car.wear_lf + car.wear_lr) / 2
        wr = (car.wear_rf + car.wear_rr) / 2
        if on_road and self.pit is None:
            self.pit = {"pos": me.pos if me else 0, "wl": wl, "wr": wr, "fuel": car.fuel, "still": None,
                        "box": 0.0, "green": snap.flag == mm.FLAG_GREEN}
        if self.pit is None:
            return
        if on_road:
            stopped = (car.speed or 0) < 0.5
            if stopped and self.pit["still"] is None:
                self.pit["still"] = engine.now
                adj = getattr(self.adjust_ev, "adjust", None)
                if adj is not None and getattr(adj, "crew", ""):
                    self.say("pits", CREW, adj.crew, HIGH, 15)
                self.adjust_ev = None
            elif not stopped and self.pit["still"] is not None:
                self.pit["box"] += max(0.0, engine.now - self.pit["still"])
                self.pit["still"] = None
            return
        stop, self.pit = self.pit, None                       # off pit road again
        if stop["box"] < 2.0:
            return                                            # drove through
        self.last_stop_laps = car.laps_done
        self.run_laps, self.run_said = [], set()
        self.dmg_ref, self.dmg_said = car.damage, 0
        self.after_stop = True
        left = wl < stop["wl"] - 0.02
        right = wr < stop["wr"] - 0.02
        box = stop["box"]
        verdict = "Great stop" if box < 13 else "Good stop" if box < 16 else "Okay stop" if box < 20 else "Slow one"
        text = f"{verdict}, {secs(box)} in the box. {service_words(left, right).capitalize()}."
        rate = self._fuel_rate(engine)
        if car.fuel is not None and rate:
            text += f" Fuel's good for about {int(car.fuel / rate)} laps."
        self.say("pits", CREW, text, NORMAL, 20, trig="stop_done")

    def _plan_call(self, car, me, engine, snap, res):
        """An AI car (AIMOD runs its strategy): say AIMOD's own call. Pitting = the stop it just started, with the
        service AIMOD picked (tires, fuel amount, repairs); staying out = its decision, once it has held a few
        seconds (a follow call or a top-off can still change it)."""
        green = snap.flag == mm.FLAG_GREEN
        ev = next((e for e in res.events if e.idx == car.idx), None)
        if ev is not None:
            if green or self.pit_said != self.caution_no:
                if not green:
                    self.pit_said = self.caution_no
                adj = getattr(ev, "adjust", None)
                line = self._driver_adjust_line(adj) if adj is not None else ""
                if line:
                    self.say("pits", DRIVER, line, HIGH, 20)                       # queued first: said first
                # a stop with an adjustment is the news; with 30 cars pitting under caution, routine
                # "four tires" calls give way to it (the TV car / your favourites are raised anyway)
                self.say("pits", CREW, self._stop_words(ev, green, self.stay_said == self.caution_no),
                         HIGH if line or not self.mgr.scanner else NORMAL, 20, "plan", trig="pit_call")
            self.adjust_ev = ev                               # the crew's adjustment line comes in the box
            return
        if (green or engine.pits_open_at is None or not engine.race_started or me is None or car.on_pit_road
                or self.stay_said == self.caution_no or self.pit_said == self.caution_no):
            self.stay_since = None
            return
        st = getattr(engine, "cars", {}).get(car.idx)
        if st is None or st.decision is not False or st.event is not None:
            self.stay_since = None
            return
        if self.stay_since is None:
            self.stay_since = time.monotonic()
        if time.monotonic() - self.stay_since >= STAY_OUT_WAIT or snap.flag >= FLAG_ONE_TO_GO:
            self.stay_said = self.caution_no
            wave = "wave" in (st.note or "").lower()
            if me.free_pass or ((me.waved or wave) and self.wave_said == self.caution_no):
                return                                         # said already ("stay out")
            if me.waved or wave:
                self.wave_said = self.caution_no
            self.say("pits", CREW, self._stay_words(st.note), HIGH, 20, "plan", trig="stay_out")

    # ----------------------------------------------------------------------- your crew chief: the teammates
    def _teammates(self, car, engine, snap, rows, green, crossed, res=None):
        """Your car only: what AIMOD has your AI teammates doing. Only the slow-changing things: each one's pit
        plan (and when it moves), their caution calls (once all are made), now and then a pace comparison."""
        mates = [c for c in snap.cars if c.valid and not c.reason_out and c.idx != car.idx and c.team == car.team
                 and c.controlled]
        states = getattr(engine, "cars", {})
        if not mates or not engine.race_started:
            return
        if green:
            self.mate_first = None
            self.mate_stay.clear()
            for m in mates:
                self._mate_plan(m, states.get(m.idx), engine, rows.get(m.idx))
        else:
            self._mate_caution(mates, states, engine, snap, rows)
        if crossed:
            self.mate_green = self.mate_green + 1 if green and not car.on_pit_road else 0
            self._mate_pace(car, mates, engine)
        ids = {m.idx: m for m in mates}
        for ev in getattr(res, "events", None) or []:
            adj = getattr(ev, "adjust", None)
            if ev.idx in ids and adj is not None and getattr(adj, "kind", ""):
                self._mate_adjust(ids[ev.idx], adj)

    def _mate_adjust(self, m, adj):
        """a teammate's crew changing his car at this stop ("The 48's taking wedge out, he's been tight.")"""
        up = adj.amount > 0
        what = {"wedge": "putting wedge in" if up else "taking wedge out",
                "track_bar": "moving the track bar", "pressure": "changing the right rear air",
                "tape": "adding grille tape"}.get(adj.kind)
        if not what:
            return
        diag = str(adj.diagnosis or "").lower()
        why = ", he's been tight" if "tight" in diag else ", he's been loose" if "loose" in diag else ""
        self.say("team", CREW, f"The {m.number}'s {what} this stop{why}.", NORMAL, 25, f"mate_adj{m.idx}")

    def _mate_plan(self, m, st, engine, row):
        if st is None or m.on_pit_road or st.event is not None or (row is not None and row.kind == "pitting"):
            return
        plan = st.planned_lap
        if plan == STAGE_BREAK:
            lap = STAGE_BREAK
        elif isinstance(plan, int):
            car_lap = m.laps_done + 1 if m.laps_done > 0 else engine.lap_now
            lap = engine.lap_now + (plan - car_lap)                     # its own laps -> race laps
            if lap <= engine.lap_now:
                return                                                  # due now: the stop itself is next
        else:
            return
        old = self.mate_plan.get(m.idx)
        now = engine.lap_now
        if old is not None and old[0] == lap:
            return
        if old is not None and isinstance(old[0], int) and isinstance(lap, int) and old[0] > now:
            if abs(lap - old[0]) < 2 or now - old[1] < 3:              # small revisions aren't news
                return
            text = (f"The {m.number} has moved his stop up to lap {lap}." if lap < old[0]
                    else f"The {m.number} has pushed his stop back to lap {lap}.")
        elif lap == STAGE_BREAK:
            text = f"Your teammate the {m.number} is running to the stage break."
        else:
            text = f"Your teammate the {m.number} is planning to pit around lap {lap}."
        self.mate_plan[m.idx] = (lap, now)
        self.say("team", CREW, text, NORMAL, 25, f"mate_plan{m.idx}")

    def _mate_caution(self, mates, states, engine, snap, rows):
        if engine.pits_open_at is None:
            return
        now = time.monotonic()
        calls: dict[int, str] = {}
        waiting = False
        for m in mates:
            if self.mate_call.get(m.idx) == self.caution_no:
                continue
            st, row = states.get(m.idx), rows.get(m.idx)
            if st is None or (row is not None and (row.status or "").upper().startswith("WAITS FOR")):
                waiting = True
                continue
            ev = st.event
            if ev is not None or st.decision is True:
                service = ev.service if ev is not None else st.service
                if service is None and ev is None:
                    waiting = True                                      # pitting, the service not picked yet
                    continue
                words = {FOUR_TIRES: "four tires", RIGHT_SIDE: "two right sides",
                         LEFT_SIDE: "two left sides"}.get(service, "fuel only")
                calls[m.idx] = f"the {m.number} is pitting for {words}"
            elif st.decision is False:
                since = self.mate_stay.setdefault(m.idx, now)
                if now - since < STAY_OUT_WAIT and snap.flag < FLAG_ONE_TO_GO:
                    waiting = True                                      # a stay-out can still flip
                    continue
                calls[m.idx] = f"the {m.number} is staying out"
            else:
                self.mate_stay.pop(m.idx, None)
                waiting = True
        if not calls:
            return
        if self.mate_said_caution != self.caution_no:
            # the first word on the teammates waits for all of them (a short while at most)
            if self.mate_first is None:
                self.mate_first = now
            if waiting and now - self.mate_first < 15 and snap.flag < FLAG_ONE_TO_GO:
                return
            self.mate_said_caution = self.caution_no
            parts = list(calls.values())
            joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
            text = f"Under this caution {joined}."
        else:
            text = " ".join(p[0].upper() + p[1:] + "." for p in calls.values())
        for idx in calls:
            self.mate_call[idx] = self.caution_no
        self.say("team", CREW, text, NORMAL, 25, "mate_caution")

    def _mate_pace(self, car, mates, engine):
        """every TEAM_PACE_LAPS clean green laps: one teammate's pace against yours, from the last laps at the
        line (only when every one of those laps was a clean green lap for both)"""
        if self.mate_green < 5 or engine.lap_now - self.mate_pace_lap < TEAM_PACE_LAPS:
            return
        mine = self._lap_times(car.idx, car.laps_done)
        if mine is None:
            return
        mates = sorted(mates, key=lambda m: m.number)
        for k in range(len(mates)):
            m = mates[(self.mate_pace_turn + k) % len(mates)]
            theirs = self._lap_times(m.idx, m.laps_done) if not m.on_pit_road else None
            if theirs is None:
                continue
            self.mate_pace_turn = (self.mate_pace_turn + k + 1) % len(mates)
            self.mate_pace_lap = engine.lap_now
            d = sorted(theirs)[1] - sorted(mine)[1]                   # middle of three laps each
            if abs(d) < 0.1:
                text = f"The {m.number} is running about the same pace as you."
            else:
                text = f"The {m.number} is running about {tenths(abs(d))} {'quicker' if d < 0 else 'slower'} than you."
            self.say("team", CREW, text, LOW, 20, "mate_pace")
            return

    def _lap_times(self, slot, laps_done) -> list[float] | None:
        d = self.cross.get(slot, {})
        try:
            t = [(d[laps_done - k] - d[laps_done - k - 1]) / 1000 for k in range(3)]
        except KeyError:
            return None
        mid = sorted(t)[1]
        return t if mid > 0 and all(abs(x - mid) <= 0.03 * mid for x in t) else None    # a stop or a caution lap

    @staticmethod
    def _stop_words(ev, green: bool, changed: bool) -> str:
        tires = {FOUR_TIRES: "four tires", RIGHT_SIDE: "two right sides", LEFT_SIDE: "two left sides"}.get(ev.service)
        fuel = FUEL_SPOKEN.get(ev.fuel_level, "fuel") if ev.fuel else None
        if tires and fuel:
            service = f"{tires} and {fuel}"
        elif tires:
            service = f"{tires}, no fuel"
        else:
            service = f"{fuel or 'fuel'} only"
        note = (ev.note or "").lower()
        if ev.damage:
            service = "fix the damage, " + service
        if green:
            opener = "Box this lap, box this lap."
        elif "top off" in note:
            opener = "Back in to top it off."
        elif changed:
            opener = "Change of plans, bring it in."
        else:
            opener = pick("Bring it in this time.", "We're pitting this time.", "Pit this time, pit this time.")
        return f"{opener} {service[0].upper()}{service[1:]}."

    @staticmethod
    def _stay_words(note: str) -> str:
        n = (note or "").lower()
        if "flipped" in n:
            return "Stay out. We flipped the stage, we want the track position."
        if "pitted recently" in n:
            return "Stay out, we just pitted."
        if "wave" in n:
            return "Stay out, we're taking the wave around."
        if "makes it on fuel" in n or "track position" in n:
            return pick("We're staying out. We make it on fuel, track position.",
                        "Stay out, stay out. We're good on fuel, track position.")
        return pick("We're staying out this time.", "Stay out, stay out.")

    def _caution_call(self, car, me, engine, snap, total):
        """Once per caution, when pit road opens: come in or stay out, from fuel and tires (your own car:
        AIMOD makes no plan for it, so this is the radio's own reading of fuel and tire age)."""
        if (snap.flag == mm.FLAG_GREEN or engine.pits_open_at is None or self.caution_call == self.caution_no
                or snap.flag >= FLAG_ONE_TO_GO or car.on_pit_road or me is None or not engine.race_started):
            return
        self.caution_call = self.caution_no
        if me.free_pass or me.waved:
            return                                            # said already: stay out
        rate = self._fuel_rate(engine)
        to_go = (total - car.laps_done) if total else None
        fuel_laps = car.fuel / rate if car.fuel is not None and rate else None
        tank = car.fuel_cap / rate if car.fuel_cap and rate else None
        on_tires = car.laps_done - self.last_stop_laps
        fall = self._falloff()
        short = fuel_laps is not None and to_go is not None and fuel_laps < to_go + 0.5
        worn = (tank and on_tires >= 0.35 * tank) or (fall is not None and fall >= 0.3)
        if to_go is not None and to_go <= 3:
            return
        if short and worn:
            text = "Pit road's open. Bring it in, four tires and fuel."
        elif short:
            text = pick("Pit road's open. We need fuel, bring it in.", "Come on in, we need fuel to make it.")
        elif worn and (to_go is None or to_go > 12):
            text = "We're good on fuel, but those tires are old. Come in for tires."
        else:
            text = pick("We're staying out. Track position.", "Stay out, stay out. We're good.")
        self.say("pits", CREW, text, HIGH, 25, trig="stay_out" if "stay" in text.lower() else "pit_call")

    # ----------------------------------------------------------------------- crew chief: other cars
    def _others(self, res, me, car, green):
        if me is None:
            return
        for ev in res.events:
            if ev.idx == car.idx:
                continue
            if green:
                if ev.pos_in == 1:
                    self.say("others", CREW, "Leader's on pit road.", NORMAL, 10, "others")
                elif ev.pos_in == me.pos - 1:
                    self.say("others", CREW, f"The {ev.number} ahead of you is pitting.", NORMAL, 10, "others")
                elif ev.pos_in == me.pos + 1:
                    self.say("others", CREW, f"The {ev.number} behind you is pitting.", LOW, 10, "others")
            elif ev.pos_in and ev.pos_in <= 3 and self.caution_pitting_said != self.caution_no:
                self.caution_pitting_said = self.caution_no
                self.say("others", CREW, "Leaders are coming to pit road.", NORMAL, 10, "others")
        for ev in res.exits:
            if ev.idx != car.idx and ev.pos_in == 1 and self.leader_service_said != self.caution_no:
                self.leader_service_said = self.caution_no
                words = {FOUR_TIRES: "four tires", RIGHT_SIDE: "two tires", LEFT_SIDE: "two left sides"}.get(
                    ev.service, "fuel only" if ev.fuel else "no service")
                self.say("others", CREW, f"Leader took {words}.", LOW, 15, "others")

    # ----------------------------------------------------------------------- at the line
    def _fuel_rate(self, engine) -> float | None:
        if len(self.fuel_rates) >= 2:
            r = sorted(self.fuel_rates[-6:])
            return r[len(r) // 2]
        return getattr(engine, "field_rate", None)

    def _falloff(self) -> float | None:
        laps = self.run_laps
        if len(laps) < 5:
            return None
        best = min(laps[: max(3, min(8, len(laps) - 2))])
        return sum(laps[-2:]) / 2 - best

    def _gap_at_line(self, a: int, b: int, lap: int) -> float | None:
        """seconds b crossed the line for lap `lap` after a did"""
        ta, tb = self.cross.get(a, {}).get(lap), self.cross.get(b, {}).get(lap)
        return (tb - ta) / 1000.0 if ta and tb and tb >= ta else None

    def _at_line(self, car, me, rows, engine, total, green, snap):
        clean = not self.lap_dirty
        self.lap_dirty = not green or bool(car.on_pit_road)
        lap = car.laps_done
        laptime = (car.cross_ms - car.prev_cross_ms) / 1000.0 if car.cross_ms and car.prev_cross_ms else None
        if clean and laptime and 5 < laptime < 600:
            self.run_laps.append(laptime)
        if car.fuel is not None:                              # mileage on clean laps
            if self.fuel_mark and clean and lap == self.fuel_mark[0] + 1:
                used = self.fuel_mark[1] - car.fuel
                if car.fuel_cap and 0 < used < car.fuel_cap * 0.2:
                    self.fuel_rates.append(used)
                    self.fuel_rates = self.fuel_rates[-12:]
            self.fuel_mark = (lap, car.fuel)
        if me is None:
            return
        pos = me.pos
        stage_end = [e for e in getattr(engine, "stage_list", []) if e == lap]
        if stage_end and ("end", lap) not in self.stage_said:
            self.stage_said.add(("end", lap))
            self.say("countdown", CREW, f"That's the end of stage {engine.stage_no(lap)}. We were P {pos}.",
                     NORMAL, 15, trig="stage_end", stage=engine.stage_no(lap))
        if total and lap >= total and not self.checker_said and engine.race_started:
            self.checker_said = True
            if pos == 1:
                self.chatter("win", URGENT)
            self.say("countdown", CREW, pick(f"Checkered flag! P {pos}. Good run.",
                                             f"That's it, P {pos}. Nice job out there."), HIGH, 20, trig="checkered", pos=pos)
            return
        if not green:
            self.my_pos = pos
            return
        self.green_laps_since_report += 1
        by_pos = {r.pos: r for r in rows.values() if r.pos}
        ahead = by_pos.get(pos - 1)
        if ahead is not None:
            g = self._gap_at_line(ahead.idx, car.idx, lap)
            if g is not None:
                self.gaps_ahead.append((lap, ahead.idx, g))
                self.gaps_ahead = self.gaps_ahead[-6:]
        said = False
        # fuel first: it decides the race
        rate = self._fuel_rate(engine)
        if car.fuel is not None and rate and not car.on_pit_road:
            fuel_laps = car.fuel / rate
            to_go = (total - lap) if total else 9999
            if fuel_laps < to_go:
                calls = ((2.5, URGENT, "Box this lap! You're about out of fuel."),
                         (5.5, HIGH, "About five laps of fuel left. Pit window's open."),
                         (10.5, NORMAL, "Ten laps of fuel left."))
                if car.controlled:          # an AI car: AIMOD decides when it pits, the radio only informs
                    calls = ((2.5, HIGH, "Two laps of fuel left."), (5.5, NORMAL, "About five laps of fuel left."),
                             (10.5, NORMAL, "Ten laps of fuel left."))
                for limit, prio, text in calls:
                    if fuel_laps <= limit and f"fuel{limit}" not in self.run_said:
                        self.run_said.add(f"fuel{limit}")
                        self.say("fuel", CREW, text, prio, 20, trig="fuel_low", laps=int(limit))
                        said = True
                        break
            tank = car.fuel_cap / rate if car.fuel_cap else None
            if not said and total and tank and to_go <= tank + 1:
                margin = fuel_laps - to_go
                if margin >= 0.7 and "fuel_ok" not in self.run_said:
                    self.run_said.add("fuel_ok")
                    self.say("fuel", CREW, pick("Good news, we're good on fuel to the end.",
                                                "Fuel's good to the finish. Go get 'em."), NORMAL, 20, trig="fuel_ok")
                    said = True
                elif -3 <= margin < 0 and f"short{math.ceil(-margin)}" not in self.run_said:
                    n = math.ceil(-margin)
                    self.run_said.add(f"short{n}")
                    self.say("fuel", CREW, f"We're about {n} lap{'s' if n > 1 else ''} short on fuel. "
                                           "Save what you can.", HIGH, 20, trig="fuel_short", laps=n)
                    said = True
        # then one more thing at most per lap, the most useful first
        if said or self._tires() or self._closing(rows):
            said = True
        elif self.after_stop:
            self.after_stop = False
            self.say("position", CREW, f"We're P {pos} after the stop.", LOW, 15, trig="stop_done", pos=pos)
            said = True
        elif self.my_pos and pos != self.my_pos and not car.on_pit_road:
            if pos == 1:
                self.chatter("lead", HIGH)
            elif pos < self.my_pos:
                self.chatter("pass", NORMAL, 0.6)
            else:
                self.chatter("passed", NORMAL, 0.5)
            if pos < self.my_pos:
                self.say("position", CREW, pick(f"Nice, up to P {pos}.", f"Good job, P {pos}."), LOW, 10, "pos", trig="gained", pos=pos)
            else:
                self.say("position", CREW, f"Lost one there, P {pos}.", LOW, 10, "pos", trig="lost", pos=pos)
            said = True
        elif self._fastest(car, laptime, clean, lap, rows):
            said = True
        if not said and self.green_laps_since_report >= int(self.cfg.get("report_laps", 15)):
            self.green_laps_since_report = 0
            self.say("position", CREW, self._report(car, me, by_pos, lap), LOW, 15, "report")
        self.my_pos = pos

    def _report(self, car, me, by_pos, lap) -> str:
        parts = []
        if me.pos == 1:
            behind = by_pos.get(2)
            g = self._gap_at_line(car.idx, behind.idx, lap) if behind else None
            parts.append("You're leading" + (f", {secs(g)} over the {behind.number}." if g is not None else "."))
        else:
            parts.append(f"P {me.pos}.")
            if self.gaps_ahead and self.gaps_ahead[-1][0] == lap:
                ahead = by_pos.get(me.pos - 1)
                if ahead is not None:
                    parts.append(f"{secs(self.gaps_ahead[-1][2])} to the {ahead.number}.")
        if self.gaps_behind and self.gaps_behind[-1][0] >= lap - 1:
            _lap, slot, g = self.gaps_behind[-1]
            behind = next((r for r in by_pos.values() if r.idx == slot), None)
            if behind is not None and me.pos != 1:
                parts.append(f"{secs(g)} back to the {behind.number}.")
        return " ".join(parts)

    def _tires(self) -> bool:
        fall = self._falloff()
        if fall is None:
            return False
        if fall >= 0.6 and "tires2" not in self.run_said:
            self.run_said.update({"tires1", "tires2"})
            self.chatter("tires")                              # queued first: the driver, then his crew
            self.say("tires", CREW, f"Tires are falling off. We're {tenths(fall)} off our best.", NORMAL, 15, trig="tires_going")
            return True
        if fall >= 0.25 and "tires1" not in self.run_said:
            self.run_said.add("tires1")
            self.say("tires", CREW, f"Tires are starting to go, about {tenths(fall)} off our best.", NORMAL, 15, trig="tires_going")
            return True
        return False

    def _closing(self, rows) -> bool:
        g = self.gaps_ahead
        if len(g) < 3 or g[-1][1] != g[-3][1] or g[-1][0] - g[-3][0] != 2:
            return False
        lap, slot, gap = g[-1]
        rate = (g[-3][2] - gap) / 2
        key = f"ahead{slot}"
        if gap < 3.0 and rate >= 0.1 and lap - self.gap_said.get(key, -99) >= 4:
            self.gap_said[key] = lap
            num = rows[slot].number if slot in rows else "car"
            self.say("gaps", CREW, f"You're catching the {num}, {tenths(rate)} a lap. Gap's {secs(gap)}.", NORMAL, 10,
                     "gap", trig="catching", other=num)
            return True
        return False

    def _caught(self, car, me, rows, green):
        """the car behind crossed the line: gap back to it, and is it catching us"""
        if me is None or not green or self.my_laps is None:
            return
        behind = next((r for r in rows.values() if r.pos == me.pos + 1), None)
        lap = car.laps_done
        if behind is None or lap <= self.behind_lap_seen:
            return
        g = self._gap_at_line(car.idx, behind.idx, lap)
        if g is None:
            return
        self.behind_lap_seen = lap
        self.gaps_behind.append((lap, behind.idx, g))
        self.gaps_behind = self.gaps_behind[-6:]
        h = self.gaps_behind
        if len(h) >= 3 and h[-3][1] == behind.idx and h[-1][0] - h[-3][0] == 2:
            rate = (h[-3][2] - g) / 2
            key = f"behind{behind.idx}"
            if g < 2.0 and rate >= 0.1 and lap - self.gap_said.get(key, -99) >= 4:
                self.gap_said[key] = lap
                self.say("gaps", CREW, f"The {behind.number} is coming, {tenths(rate)} a lap. He's {secs(g)} back.",
                         NORMAL, 10, "gap", trig="caught", other=behind.number)

    def _fastest(self, car, laptime, clean, lap, rows) -> bool:
        if not clean or not laptime or lap - self.fastest_said < 10:
            return False
        others = []
        for idx, laps in self.cross.items():
            if idx == car.idx or idx not in rows or rows[idx].laps_down:
                continue
            l2 = max(laps) if laps else None
            if l2 is not None and l2 >= lap - 1 and (l2 - 1) in laps:
                others.append((laps[l2] - laps[l2 - 1]) / 1000.0)
        if len(others) >= 5 and laptime < min(others):
            self.fastest_said = lap
            self.say("tires", CREW, pick("Fastest car on track right now.", "That's the fastest lap out there."),
                     LOW, 10, trig="fastest")
            return True
        return False
