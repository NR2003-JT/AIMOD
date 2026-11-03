"""Reads the wave-around logs of Race Control (race_control.log) and Better Cautions with
Wave Around (bc_wave.log) to tag waved cars.

Lines understood:
  17:09:09 lap 52 flag 5: wave around (laps behind the leader): #83(1.93) #22(1.94) -> tail of the field
  02:30:16 lap 51 flag 2: wave around: #37 stayed out ahead of #5 (first lead lap car on track), ...
  12:00:00 lap 112 flag 2: wave around: #88 stayed out in front of every lead lap car on track (first: #47), ...
  12:54:18 lap 66 flag 2: first time by summary - waved: #9 #33; lead lap, sent around ...: #33; ...
  14:30:00 lap 88 flag 3: first time by summary - free pass: #47; waved: #8 #77; ...      (Race Control 2.6.5+)
  14:29:40 lap 87 flag 2: free pass: #47 (1.21 laps behind the leader, the first car one lap down) goes ...
  15:05:03 lap 86 flag 2: waved #8 goes to the end of the line until it has passed the pace car
  15:05:44 lap 87 flag 2: waved #8 crossed the line after passing the pace car (lap gained on track); ...
  (a car between those two lines is still going around the pace car: a stop then costs it a lap;
   also released by "waved #N is pitting", "#N dropped back behind the leader instead of going around
   the pace car", "free pass: #N did not get around the pace car in 45 s", or any flag 7 line)
AIMOD only reads the files; lines written before AIMOD started are ignored.
"""
from __future__ import annotations

import ctypes
import os
import re
import sys
import time

WAVE_RE = re.compile(r"lap (\d+) flag \d+: wave around[^:]*:(.*)")
SUMMARY_RE = re.compile(r"lap (\d+) flag \d+: first time by summary - .*?waved:([^;]*)")
FREE_PASS_RE = re.compile(r"lap (\d+) flag \d+: (?:first time by summary - )?free pass: #(\d+)")
NUM_RE = re.compile(r"#(\d+)")
AROUND_RE = re.compile(r"lap (\d+) flag \d+: waved #(\d+) goes to the end of the line")
AROUND_DONE_RE = re.compile(r"flag \d+: (?:waved #(\d+) (?:crossed the line after passing the pace car|is pitting)"
                            r"|#(\d+) dropped back behind the leader instead of going around the pace car"
                            r"|free pass: #(\d+) did not get around the pace car)")
GREEN_RE = re.compile(r"lap \d+ flag 7:")
AROUND_LAPS = 2        # forget a trip around the pace car this many laps on (a missed line)
SHOW_LAPS = 5          # keep the tag this many laps after the wave


def default_paths() -> list[str]:
    base = os.path.join(os.path.expanduser("~"), "Desktop", "MODS", "RaceControl")
    return [os.path.join(base, "app", "race_control.log")]


def race_control_logs() -> list[str]:
    """race_control.log next to every running RaceControl*.exe (Race Control writes its log beside the exe,
    wherever it is installed)."""
    if sys.platform != "win32":
        return []
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.OpenProcess.restype = wintypes.HANDLE
    out = []
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return out
    try:
        e = PE()
        e.dwSize = ctypes.sizeof(PE)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            if e.szExeFile.lower().startswith("racecontrol") and e.szExeFile.lower().endswith(".exe"):
                h = k32.OpenProcess(0x1000, False, e.th32ProcessID)      # PROCESS_QUERY_LIMITED_INFORMATION
                if h:
                    try:
                        buf = ctypes.create_unicode_buffer(1024)
                        n = wintypes.DWORD(1024)
                        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                            out.append(os.path.join(os.path.dirname(buf.value), "race_control.log"))
                    finally:
                        k32.CloseHandle(h)
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def default_path() -> str:
    return default_paths()[0]


def parse_line(line: str) -> tuple[int, list[int]] | None:
    m = SUMMARY_RE.search(line)
    if m:
        return int(m.group(1)), [int(n) for n in NUM_RE.findall(m.group(2))]
    m = WAVE_RE.search(line)
    if not m:
        return None
    body = m.group(2).split("->")[0]
    if "stayed out" in body:                   # one car per line; later numbers are lead-lap cars it passed
        body = body.split("stayed out")[0]
    return int(m.group(1)), [int(n) for n in NUM_RE.findall(body)]


