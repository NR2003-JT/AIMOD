"""Sound set manager tests. Everything runs on temp folders, never the live game folder."""
import json
import tempfile
import unittest
import wave
from pathlib import Path

from aimod import soundsets as ss


def make_wav(path: Path, channels=1, rate=22050, bits=16, frames=100, fill=1):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(bits // 8)
        w.setframerate(rate)
        w.writeframes((bytes([fill]) * (bits // 8)) * channels * frames)


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = Path(self._t.name)
        self.game = self.tmp / "game"
        self.snd = self.game / "sound"
        # stock loose bank: 3 mono + 1 stereo
        make_wav(self.snd / "hit_hard.wav", fill=1)
        make_wav(self.snd / "skid.wav", fill=1)
        make_wav(self.snd / "winnf_idle.wav", fill=1)
        make_wav(self.snd / "winston_idle.wav", channels=2, fill=1)
        (self.snd / "unrelated.txt").write_text("keep")
        self.running = False
        self.m = ss.SoundSetManager(str(self.tmp / "app"), running=lambda: self.running,
                                    bundled_root=self.tmp / "bundled")

    def tearDown(self):
        self._t.cleanup()

    def mkset(self, name, files, parent=None, root=None):
        root = root or self.m.user_root
        for fn, kw in files.items():
            make_wav(root / name / fn, **kw)
        if parent:
            (root / name / "set.json").write_text(json.dumps({"parent": parent}))

    def data(self, fn):
        with wave.open(str(self.snd / fn)) as w:
            return w.readframes(1)[:1]


class TestResolve(Base):
    def test_layering_and_fallback(self):
        self.mkset("full", {"hit_hard.wav": dict(fill=2), "skid.wav": dict(fill=2)})
        self.mkset("part", {"skid.wav": dict(fill=3)}, parent="full")
        r = self.m.resolve("part")
        self.assertEqual(set(r), {"hit_hard.wav", "skid.wav"})
        self.assertEqual(r["skid.wav"].parent.name, "part")
        self.assertEqual(r["hit_hard.wav"].parent.name, "full")

    def test_parent_loop_and_missing(self):
        self.mkset("a", {"skid.wav": {}}, parent="b")
        self.mkset("b", {"skid.wav": {}}, parent="a")
        with self.assertRaises(ss.SoundSetError):
            self.m.resolve("a")
        self.mkset("c", {"skid.wav": {}}, parent="nope")
        with self.assertRaises(ss.SoundSetError):
            self.m.resolve("c")

    def test_names_include_bundled(self):
        self.mkset("b1", {"skid.wav": {}}, root=self.tmp / "bundled")
        self.assertIn("b1", self.m.names())
        self.assertEqual(self.m.names()[0], ss.ORIGINAL)


class TestApply(Base):
    def test_partial_set_keeps_bank_complete(self):
        self.mkset("part", {"skid.wav": dict(fill=5), "extra.wav": dict(fill=5)})
        self.m.apply(str(self.game), "part")
        for f in ("hit_hard", "skid", "winnf_idle", "winston_idle", "extra"):
            self.assertTrue((self.snd / f"{f}.wav").is_file(), f)
        self.assertEqual(self.data("skid.wav"), bytes([5]))
        self.assertEqual(self.data("hit_hard.wav"), bytes([1]))

    def test_switch_and_restore(self):
        self.mkset("one", {"skid.wav": dict(fill=5), "extra.wav": dict(fill=5)})
        self.mkset("two", {"hit_hard.wav": dict(fill=6)})
        self.m.apply(str(self.game), "one")
        self.m.apply(str(self.game), "two")
        self.assertEqual(self.data("skid.wav"), bytes([1]))      # original back
        self.assertFalse((self.snd / "extra.wav").exists())      # AIMOD-added file removed
        self.assertEqual(self.data("hit_hard.wav"), bytes([6]))
        self.m.restore(str(self.game))
        self.assertEqual(self.data("hit_hard.wav"), bytes([1]))
        self.assertEqual((self.snd / "unrelated.txt").read_text(), "keep")
        self.assertEqual(self.m.status(str(self.game))["active"], ss.ORIGINAL)

    def test_backup_made_once(self):
        self.mkset("one", {"skid.wav": dict(fill=5)})
        self.m.apply(str(self.game), "one")
        bak = self.snd / ss.BACKUP_DIR
        self.assertEqual({p.name for p in bak.iterdir()},
                         {"hit_hard.wav", "skid.wav", "winnf_idle.wav", "winston_idle.wav"})
        self.m.apply(str(self.game), "one")
        with wave.open(str(bak / "skid.wav")) as w:
            self.assertEqual(w.readframes(1)[:1], bytes([1]))    # still the original, not the set's

    def test_never_deletes_unmanaged(self):
        make_wav(self.snd / "mine.wav")
        self.mkset("one", {"skid.wav": {}})
        self.m.apply(str(self.game), "one")
        self.m.restore(str(self.game))
        self.assertTrue((self.snd / "mine.wav").is_file())

    def test_user_edited_file_left_alone_on_removal(self):
        self.mkset("one", {"extra.wav": dict(fill=5)})
        self.mkset("two", {"skid.wav": dict(fill=6)})
        self.m.apply(str(self.game), "one")
        make_wav(self.snd / "extra.wav", fill=9)                 # user replaced it
        self.m.apply(str(self.game), "two")
        self.assertTrue((self.snd / "extra.wav").is_file())

    def test_refuses_while_running(self):
        self.mkset("one", {"skid.wav": {}})
        self.running = True
        with self.assertRaises(ss.SoundSetError):
            self.m.apply(str(self.game), "one")
        self.assertFalse((self.snd / ss.BACKUP_DIR).exists())

    def test_invalid_wav_writes_nothing(self):
        self.mkset("bad", {"skid.wav": dict(fill=5), "hit_hard.wav": dict(bits=8)})
        with self.assertRaises(ss.SoundSetError) as cm:
            self.m.apply(str(self.game), "bad")
        self.assertIn("8-bit", str(cm.exception))
        self.assertEqual(self.data("skid.wav"), bytes([1]))
        self.assertFalse((self.snd / ss.MANIFEST).exists())

    def test_channel_and_rate_and_empty_rejected(self):
        for kw in (dict(channels=2), dict(rate=44100), dict(frames=0)):
            self.mkset("x", {"skid.wav": kw})
            with self.assertRaises(ss.SoundSetError, msg=str(kw)):
                self.m.apply(str(self.game), "x")
            (self.m.user_root / "x" / "skid.wav").unlink()

    def test_stereo_file_must_be_stereo(self):
        self.mkset("x", {"winston_idle.wav": dict(channels=1)})
        with self.assertRaises(ss.SoundSetError):
            self.m.apply(str(self.game), "x")
        self.mkset("y", {"winston_idle.wav": dict(channels=2, fill=7)})
        self.m.apply(str(self.game), "y")

    def test_missing_stock_file_restored_from_backup(self):
        self.mkset("one", {"skid.wav": {}})
        self.m.apply(str(self.game), "one")
        (self.snd / "winnf_idle.wav").unlink()                   # something removed it
        self.m.apply(str(self.game), "one")
        self.assertTrue((self.snd / "winnf_idle.wav").is_file())


class TestImport(Base):
    def test_import_converts_8bit_and_strips_chunks(self):
        src = self.tmp / "pack"
        make_wav(src / "scr_pave.wav", bits=8, fill=200)
        make_wav(src / "skid.wav")
        make_wav(src / "bad_rate.wav", rate=44100)
        name, warns = self.m.import_folder(str(src), name="My pack")
        self.assertEqual(name, "My pack")
        self.assertTrue(any("8-bit" in w for w in warns))
        self.assertTrue(any("44100" in w for w in warns))
        ss.check_wav(self.m.user_root / name / "scr_pave.wav")
        self.assertFalse((self.m.user_root / name / "bad_rate.wav").exists())
        self.m.apply(str(self.game), name)

    def test_import_empty_folder(self):
        (self.tmp / "empty").mkdir()
        with self.assertRaises(ss.SoundSetError):
            self.m.import_folder(str(self.tmp / "empty"))


class TestMapping(unittest.TestCase):
    def test_set_for(self):
        self.assertEqual(ss.SoundSetManager.set_for("GN70SS", {"GN70SS": "Petty"}), "Petty")
        self.assertEqual(ss.SoundSetManager.set_for("Cup12", {"GN70SS": "Petty"}), ss.ORIGINAL)
        self.assertEqual(ss.SoundSetManager.set_for("x", None), ss.ORIGINAL)


if __name__ == "__main__":
    unittest.main()
