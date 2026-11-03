"""5-speed (or 6-speed) gearing in the tracks' fast setups (tracks\\<track>\\fast.cup.sim) - the setup every AI
car runs and the "fast" setup the player can load. The gears are re-spread so the top gear is the 4-speed's
old 4th (same top speed) and every gear is needed: each file's own 1st and 4th are kept and the gears in
between are put in equal ratio steps, using the nearest ratios the garage offers.

Garage gear lookup (NR2003.exe file offset 0x2ED880): entry i = two int32 tooth counts (a, b), gear ratio b/a;
gears use entries 0-68 (index 5 = 42/16 = 2.625, 14 = 32/16, 65 = 1:1, 66 = 24/25).
Setup file: v14 (2892 bytes) has 1st-4th gear index at 0x2F8/0x2FC/0x300/0x304, final drive at 0x308, 5th at
0x338, 6th at 0x33C. v13 (2884 bytes) lacks the 5th/6th fields: it becomes v14 by inserting them at 0x338,
size field (byte 8) + 8, the DGTS block's own length (0x224) + 8 and version (0x220) = 14. (Without the block
length NR2003 refuses the file: "cannot load track".)

Every original is kept as <file>.aimod4spd.bak before the first change; restore() puts them back.
These files are shared by every cup-type mod that runs the track.
"""
from __future__ import annotations

import glob
import logging
import os
import shutil
import struct

log = logging.getLogger("aimod.setupfiles")

LOOKUP_OFF = 0x2ED880
BACKUP = ".aimod4spd.bak"
GEAR_OFFS = (0x2F8, 0x2FC, 0x300, 0x304)
OFF_5TH, OFF_6TH, OFF_VER, OFF_SIZE = 0x338, 0x33C, 0x220, 8
OFF_BLOCK = 0x224                                       # DGTS block length (v13 272, v14 280)


def gear_table(exe_path: str) -> list[float] | None:
    """Ratio of garage gear index 0..68 (None if the exe isn't the expected one)."""
    try:
        with open(exe_path, "rb") as f:
            f.seek(LOOKUP_OFF)
            raw = f.read(69 * 8)
    except OSError:
        return None
    out = []
    for i in range(69):
        a, b = struct.unpack_from("<II", raw, 8 * i)
        if not (8 <= a <= 60 and 8 <= b <= 60):
            return None
        out.append(b / a)
    if abs(out[14] - 2.0) > 1e-6 or abs(out[65] - 1.0) > 1e-6:
        return None
    return out


def respread(indexes: list[int], table: list[float], gears: int) -> list[int]:
    """Gear indexes for `gears` speeds from this setup's 1st and 4th."""
    first, top = table[indexes[0]], table[indexes[3]]
    step = (top / first) ** (1.0 / (gears - 1))
    out, lo = [], 0
    for g in range(gears):
        want = first * step ** g
        cands = range(lo, 69)
        best = min(cands, key=lambda i: abs(table[i] - want))
        out.append(best)
        lo = best + 1                                    # strictly shorter each gear
    out[0], out[-1] = indexes[0], indexes[3]             # keep the setup's own 1st and top
    return out


def convert(path: str, table: list[float], gears: int = 5) -> bool:
    """Re-gear one setup file (keeps a backup). False if it isn't a setup this knows."""
    try:
        with open(path, "rb") as f:
            d = bytearray(f.read())
    except OSError:
        return False
    if d[:4] != b"PGTS" or len(d) not in (2884, 2892):
        return False
    ver = struct.unpack_from("<i", d, OFF_VER)[0]
    if (ver, len(d)) not in ((13, 2884), (14, 2892)):
        return False
    src = path + BACKUP
    if os.path.isfile(src):                              # always re-gear from the original
        with open(src, "rb") as f:
            d = bytearray(f.read())
        ver = struct.unpack_from("<i", d, OFF_VER)[0]
    else:
        shutil.copy2(path, src)
    idx = [struct.unpack_from("<i", d, o)[0] for o in GEAR_OFFS]
    if not all(0 <= i <= 68 for i in idx) or not idx[0] < idx[1] < idx[2] < idx[3]:
        return False
    if ver == 13:                                        # make room for 5th / 6th (v14 layout)
        d[OFF_5TH:OFF_5TH] = struct.pack("<ii", 66, 67)
        struct.pack_into("<I", d, OFF_SIZE, struct.unpack_from("<I", d, OFF_SIZE)[0] + 8)
        struct.pack_into("<i", d, OFF_VER, 14)
        struct.pack_into("<i", d, OFF_BLOCK, struct.unpack_from("<i", d, OFF_BLOCK)[0] + 8)
    new = respread(idx, table, gears)
    for o, i in zip(GEAR_OFFS, new[:4]):
        struct.pack_into("<i", d, o, i)
    struct.pack_into("<i", d, OFF_5TH, new[4])
    struct.pack_into("<i", d, OFF_6TH, new[5] if gears >= 6 else min(68, new[4] + 1))
    with open(path, "wb") as f:
        f.write(d)
    return True


def convert_all(nr2003_dir: str, gears: int = 5, series: str = "cup") -> tuple[int, int]:
    """Re-gear every track's fast.<series>.sim. Returns (converted, skipped)."""
    table = gear_table(os.path.join(nr2003_dir, "NR2003.exe"))
    if table is None:
        log.warning("Setup gearing: NR2003.exe gear table not as expected - setup files not changed")
        return 0, 0
    done = skipped = 0
    for path in glob.glob(os.path.join(nr2003_dir, "tracks", "*", f"fast.{series}.sim")):
        if convert(path, table, gears):
            done += 1
        else:
            skipped += 1
    log.info("Setup gearing: %d-speed spread in %d track fast setups (%d skipped); originals kept as *%s",
             gears, done, skipped, BACKUP)
    return done, skipped


def restore_all(nr2003_dir: str, series: str = "cup") -> int:
    n = 0
    for bak in glob.glob(os.path.join(nr2003_dir, "tracks", "*", f"fast.{series}.sim{BACKUP}")):
        try:
            shutil.copy2(bak, bak[: -len(BACKUP)])
            os.remove(bak)
            n += 1
        except OSError as e:
            log.warning("Setup gearing: could not restore %s (%s)", bak, e)
    if n:
        log.info("Setup gearing: %d track fast setups restored to their original 4-speed gearing", n)
    return n


def converted(nr2003_dir: str, series: str = "cup") -> bool:
    return bool(glob.glob(os.path.join(nr2003_dir, "tracks", "*", f"fast.{series}.sim{BACKUP}")))