class WaveLog:
    def __init__(self, path: str | list[str] | None = None):
        paths = path if isinstance(path, list) else [path] if path else default_paths()
        self.paths = paths
        self.path = paths[0]
        self._pos: dict[str, int | None] = {p: None for p in paths}
        self.waved: dict[int, int] = {}      # car number -> race lap of the wave
        self.free_pass: dict[int, int] = {}  # car number -> race lap of the free pass (lucky dog)
        self.new_free_pass: list[tuple[int, int]] = []
        self.around: dict[int, int] = {}     # car number -> race lap it was sent around the pace car
        self.find_race_control = path is None
        self._next_find = 0.0

    def _find_more(self):
        """Every 10 s: add the log of a Race Control that is running from anywhere."""
        if not self.find_race_control or time.monotonic() < self._next_find:
            return
        self._next_find = time.monotonic() + 10.0
        try:
            found = race_control_logs()
        except Exception:
            return
        for p in found:
            if os.path.normcase(p) not in {os.path.normcase(x) for x in self.paths}:
                self.paths.append(p)
                self._pos[p] = None

    def poll(self) -> list[tuple[int, list[int]]]:
        """New wave-arounds since the last poll: [(lap, [car numbers])]."""
        self._find_more()
        found = []
        for path in list(self.paths):
            found += self._poll(path)
        return found

    def _poll(self, path: str) -> list[tuple[int, list[int]]]:
        try:
            size = os.path.getsize(path)
        except OSError:
            if self._pos.get(path) is None:
                self._pos[path] = 0                # appears later: read it from the start
            return []
        pos = self._pos.get(path)
        if pos is None or size < pos:              # first look, or log restarted
            self._pos[path] = size if pos is None else 0
            return []
        if size == pos:
            return []
        with open(path, encoding="utf-8", errors="replace") as f:
            f.seek(pos)
            text = f.read()
            self._pos[path] = f.tell()
        found = []
        for line in text.splitlines():
            ar = AROUND_RE.search(line)
            if ar:
                self.around[int(ar.group(2))] = int(ar.group(1))
            else:
                done = AROUND_DONE_RE.search(line)
                if done:
                    self.around.pop(int(next(g for g in done.groups() if g)), None)
                elif GREEN_RE.search(line):
                    self.around.clear()                # green: nobody is going around any more
            fp = FREE_PASS_RE.search(line)
            if fp:
                lap, number = int(fp.group(1)), int(fp.group(2))
                if self.free_pass.get(number) != lap:
                    self.new_free_pass.append((lap, number))
                self.free_pass[number] = lap
            hit = parse_line(line)
            if hit and hit[1]:
                lap, nums = hit
                new = [n for n in nums if self.waved.get(n) != lap]
                for n in nums:
                    self.waved[n] = lap
                if new:
                    found.append((lap, new))
        return found

    def is_going_around(self, number: int, race_lap: int) -> bool:
        """Sent around the pace car (wave around, free pass, or a lead lap car catching the field) and not
        yet back across the line behind it."""
        lap = self.around.get(number)
        if lap is None:
            return False
        if race_lap > lap + AROUND_LAPS or race_lap < lap - 1:
            del self.around[number]
            return False
        return True

    def is_free_pass(self, number: int, race_lap: int) -> bool:
        lap = self.free_pass.get(number)
        if lap is None:
            return False
        if race_lap > lap + SHOW_LAPS or race_lap < lap - 1:
            del self.free_pass[number]
            return False
        return True

    def is_waved(self, number: int, race_lap: int) -> bool:
        lap = self.waved.get(number)
        if lap is None:
            return False
        if race_lap > lap + SHOW_LAPS or race_lap < lap - 1:
            del self.waved[number]
            return False
        return True

    def reset(self):
        self.waved.clear()
        self.free_pass.clear()
        self.around.clear()
