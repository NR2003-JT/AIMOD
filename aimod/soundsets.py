"""Per-mod NR2003 loose-sound sets.

NR2003 reads loose WAVs from ``<game>\\sound`` ahead of the packed ``sound.dat``. A *sound set* is a folder of
loose WAVs (plus an optional ``set.json``). AIMOD stages a set into the game folder before the game starts; the
game loads its sounds at launch, so nothing here can switch sounds live.

Safety rules (see "SOUND SETS - plan and prompt.txt"):
  * Apply is refused while NR2003.exe runs.
  * Every WAV in the resolved set is validated BEFORE anything is written (all-or-nothing).
  * The game's loose WAVs are copied once to ``sound\\_stock_backup``; AIMOD records every file it writes in
    ``sound\\_aimod_sounds.json`` and only ever removes or replaces files listed there.
  * A set never clears the folder. Files a set does not supply keep (or get back) the original loose file, and a
    final check puts back anything from the backup that went missing, so the bank is always complete.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Callable

log = logging.getLogger("aimod.soundsets")

ORIGINAL = "Original sounds"
BACKUP_DIR = "_stock_backup"
MANIFEST = "_aimod_sounds.json"
SET_JSON = "set.json"
RATE = 22050
# Files that are stereo in the stock bank (and in Petty's V8); everything else is mono.
STEREO = frozenset({
    "winston_high.wav", "winston_idle.wav", "winston_low.wav", "winston_lowmed.wav", "winston_med.wav",
    "winston_start.wav", "winston_verylow.wav", "win_high_off.wav", "win_low_off.wav", "win_med_off.wav",
    "blimp_cam.wav",
})


class SoundSetError(Exception):
    pass


def _bundled_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "sound_sets"
    return Path(__file__).resolve().parent.parent / "sound_sets"


def nr2003_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq NR2003.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "nr2003.exe" in out.casefold()


# ---------------------------------------------------------------- WAV checks

def read_wav(path: Path) -> tuple[int, int, int, bytes]:
    """(channels, rate, bits, pcm bytes) of a PCM WAV; raises SoundSetError on anything else."""
    try:
        with wave.open(str(path), "rb") as w:
            if w.getcomptype() != "NONE":
                raise SoundSetError(f"{path.name}: not PCM")
            return w.getnchannels(), w.getframerate(), w.getsampwidth() * 8, w.readframes(w.getnframes())
    except (wave.Error, EOFError, struct.error, OSError) as e:
        raise SoundSetError(f"{path.name}: unreadable WAV ({e})") from e


def expected_channels(filename: str, stock: Path | None = None) -> int:
    if stock is not None:
        try:
            return read_wav(stock)[0]
        except SoundSetError:
            pass
    return 2 if filename.casefold() in STEREO else 1


def check_wav(path: Path, channels: int | None = None) -> None:
    """Raise SoundSetError unless the file is a non-empty PCM 22050 Hz 16-bit WAV of the right channel count."""
    ch, rate, bits, pcm = read_wav(path)
    if not pcm:
        raise SoundSetError(f"{path.name}: empty (0 samples)")
    if bits != 16:
        raise SoundSetError(f"{path.name}: {bits}-bit (needs 16-bit)")
    if rate != RATE:
        raise SoundSetError(f"{path.name}: {rate} Hz (needs {RATE} Hz)")
    want = channels or expected_channels(path.name)
    if ch != want:
        raise SoundSetError(f"{path.name}: {ch} channel(s), the game expects {want}")


def write_wav(path: Path, channels: int, rate: int, pcm: bytes):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


def normalize(src: Path, dest: Path) -> str | None:
    """Rewrite src as a clean 16-bit PCM WAV at dest (drops extra RIFF chunks, widens 8-bit).

    Returns a note for something that was converted, raises SoundSetError for what can't be fixed
    (wrong rate, empty, unsupported depth, wrong channel count for a known stock file)."""
    ch, rate, bits, pcm = read_wav(src)
    note = None
    if not pcm:
        raise SoundSetError(f"{src.name}: empty (0 samples)")
    if bits == 8:
        pcm = b"".join(struct.pack("<h", (b - 128) << 8) for b in pcm)
        note = f"{src.name}: converted 8-bit to 16-bit"
    elif bits != 16:
        raise SoundSetError(f"{src.name}: {bits}-bit is not supported")
    if rate != RATE:
        raise SoundSetError(f"{src.name}: {rate} Hz (needs {RATE} Hz; resample it first)")
    want = expected_channels(src.name)
    if ch == 2 and want == 1:
        n = len(pcm) // 4
        vals = struct.unpack(f"<{n * 2}h", pcm[: n * 4])
        pcm = struct.pack(f"<{n}h", *((vals[i] + vals[i + 1]) // 2 for i in range(0, n * 2, 2)))
        ch = 1
        note = f"{src.name}: stereo folded to mono"
    elif ch != want:
        raise SoundSetError(f"{src.name}: {ch} channel(s), the game expects {want}")
    write_wav(dest, ch, rate, pcm)
    return note


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _wavs(folder: Path) -> list[Path]:
    try:
        return sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.casefold() == ".wav"),
                      key=lambda p: p.name.casefold())
    except OSError:
        return []


def _atomic_copy(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".aimod-tmp", dir=target.parent)
    os.close(fd)
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, target)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _write_json(path: Path, data: dict):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _safe_name(name: str) -> str:
    safe = "".join(c for c in name if c not in '<>:"/\\|?*' and ord(c) >= 32).strip(" .")
    return safe or "Imported sounds"


# ---------------------------------------------------------------- manager

class SoundSetManager:
    def __init__(self, app_dir: str, running: Callable[[], bool] = nr2003_running,
                 bundled_root: Path | None = None):
        self.app_dir = Path(app_dir)
        self.user_root = self.app_dir / "sound_sets"
        self.bundled_root = bundled_root if bundled_root is not None else _bundled_root()
        self._running = running

    def _roots(self) -> tuple[Path, Path]:
        return self.user_root, self.bundled_root

    def names(self) -> list[str]:
        found = set()
        for root in self._roots():
            try:
                found.update(p.name for p in root.iterdir() if p.is_dir() and _wavs(p))
            except OSError:
                continue
        return [ORIGINAL, *sorted(found, key=str.casefold)]

    def folder(self, name: str) -> Path | None:
        for root in self._roots():
            p = root / name
            if p.is_dir() and _wavs(p):
                return p
        return None

    def resolve(self, name: str) -> dict[str, Path]:
        """filename -> source path, layering the set over its parent chain (nearest set wins)."""
        chain, seen, cur = [], set(), name
        while cur:
            if cur.casefold() in seen:
                raise SoundSetError(f"Sound set '{name}' has a parent loop at '{cur}'.")
            seen.add(cur.casefold())
            p = self.folder(cur)
            if p is None:
                raise SoundSetError(f"Sound set '{cur}' is not installed in AIMOD.")
            chain.append(p)
            cur = str(_read_json(p / SET_JSON).get("parent") or "").strip()
        out: dict[str, Path] = {}
        for p in reversed(chain):
            for w in _wavs(p):
                out[w.name] = w
        return out

    def import_folder(self, source: str, name: str = "", parent: str = "", notes: str = "") -> tuple[str, list[str]]:
        """Copy a folder of the user's own WAVs into sound_sets\\<name>, normalised. Returns (name, warnings)."""
        src = Path(source)
        wavs = _wavs(src)
        if not wavs:
            raise SoundSetError("The selected folder does not contain any WAV files.")
        safe = _safe_name(name or src.name)
        if parent and self.folder(parent) is None:
            raise SoundSetError(f"Base set '{parent}' is not installed.")
        self.user_root.mkdir(parents=True, exist_ok=True)
        staging = self.user_root / f".{safe}.importing"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        warnings: list[str] = []
        try:
            for w in wavs:
                try:
                    note = normalize(w, staging / w.name)
                except SoundSetError as e:
                    warnings.append(f"skipped {e}")
                    continue
                if note:
                    warnings.append(note)
            if not _wavs(staging):
                raise SoundSetError("None of the WAV files could be imported: " + "; ".join(warnings))
            meta = {"name": safe, "parent": parent or None, "notes": notes}
            if warnings:
                meta["import_warnings"] = warnings
            _write_json(staging / SET_JSON, meta)
            dest = self.user_root / safe
            if dest.exists():
                for old in _wavs(dest):
                    old.unlink()
            else:
                dest.mkdir()
            for f in staging.iterdir():
                os.replace(f, dest / f.name)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return safe, warnings

    @staticmethod
    def _paths(game_dir: str) -> tuple[Path, Path, Path]:
        root = Path(game_dir) / "sound"
        return root, root / BACKUP_DIR, root / MANIFEST

    def status(self, game_dir: str) -> dict:
        _, _, manifest = self._paths(game_dir)
        m = _read_json(manifest)
        return {"active": m.get("active", ORIGINAL), "written": sorted(m.get("written", {}))}

    def validate(self, name: str, game_dir: str) -> dict[str, Path]:
        """Resolve and check every file of the set; raises SoundSetError listing all problems."""
        files = self.resolve(name)
        root, backup, _ = self._paths(game_dir)
        problems = []
        for fn, src in files.items():
            stock = backup / fn if (backup / fn).is_file() else (root / fn if (root / fn).is_file() else None)
            try:
                check_wav(src, expected_channels(fn, stock))
            except SoundSetError as e:
                problems.append(str(e))
        if problems:
            raise SoundSetError(f"Sound set '{name}' was not applied:\n" + "\n".join(problems))
        return files

    def apply(self, game_dir: str, name: str) -> str:
        if self._running():
            raise SoundSetError("NR2003 is running. Close it first: it loads its sounds at startup.")
        root, backup, manifest_path = self._paths(game_dir)
        if not root.is_dir():
            raise SoundSetError(f"NR2003 sound folder was not found: {root}")
        files = {} if name == ORIGINAL else self.validate(name, game_dir)

        manifest = _read_json(manifest_path)
        written: dict[str, str] = dict(manifest.get("written", {}))     # filename -> sha256 AIMOD wrote
        first_time = not backup.is_dir()

        # 1. Back up originals once: the whole loose bank on first use, then any file about to be overwritten.
        backup.mkdir(exist_ok=True)
        if first_time:
            for w in _wavs(root):
                if w.name not in written:
                    _atomic_copy(w, backup / w.name)
        for fn in files:
            tgt, bak = root / fn, backup / fn
            if tgt.is_file() and fn not in written and not bak.is_file():
                _atomic_copy(tgt, bak)

        # 2. Journal first so an interrupted run is still recoverable (every possible file is listed).
        pending = dict(written)
        for fn in files:
            pending.setdefault(fn, "")
        _write_json(manifest_path, {"active": manifest.get("active", ORIGINAL), "written": pending,
                                    "applying": name})

        # 3. Put back originals for files no longer supplied (only AIMOD-written ones, untouched by the user).
        for fn, digest in list(written.items()):
            if fn in files:
                continue
            tgt, bak = root / fn, backup / fn
            if tgt.is_file() and digest and _sha(tgt) != digest:
                log.warning("Sound set: %s was changed outside AIMOD, leaving it", fn)
                written.pop(fn)
                continue
            if bak.is_file():
                _atomic_copy(bak, tgt)
            else:
                try:
                    tgt.unlink()
                except FileNotFoundError:
                    pass
            written.pop(fn)

        # 4. Write the set.
        for fn, src in files.items():
            _atomic_copy(src, root / fn)
            written[fn] = _sha(root / fn)

        # 5. Completeness check: nothing from the stock bank may be missing.
        for bak in _wavs(backup):
            if not (root / bak.name).is_file():
                log.warning("Sound set: %s was missing, restored from backup", bak.name)
                _atomic_copy(bak, root / bak.name)

        _write_json(manifest_path, {"active": name, "written": written})
        if name == ORIGINAL:
            return "Original sounds restored. Start NR2003 to load them."
        return f"'{name}' applied ({len(files)} files). Start NR2003 to load it."

    def restore(self, game_dir: str) -> str:
        return self.apply(game_dir, ORIGINAL)

    @staticmethod
    def set_for(series: str, mapping: dict | None) -> str:
        return str((mapping or {}).get(series) or ORIGINAL)
