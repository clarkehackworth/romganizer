"""Tests for the dedup helper scripts and the romganizer version flag.

The planner scripts (find_dups.py, plan_dedup.py) had no tests at all -- their
pure functions are exercised here offline; nothing reads a real disk image.
"""
import sys
from pathlib import Path
import tempfile

import find_dups
import plan_dedup
import romganizer


# --- find_dups.digest / narrow -----------------------------------------------

def test_digest_returns_md5_of_bytes():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / 'a.bin'
        p.write_bytes(b'hello world')
        assert find_dups.digest(p) == __import__('hashlib').md5(b'hello world').hexdigest()


def test_digest_limit_reads_only_a_prefix():
    # The limit is checked after each 1 MB chunk, so it only bites on files
    # larger than that -- a small file is read whole regardless.
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / 'big.bin'
        data = b'abcdef' * 300000          # 1.8 MB
        p.write_bytes(data)
        full = find_dups.digest(p)
        limited = find_dups.digest(p, limit=32)
        assert limited is not None and limited != full


def test_digest_missing_file_returns_none():
    assert find_dups.digest(Path('/no/such/file.bin')) is None


def test_narrow_splits_groups_and_drops_singletons():
    with tempfile.TemporaryDirectory() as td:
        t = Path(td)
        a, b = t / 'a.bin', t / 'b.bin'      # identical pair
        c = t / 'c.bin'                      # unique
        for f in (a, b, c):
            f.write_bytes(b'same bytes' if f is not c else b'other bytes')
        groups = find_dups.narrow([[a, b, c]], 1 << 20, "prefix")
        # {a, b} survive as one group; {c} collapses to a singleton and is dropped.
        assert len(groups) == 1
        assert {p.name for p in groups[0]} == {'a.bin', 'b.bin'}


def test_narrow_drops_groups_with_a_missing_file():
    # A group all of whose digests are None (unreadable) must not survive.
    result = find_dups.narrow([[Path('/no/a.bin'), Path('/no/b.bin')]], 1024, "x")
    assert result == []


# --- plan_dedup categories ---------------------------------------------------

class TestPlanDedupCategories:
    def test_cd_track_detected_by_name(self):
        g = ['/roms/psx/Game (USA) (Track 1).bin']
        assert plan_dedup.is_cd_track(g) is True

    def test_not_a_cd_track(self):
        assert plan_dedup.is_cd_track(['/roms/psx/Game (USA).iso']) is False

    def test_chd_detected_by_extension(self):
        assert plan_dedup.is_chd(['/mame/roms/streetfighter.chd']) is True
        assert plan_dedup.is_chd(['/mame/roms/streetfighter.zip']) is False

    def test_firmware_by_parent_folder(self):
        g = ['/storage/bios/scph39001.bin', '/storage/bios/scph39001.bin']
        assert plan_dedup.is_firmware(g) is True
        assert plan_dedup.is_firmware(['/storage/roms/psx/scph39001.bin']) is False

    def test_cue_named_member(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / 'game.cue').write_text('FILE "Track 1.bin" BINARY')
            g = [str(d / 'Track 1') + '.bin']
            assert plan_dedup.is_cue_named(g) is True

    def test_csv_sheet_backs_an_iso(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / 'game.gdi').write_text('all things,3 4 0 0 0 0 grove.bin')
            g = [str(d / 'grove.bin')]
            assert plan_dedup.is_cue_named(g) is True


class TestPlanDedupScore:
    def test_real_title_beats_disc_id(self):
        id_only = plan_dedup.score('/roms/psx/SLUS-21555 (1.02).iso')
        titled = plan_dedup.score('/roms/psx/Lara Croft Tomb Raider (USA).iso')
        assert titled > id_only

    def test_region_tag_adds_a_point(self):
        tagged = plan_dedup.score('/r/Game (USA).iso')
        untagged = plan_dedup.score('/r/Game.iso')
        assert tagged > untagged

    def test_equal_stems_break_by_rarity(self):
        shallow = plan_dedup.score('/r/game.iso')
        deep = plan_dedup.score('/very/long/path/game.iso')
        assert shallow > deep  # keep the shallower copy


class TestPlanDedupSh:
    def test_plain_path_quoted(self):
        assert plan_dedup.sh('/data/roms/a.bin') == "'/data/roms/a.bin'"

    def test_single_quote_is_escaped(self):
        # A real rom name can contain an apostrophe ("Danger Zone's Ride").
        escaped = plan_dedup.sh("/roms/snes/Danger Zone's Ride.smc")
        assert "'" in escaped       # wrapped in quotes
        assert "\\'" in escaped     # inner quote escaped for the shell


# --- version -----------------------------------------------------------------

def test_version_flag_prints_version_and_exits_zero():
    import subprocess
    script = str(Path(__file__).parent / 'romganizer.py')
    r = subprocess.run([sys.executable, script, '--version'],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert romganizer.__version__ in r.stdout


def test_pyproject_agrees_with_module_version():
    import tomllib
    pyproject = tomllib.loads((Path(__file__).parent / 'pyproject.toml').read_text())
    assert pyproject['project']['version'] == romganizer.__version__