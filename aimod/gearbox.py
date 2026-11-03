"""5-speed (or 6-speed) gearbox for NR2003, the native way (memory only: game files are never changed).

NR2003.exe's engine table (VA 0x6EECA8 + k*0x70, .rdata) holds each car class's forward-gear count at +0x44
(reverse ratio at +0x48). Raising the count of the Cup records (k = 0..4: qualifying, race, plate qualifying,
plate race, chassis 0) from 4 to 5 makes the game itself offer a 5th gear: the garage shows and tunes it and
setups save it (v14 .sim: 5th gear index at 0x338, 6th at 0x33C). Confirmed live 2026-10-18 with
Desktop\\MODS\\GearboxProbe\\garage_test.py.

Best set before a session or the garage loads (AIMOD does it as soon as it attaches - normally at NR2003's main
menu); a change made mid-session applies from the next session / garage load. Per-car gearbox ratios are never
rewritten, so the player's garage choices stand.
"""
from __future__ import annotations

import ctypes
import logging
import struct
from ctypes import wintypes

log = logging.getLogger("aimod.gearbox")

ENGINE_TABLE = 0x6EECA8
STRIDE = 0x70
OFF_GEARS = 0x44
OFF_REVERSE = 0x48
CUP_RECORDS = (0, 1, 2, 3, 4)
STOCK_REVERSE = -2.7333
STOCK_GEARS = 4
GEARBOX_MOD_PREFIXES = ("FCRD NCS22",)     # mods where it's on by default (any mod can turn it on)

# each AI car's own gearbox record (count | reverse | neutral | 6 ratios | final drive), per car slot
AI_RECORDS = ((0xACCA30 + 1104, 8724), (0xC12480, 0x15B8), (0xE0C688, 0x15B8))
PLAYER_SLOT, PACE_SLOT, SLOTS = 0, 1, 44


def offered(mod: str) -> bool:
    return True


def default_on(mod: str) -> bool:
    return any((mod or "").startswith(p) for p in GEARBOX_MOD_PREFIXES)


def spread(first: float, top: float, n: int) -> list[float]:
    """n ratios from first to top in equal ratio steps."""
    step = (top / first) ** (1.0 / (n - 1))
    return [round(first * step ** i, 4) for i in range(n)]


class Gearbox:
    def __init__(self):
        self.status = ""
        self.applied: int | None = None           # gear count AIMOD last set (None = untouched)

    @staticmethod
    def _write(pm, addr: int, data: bytes) -> bool:
        k32 = ctypes.windll.kernel32
        old = wintypes.DWORD()
        h = pm.process_handle
        k32.VirtualProtectEx(h, ctypes.c_void_p(addr), len(data), 0x04, ctypes.byref(old))
        try:
            pm.write_bytes(addr, data, len(data))
            return True
        except Exception as e:
            log.warning("Gearbox: write failed at 0x%X (%s)", addr, e)
            return False
        finally:
            k32.VirtualProtectEx(h, ctypes.c_void_p(addr), len(data), old.value, ctypes.byref(old))

    def counts(self, pm) -> list[int | None]:
        """Forward-gear count of each Cup record, None where the record doesn't look like the stock one."""
        out = []
        for k in CUP_RECORDS:
            try:
                n, rev = struct.unpack("<if", pm.read_bytes(ENGINE_TABLE + k * STRIDE + OFF_GEARS, 8))
            except Exception:
                out.append(None)
                continue
            out.append(n if abs(rev - STOCK_REVERSE) < 0.01 and 3 <= n <= 6 else None)
        return out

    def apply(self, pm, gears: int) -> int:
        """Set the Cup records' forward-gear count. Returns how many records were changed."""
        gears = max(STOCK_GEARS, min(6, int(gears)))
        changed = 0
        counts = self.counts(pm)
        for k, n in zip(CUP_RECORDS, counts):
            if n is None or n == gears:
                continue
            if self._write(pm, ENGINE_TABLE + k * STRIDE + OFF_GEARS, struct.pack("<i", gears)):
                changed += 1
        bad = sum(1 for n in counts if n is None)
        if changed:
            log.info("Gearbox: %d-speed set on %d Cup engine records%s", gears, changed,
                     f" ({bad} record(s) not as expected - left alone)" if bad else "")
        if bad == len(CUP_RECORDS):
            self.status = "Gearbox: engine table not as expected (different NR2003.exe?) - not changed"
        else:
            self.status = (f"Gearbox: {gears}-speed (native - the garage shows it; applies from the next "
                           f"session or garage load)" if gears != STOCK_GEARS else "Gearbox: stock 4-speed")
        self.applied = gears
        return changed

    def restore(self, pm) -> int:
        return self.apply(pm, STOCK_GEARS)

    def spread_ai(self, pm, gears: int) -> int:
        """AI cars: spread their gears so the top gear is where the 4-speed's 4th was (same top speed,
        every gear needed). Each car's own 1st and old top gear are kept. The game rebuilds these records
        on a session load, so this is repeated; records already as written are skipped. Not the player's."""
        if not hasattr(self, "_ai_written"):
            self._ai_written = {}
        changed = 0
        for s in range(SLOTS):
            if s in (PLAYER_SLOT, PACE_SLOT):
                continue
            for base, stride in AI_RECORDS:
                addr = base + s * stride
                try:
                    raw = pm.read_bytes(addr, 40)
                except Exception:
                    continue
                if self._ai_written.get(addr) == raw[:36]:
                    continue
                n, rev, neu = struct.unpack_from("<iff", raw, 0)
                g = list(struct.unpack_from("<6f", raw, 12))
                if not (gears <= n <= 6 and -5.0 < rev < -1.0 and neu == 0.0 and g[0] > g[1] > g[2] > g[3] > 0.3):
                    continue
                steps = [g[i] / g[i + 1] for i in range(gears - 1) if g[i + 1] > 0]
                if len(steps) == gears - 1 and max(steps) - min(steps) < 0.01 * max(steps):
                    self._ai_written[addr] = raw[:36]   # already spread (equal steps): leave it
                    continue
                top = g[3]                          # the 4-speed's top gear (5th/6th are the game's extras)
                new = spread(g[0], top, gears) + g[gears:]
                data = struct.pack("<i", gears) + raw[4:12] + struct.pack("<6f", *new)
                try:
                    pm.write_bytes(addr, data, len(data))
                except Exception:
                    continue
                self._ai_written[addr] = data
                changed += 1
        if changed:
            log.info("Gearbox: AI gears spread to %d speeds on %d records (top gear = the old 4th)", gears, changed)
        return changed

    def forget_ai(self):
        self._ai_written = {}

    def forget(self):
        self.applied = None
