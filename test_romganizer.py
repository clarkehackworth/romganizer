"""Self-check: python3 test_romganizer.py (no framework, asserts only)."""
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

from romganizer import (CONTEXT_HINTS, SYSTEM_MAPPING, archive_crcs, bios_root, in_saves, quick_compare,
                        build_destination_path, classify, dat_lookup,
                        determine_system, fetch_dat, header_size, is_bios_name,
                        is_smd, parse_dat, smd_md5, sniff)


def touch(path, data=b'rom'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_routing():
    with tempfile.TemporaryDirectory() as td:
        src = Path(td)
        # unambiguous extensions
        assert determine_system(touch(src / 'a.nes')) == 'nes'
        assert determine_system(touch(src / 'a.sfc')) == 'snes'
        assert determine_system(touch(src / 'a.ws')) == 'wonderswan'
        # ambiguous extensions must follow hints, not dict order
        assert determine_system(touch(src / 'PSX' / 'g.bin')) == 'psx'
        assert determine_system(touch(src / 'Sega CD' / 'g.bin')) == 'segacd'
        assert determine_system(touch(src / 'Atari 2600' / 'g.bin')) == 'atari2600'
        assert determine_system(touch(src / 'Wii' / 'g.iso')) == 'wii'
        assert determine_system(touch(src / 'Wii U' / 'g.iso')) == 'wiiu'
        assert determine_system(touch(src / 'PS2' / 'g.iso')) == 'ps2'
        assert determine_system(touch(src / 'GameCube' / 'g.iso')) == 'ngc'
        assert determine_system(touch(src / 'Xbox 360' / 'g.iso')) == 'xbox360'
        # no hint anywhere -> flagged, not silently misfiled
        assert determine_system(touch(src / 'loose' / 'g.iso')) == 'ambiguous'
        assert determine_system(touch(src / 'loose' / 'g.xyz')) == 'unknown'
        # unrecognized extension: the path is the only evidence, so use it
        assert determine_system(touch(src / 'Sega CD' / 'g.xyz')) == 'segacd'
        assert determine_system(touch(src / 'SNES' / 'sub' / 'g.xyz')) == 'snes'
        assert determine_system(touch(src / 'PSX' / 'noext')) == 'psx'
        # a filename is a title, not a system: "Arcade Mode" and ".arcade." name
        # nothing, so only the directories above vote
        assert determine_system(touch(
            src / 'TOP 100 PLAYSTATION1 GAMES' / 'Gran Turismo 2 (USA)' /
            'Gran Turismo 2 (USA) (Arcade Mode).cue')) == 'psx'
        assert determine_system(touch(
            src / 'xbox360' / 'tmnt-COMPLEX' / 'L.dash.xex.arcade.xzp')) == 'xbox360'
        assert determine_system(touch(src / 'roms' / 'wii sports.iso')) == 'ambiguous'
        # .7z is a container, not arcade: only the path says what is inside
        assert determine_system(touch(
            src / 'Sony PlayStation 1 Redump' / 'Games' / 'Tomb Raider (USA).7z')) == 'psx'
        assert determine_system(touch(src / 'mame' / 'sf2.7z')) == 'arcade'
        # ...but artwork and readmes never become roms, whatever folder they're in
        assert determine_system(touch(src / 'PSX' / 'cover.jpg')) == 'unknown'
        assert determine_system(touch(src / 'PSX' / 'readme.txt')) == 'unknown'
        # bios
        assert determine_system(touch(src / 'scph1001.bin')) == 'bios'
        # split archive volumes are not dumps; the lookalikes around them are
        assert determine_system(touch(src / 'arcade' / 'Coll.zip.001')) == 'unknown'
        assert determine_system(touch(src / 'arcade' / 'Coll.zip.016')) == 'unknown'
        # ".z80" is a ZX Spectrum snapshot, not a split-zip volume
        assert determine_system(touch(src / 'roms' / 'zxspectrum' / 'Bomb Jack.z80')) == 'zxspectrum'
        assert determine_system(touch(src / 'mame' / 'sf2.zip')) == 'arcade'
        assert determine_system(touch(src / 'TombRaiderDump' / 'SLUS_001.52')) == 'unknown'
        # .xex: Xbox 360 vs Atari 8-bit, decided by the surrounding dir
        assert determine_system(touch(src / 'Atari 2600 5200 7800 XEGS' / 'g.xex')) == 'atari800'
        assert determine_system(touch(src / 'Xbox 360' / 'g.xex')) == 'xbox360'
        assert determine_system(touch(src / 'g.a52')) == 'atari5200'
        assert determine_system(touch(src / 'Atari ST' / 'g.st')) == 'atarist'
        assert determine_system(touch(src / 'loose' / 'g.msa')) == 'atarist'
        # .car is an 800 or a 5200 cart; the folder decides
        assert determine_system(touch(src / 'Atari 5200' / 'g.car')) == 'atari5200'
        assert determine_system(touch(src / 'Atari 800' / 'g.car')) == 'atari800'
        assert determine_system(touch(src / 'loose' / 'g.car')) == 'ambiguous'
        # nearest directory beats a broader ancestor
        assert determine_system(touch(src / 'PSX' / 'Sega CD' / 'g.bin')) == 'segacd'


def test_conflicts():
    with tempfile.TemporaryDirectory() as td:
        src = Path(td)
        # unique extension vs a directory naming another system: extension wins,
        # conflict is recorded
        system, evidence = classify(touch(src / 'SNES' / 'g.nes'))
        assert system == 'nes', evidence
        assert evidence[0].startswith('conflict'), evidence
        # agreeing signals are not a conflict
        system, evidence = classify(touch(src / 'SNES' / 'g.sfc'))
        assert system == 'snes' and not evidence[0].startswith('conflict'), evidence
        # a shared extension never conflicts with the directory; it defers to it
        system, evidence = classify(touch(src / 'PSX' / 'g.bin'))
        assert system == 'psx' and not evidence[0].startswith('conflict'), evidence
        # no directory hint at all -> nothing to disagree with
        _, evidence = classify(touch(src / 'g.nes'))
        assert not evidence[0].startswith('conflict'), evidence


def make_iso(path, entries, sector=2048, skip=0):
    """Minimal ISO9660: a volume descriptor at sector 16 pointing at a root
    directory at sector 18, with each file's data one sector apart from 20."""
    img = bytearray(sector * 40)

    def put(lba, data):
        for i in range(0, max(len(data), 1), 2048):
            chunk = data[i:i + 2048]
            at = (lba + i // 2048) * sector + skip
            img[at:at + len(chunk)] = chunk

    def record(lba, size, name):
        n = len(name)
        rec = bytearray(33 + n + (n % 2 == 0))
        rec[0] = len(rec)
        rec[2:6] = lba.to_bytes(4, 'little')
        rec[10:14] = size.to_bytes(4, 'little')
        rec[32] = n
        rec[33:33 + n] = name
        return rec

    pvd = bytearray(2048)
    pvd[1:6] = b'CD001'
    pvd[156:190] = record(18, 2048, b'\x00')
    put(16, pvd)

    root = bytearray()
    for i, (name, content) in enumerate(entries.items()):
        root += record(20 + i, len(content), name)
        put(20 + i, content)
    put(18, root)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(img)
    return path


def test_magic_bytes():
    with tempfile.TemporaryDirectory() as td:
        src = Path(td)
        # header magic identifies the file regardless of where it sits
        assert determine_system(touch(src / 'x.rom', b'NES\x1a' + b'\0' * 16)) == 'nes'
        assert determine_system(touch(src / 'x.rom', b'\x80\x37\x12\x40' + b'\0' * 16)) == 'n64'
        gba = b'\0' * 4 + b'\x24\xff\xae\x51\x69\x9a\xa2\x21'
        assert determine_system(touch(src / 'x.rom', gba)) == 'gba'
        gcm = b'\0' * 0x1c + b'\xc2\x33\x9f\x3d'
        assert determine_system(touch(src / 'loose' / 'x.iso', gcm)) == 'ngc'
        wii = b'\0' * 0x18 + b'\x5d\x1c\x9e\xa3'
        assert determine_system(touch(src / 'PS2' / 'x.iso', wii)) == 'wii'  # beats the folder
        # TMSS requires the 0x100 field to start with SEGA; the rest varies
        for name in (b'SEGA MEGA DRIVE ', b'SEGA GENESIS    ', b'SEGA EVERDRIVE  '):
            assert determine_system(touch(src / 'x.zzz', b'\0' * 0x100 + name)) == 'megadrive'
        assert determine_system(touch(src / 'x.zzz', b'\0' * 0x100 + b' SEGA GENESIS')) == 'megadrive'
        assert determine_system(touch(src / 'x.zzz', b'\0' * 0x100 + b'SEGA 32X    ')) == 'sega32x'
        # .md is both Markdown and a Mega Drive dump; the bytes break the tie
        assert determine_system(touch(src / 'GEN' / 'g.md', b'\0' * 0x100 + b'SEGA GENESIS')) == 'megadrive'
        assert determine_system(touch(src / 'GEN' / 'README.md', b'# notes\n')) == 'unknown'
        # sega 8-bit: header offset floats, last byte's high nibble picks the machine
        for at in (0x1ff0, 0x3ff0, 0x7ff0):
            sms = bytearray(at + 0x10)
            sms[at:at + 8] = b'TMR SEGA'
            sms[at + 0x0f] = 0x4c
            assert determine_system(touch(src / 'x.zzz', bytes(sms))) == 'mastersystem'
            sms[at + 0x0f] = 0x6c
            assert determine_system(touch(src / 'x.zzz', bytes(sms))) == 'gamegear'
        # neo geo pocket, mono vs color
        ngp = bytearray(0x30)
        ngp[0x0a:0x1c] = b'BY SNK CORPORATION'
        assert determine_system(touch(src / 'x.zzz', bytes(ngp))) == 'ngp'
        ngp[0x23] = 0x10
        assert determine_system(touch(src / 'x.zzz', bytes(ngp))) == 'ngpc'
        # headerless fds disk, gbc flag, switch cartridge
        assert determine_system(touch(src / 'x.zzz', b'\x01*NINTENDO-HVC*')) == 'fds'
        gbc = bytearray(0x150)
        gbc[0x104:0x10c] = b'\xce\xed\x66\x66\xcc\x0d\x00\x0b'
        assert determine_system(touch(src / 'x.zzz', bytes(gbc))) == 'gb'
        gbc[0x143] = 0xc0
        assert determine_system(touch(src / 'x.zzz', bytes(gbc))) == 'gbc'
        assert determine_system(touch(src / 'x.zzz', b'\0' * 0x100 + b'HEAD')) == 'switch'
        # the Xbox media marker doesn't say which Xbox: needs the folder
        xb = b'\0' * 0x10000 + b'MICROSOFT*XBOX*MEDIA'
        assert determine_system(touch(src / 'loose' / 'x.iso', xb)) == 'ambiguous'
        assert determine_system(touch(src / 'Xbox 360' / 'x.iso', xb)) == 'xbox360'
        # a mislabeled file is filed by its contents, and the lie is recorded
        system, evidence = classify(touch(src / 'SNES' / 'Mario.nes', gba))
        assert system == 'gba', evidence
        assert evidence[0] == 'conflict: contents say gba, .nes says nes, path says snes', evidence


def test_iso9660_probe():
    with tempfile.TemporaryDirectory() as td:
        src = Path(td)
        for sector, skip in ((2048, 0), (2352, 16)):  # plain image and raw dump
            tag = f'{sector}'
            psx = make_iso(src / tag / 'a.iso', {b'SYSTEM.CNF': b'BOOT=cdrom:\\SLUS_007.57;1'},
                           sector, skip)
            ps2 = make_iso(src / tag / 'b.iso', {b'SYSTEM.CNF': b'BOOT2=cdrom0:\\SLUS_202.28;1'},
                           sector, skip)
            psp = make_iso(src / tag / 'c.iso', {b'UMD_DATA.BIN': b'x'}, sector, skip)
            assert determine_system(psx) == 'psx', (tag, classify(psx))
            assert determine_system(ps2) == 'ps2', (tag, classify(ps2))
            assert determine_system(psp) == 'psp', (tag, classify(psp))
        # a .cue is identified by the track it points at
        make_iso(src / 'g' / 'Game (Track 01).bin', {b'SYSTEM.CNF': b'BOOT=cdrom:\\SLUS_1;1'},
                 2352, 16)
        cue = touch(src / 'g' / 'Game.cue',
                    b'FILE "Game (Track 01).bin" BINARY\n  TRACK 01 MODE2/2352\n')
        assert determine_system(cue) == 'psx', classify(cue)
        # truncated image (all we get to see inside an archive): SYSTEM.CNF is
        # listed but its data is past the end. Claim nothing rather than psx.
        cut = make_iso(src / 't' / 'd.iso', {b'SYSTEM.CNF': b'BOOT2=cdrom0:\\SLUS_202.28;1'})
        cut.write_bytes(cut.read_bytes()[:20 * 2048])
        assert sniff(cut) == (), sniff(cut)
        assert determine_system(src / 'PS2' / 'd.iso') == 'ps2'  # path still decides


def make_cso(path, data, block=2048):
    """Pack bytes into a .cso: header, block index, then deflated blocks."""
    n = (len(data) + block - 1) // block
    data = data.ljust(n * block, b'\0')
    index, blobs, pos = [], [], 0x18 + (n + 1) * 4
    for i in range(n):
        c = zlib.compressobj(9, zlib.DEFLATED, -15)
        blob = c.compress(data[i * block:(i + 1) * block]) + c.flush()
        index.append(pos)
        blobs.append(blob)
        pos += len(blob)
    index.append(pos)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack('<4sIQIBBH', b'CISO', 0x18, n * block, block, 1, 0, 0)
                     + b''.join(struct.pack('<I', p) for p in index) + b''.join(blobs))
    return path


def test_cso_is_read_without_a_utility():
    with tempfile.TemporaryDirectory() as td:
        src = Path(td)
        iso = make_iso(src / 'raw.iso', {b'UMD_DATA.BIN': b'x'})
        assert determine_system(make_cso(src / 'g.cso', iso.read_bytes())) == 'psp'
        iso = make_iso(src / 'raw2.iso', {b'SYSTEM.CNF': b'BOOT2=cdrom0:\\SLUS_1;1'})
        assert determine_system(make_cso(src / 'h.cso', iso.read_bytes())) == 'ps2'
        # truncated garbage must not raise, just decline to answer
        (src / 'bad.cso').write_bytes(b'CISO' + b'\0' * 32)
        assert determine_system(src / 'bad.cso') in ('ambiguous', 'unknown')


def test_archive_members_are_sniffed():
    if not shutil.which('7z'):
        print('    (skipped: 7z not installed)')
        return
    with tempfile.TemporaryDirectory() as td:
        src = Path(td)
        touch(src / 'Sonic.zzz', b'\0' * 0x100 + b'SEGA MEGA DRIVE ')
        archive = src / 'Sonic.7z'
        subprocess.run(['7z', 'a', str(archive), str(src / 'Sonic.zzz')],
                       capture_output=True, check=True)
        assert determine_system(archive) == 'megadrive', classify(archive)
        # nothing recognizable inside -> the member's extension is the fallback
        touch(src / 'Mario.nes', b'not really a rom')
        other = src / 'Mario.7z'
        subprocess.run(['7z', 'a', str(other), str(src / 'Mario.nes')],
                       capture_output=True, check=True)
        assert determine_system(other) == 'nes', classify(other)


def test_every_hinted_system_is_mapped():
    # A system with hints but no extensions can never win a vote, so its files
    # fall through to "Not a recognized ROM extension" (how .a52 got skipped).
    unmapped = [s for s, _ in CONTEXT_HINTS if s not in SYSTEM_MAPPING]
    assert not unmapped, unmapped


def test_disc_grouping():
    dest = Path('/lib')
    _, d1 = build_destination_path(dest, 'psx', 'Game (Disc 1).cue')
    _, d2 = build_destination_path(dest, 'psx', 'Game (Disc 2).cue')
    _, t1 = build_destination_path(dest, 'psx', 'Game (Track 01).bin')
    assert d1.parent == d2.parent == t1.parent, (d1, d2, t1)
    assert d1.parent == dest / 'roms' / 'psx' / 'Game'
    # flat systems get no per-game folder
    _, n = build_destination_path(dest, 'nes', 'Game.nes')
    assert n == dest / 'roms' / 'nes' / 'Game.nes'
    # bios goes to its own root
    _, b = build_destination_path(dest, 'psx', 'scph1001.bin', is_bios=True)
    assert b == dest / 'bios' / 'psx' / 'scph1001.bin'


def test_game_folder_moves_as_a_unit():
    """Generic sidecars (Game.ini, pcsx.cfg) follow their game instead of colliding."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dest = root / 'src', root / 'dest'
        for game in ('Digimon World (USA)', 'Chrono Cross (USA)'):
            d = src / 'TOP 100 PLAYSTATION1 GAMES' / game
            touch(d / f'{game}.cue')
            touch(d / f'{game}.bin')
            touch(d / 'Game.ini')
            touch(d / 'pcsx.cfg')
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(dest), '--mode', 'copy'], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
        for game in ('Digimon World (USA)', 'Chrono Cross (USA)'):
            folder = dest / 'roms' / 'psx' / game
            for name in (f'{game}.cue', f'{game}.bin', 'Game.ini', 'pcsx.cfg'):
                assert (folder / name).exists(), (folder / name, r.stdout)
        assert not (dest / 'duplicates').exists(), r.stdout
        assert not (dest / 'roms' / 'psx' / 'Game').exists(), r.stdout
        assert r.stdout.count('as one unit') == 2, r.stdout

        # move mode does it as one folder operation, leaving no empty source dir
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(root / 'dest2'), '--mode', 'move'], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
        assert not (src / 'TOP 100 PLAYSTATION1 GAMES' / 'Digimon World (USA)').exists(), r.stdout


def test_archive_unpacked_for_systems_that_cannot_read_one():
    """A switch .zip is unpacked into its game folder; an arcade .zip stays a .zip."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dest = root / 'src', root / 'dest'
        game = src / 'Switch' / 'Metroid Dread (USA).zip'
        game.parent.mkdir(parents=True)
        with zipfile.ZipFile(game, 'w') as z:
            z.writestr('Metroid Dread (USA).nsp', 'rom' * 100)
        touch(src / 'mame' / 'sf2.zip', b'PK\x03\x04' + b'\0' * 60)

        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(dest), '--mode', 'copy'], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
        folder = dest / 'roms' / 'switch' / 'Metroid Dread (USA)'
        assert (folder / 'Metroid Dread (USA).nsp').exists(), r.stdout
        assert not (dest / 'roms' / 'switch' / 'Metroid Dread (USA).zip').exists(), r.stdout
        # a system whose emulator reads archives keeps the archive
        assert (dest / 'roms' / 'arcade' / 'sf2.zip').exists(), r.stdout

        # move mode consumes the archive, same as every other move
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(root / 'dest2'), '--mode', 'move'], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
        assert not game.exists(), r.stdout


def test_7z_is_unpacked_before_identification():
    """A .7z is opaque to grouping and dup detection, so it is unpacked first --
    except an arcade set, which MAME loads as the .7z itself."""
    if not shutil.which('7z'):
        print('    (skipped: 7z not installed)')
        return
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dest, build = root / 'src', root / 'dest', root / 'build'
        (src / 'PS2').mkdir(parents=True)
        (src / 'mame').mkdir(parents=True)
        touch(build / 'Sly Cooper (USA).iso', b'\0' * 3000)
        touch(build / 'sf2.rom', b'\0' * 500)
        for member, into in ((build / 'Sly Cooper (USA).iso', src / 'PS2' / 'Sly Cooper (USA).7z'),
                             (build / 'sf2.rom', src / 'mame' / 'sf2.7z')):
            subprocess.run(['7z', 'a', str(into), str(member)], capture_output=True, check=True)

        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(dest), '--mode', 'move'], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
        # the ps2 archive: unpacked, and the path hint above it survived the trip
        assert (dest / 'roms' / 'ps2' / 'Sly Cooper (USA)' / 'Sly Cooper (USA).iso').exists(), r.stdout
        assert not (src / 'PS2' / 'Sly Cooper (USA).7z').exists(), r.stdout
        # the arcade set stays sealed
        assert list(dest.rglob('sf2.7z')), r.stdout
        # staging never survives a clean run
        assert not (dest / '.romganizer-staging').exists(), r.stdout


def test_folder_marker_claims_a_whole_rip():
    """A PS3 rip has no telling extension anywhere; PS3_DISC.SFB names the folder."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dest = root / 'src', root / 'dest'
        rel = src / 'games' / 'ps3' / 'ICO.and.Shadow.of.the.Colossus.PS3 DUPLEX'
        rip = rel / 'BCES01097-[ICO Classics HD]'
        for f in ('PS3_DISC.SFB', 'PS3_GAME/PARAM.SFO', 'PS3_GAME/SND0.AT3',
                  'PS3_GAME/USRDIR/EBOOT.BIN', 'PS3_GAME/USRDIR/ICO.self'):
            touch(rip / f)
        for f in ('duplex.nfo', 'dpx-ico.sfv', 'dpx-ico.rar', 'dpx-ico.r00'):
            touch(rel / f)  # the scene set beside the rip belongs to the release
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(dest), '--mode', 'copy'], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
        out = dest / 'roms' / 'ps3' / 'ICO.and.Shadow.of.the.Colossus.PS3 DUPLEX'
        assert (out / 'dpx-ico.r00').exists(), r.stdout
        assert (out / 'BCES01097-[ICO Classics HD]' / 'PS3_GAME' / 'USRDIR' / 'EBOOT.BIN').exists(), r.stdout
        assert 'skipped: 0' in r.stdout, r.stdout


def test_wiiu_dump_travels_as_one_folder():
    """code/cos.xml names the title: without it the content/ tree gets filed one
    nameless asset at a time, flat into roms/wiiu/."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dest = root / 'src', root / 'dest'
        rip = src / 'WiiU (US) Library' / 'Mario Kart 8 (US)' / 'Mario Kart 8 (US)[Loadline]'
        for f in ('code/cos.xml', 'code/app.xml', 'code/Turbo.rpx',
                  'content/ai/AIRivalTable.byaml', 'content/audio/body/SNDG_B_Amb.bars',
                  'content/course/Gwii/course.kcl', 'meta/iconTex.tga'):
            touch(rip / f)
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(dest), '--mode', 'copy'], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
        out = dest / 'roms' / 'wiiu' / 'Mario Kart 8 (US)[Loadline]'
        assert (out / 'content' / 'course' / 'Gwii' / 'course.kcl').exists(), r.stdout
        assert (out / 'meta' / 'iconTex.tga').exists(), r.stdout
        assert not (dest / 'roms' / 'wiiu' / 'course.kcl').exists(), r.stdout


def test_a_bios_pack_is_never_a_game_folder():
    """A RetroArch system/ tree is loose firmware in named directories, which
    looks exactly like a game folder. .rom names no machine on its own either."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dest = root / 'src', root / 'dest'
        sysd = src / 'Retroarch BIOS Pack' / 'system'
        touch(sysd / 'Machines' / 'Shared Roms' / 'MSX.rom')
        touch(sysd / 'Machines' / 'SVI - Spectravideo SVI-318' / 'svi318.rom')
        touch(sysd / '5200.rom')
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(dest), '--mode', 'copy'], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
        assert (dest / 'bios' / 'Machines' / 'Shared Roms' / 'MSX.rom').exists(), r.stdout
        assert (dest / 'bios' / '5200.rom').exists(), r.stdout
        assert not (dest / 'roms' / 'atari2600').exists(), r.stdout


def test_a_part_file_is_not_a_dump():
    """An incomplete download rides along with a game folder unless it is
    dropped at the scan: moving it out from under the client breaks the transfer."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dest = root / 'src', root / 'dest'
        touch(src / 'psx' / 'Games' / 'Tomba (USA).cue', b'FILE "Tomba (USA).bin" BINARY\n')
        touch(src / 'psx' / 'Games' / 'Tomba (USA).bin')
        touch(src / 'psx' / 'Games' / 'Tom and Jerry (USA).7z.part')
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(dest), '--mode', 'copy'], capture_output=True, text=True)
        assert not list(dest.rglob('*.part')), r.stdout
        assert (src / 'psx' / 'Games' / 'Tom and Jerry (USA).7z.part').exists(), r.stdout


def test_game_ships_with_its_dlc():
    """A title and its unlock package are one unit; two unrelated titles are not."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dest = root / 'src', root / 'dest'
        d = src / 'ps3' / 'PS3_NPEB00870_Darkstalkers.Resurrection'
        touch(d / 'Darkstalkers.Resurrection_d30n_NPEB00870_v1.02.pkg')
        touch(d / 'Darkstalkers.Resurrection.Unlock_d30n_NPEB00870.pkg')
        touch(d / 'EP0102-NPEB00870_00-VAMPIREKEY000000.rap')
        # unrelated titles sharing a folder stay separate, sidecar or not
        lib = src / 'ps3' / 'store'
        touch(lib / 'Ape Escape.pkg')
        touch(lib / 'Tomb Raider.pkg')
        touch(lib / 'notes.txt')
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(dest), '--mode', 'copy'], capture_output=True, text=True)
        out = dest / 'roms' / 'ps3' / 'Darkstalkers.Resurrection_d30n_NPEB00870_v1.02'
        assert (out / 'EP0102-NPEB00870_00-VAMPIREKEY000000.rap').exists(), r.stdout
        assert (out / 'Darkstalkers.Resurrection.Unlock_d30n_NPEB00870.pkg').exists(), r.stdout
        assert r.stdout.count('as one unit') == 1, r.stdout
        assert (dest / 'roms' / 'ps3' / 'Ape Escape.pkg').exists(), r.stdout


def test_frontend_system_dirs_win():
    """roms/<system>/ is how frontends lay out a library, including for systems
    this script has never heard of. The name is taken as written."""
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / 'games' / 'arcade' / 'Backup' / 'roms'
        for n, want in (('intellivision/Astrosmash.int', 'intellivision'),
                        ('zxspectrum/Manic Miner.tzx', 'zxspectrum'),
                        ('openbor/beats.pak', 'openbor'),
                        ('colecovision/g.col', 'colecovision'),
                        ('x68000/g.bin', 'x68000'),
                        ('mame/sf2.zip', 'mame'),
                        ('ports/Doom/doom.wad', 'pc')):
            assert determine_system(touch(src / n)) == want, (n, classify(src / n))
        # contents lose to the directory: its owner put the file there on purpose
        fds = touch(src / 'fds' / 'Zelda.nes', b'NES\x1a' + b'\0' * 32)
        assert determine_system(fds) == 'fds', classify(fds)
        # but artwork and bios are still recognised for what they are
        assert determine_system(touch(src / 'nes' / 'media' / 'a.png')) == 'unknown'
        # a bios keeps its system, so it can be filed as bios/psx/ not bios/bios/
        bios = touch(src / 'psx' / 'scph1001.bin')
        assert determine_system(bios) == 'psx', classify(bios)
        assert is_bios_name(bios.name)
        assert build_destination_path(Path('/lib'), 'psx', bios.name, True)[1] == \
            Path('/lib/bios/psx/scph1001.bin')
        # nothing names the machine -> bios/, never bios/bios/
        assert build_destination_path(Path('/lib'), 'bios', 'x.zip', True)[1] == \
            Path('/lib/bios/x.zip')
        # save states are not dumps; skipping leaves them where the emulator wants
        saves = src.parent / 'saves' / 'mame' / 'cfg' / 'centiped.cfg'
        assert determine_system(touch(saves)) == 'unknown', classify(saves)


def test_pc_ports_are_not_arcade():
    """roms/ports/ is where pc ports live, whatever sits above it. Each game
    folder is one unit even though nothing in it has a telling extension."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dest = root / 'src', root / 'dest'
        # an "arcade" directory five levels up must not outvote "ports"
        ports = src / 'games' / 'arcade' / 'Backup' / 'roms' / 'ports'
        touch(ports / 'Doom' / 'doom.wad')
        touch(ports / 'Doom' / '_readme.txt')
        touch(ports / 'Quake' / 'id1' / 'pak0.pak')   # game dir is Quake, not id1
        touch(ports / 'Quake' / '_readme.txt')
        for f in ('WOLF3D.EXE', 'CATALOG.EXE', 'VSWAP.WL1', 'AUDIOT.WL1',
                  'GAMEMAPS.WL1', 'ORDER.FRM'):
            touch(ports / 'Wolfenstein 3D' / f)
        touch(src / 'games' / 'arcade' / 'Backup' / 'roms' / 'mame' / 'sf2.zip')
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(dest), '--mode', 'copy'], capture_output=True, text=True)
        pc = dest / 'roms' / 'pc'
        assert (pc / 'Doom' / 'doom.wad').exists(), r.stdout
        assert (pc / 'Quake' / 'id1' / 'pak0.pak').exists(), r.stdout
        # named for the directory, not for the shortest executable in it
        assert (pc / 'Wolfenstein 3D' / 'WOLF3D.EXE').exists(), r.stdout
        assert (pc / 'Wolfenstein 3D' / 'VSWAP.WL1').exists(), r.stdout
        # a sibling under roms/ keeps its own name, not the "arcade" ancestor's
        assert (dest / 'roms' / 'mame' / 'sf2.zip').exists(), r.stdout


def test_a_unit_is_named_for_its_directory():
    """Games ship identically-named binaries -- OpenLara.exe, Setup.exe -- so a
    unit named after a file inside it merges games that have nothing to do with
    each other. And a unit always gets a folder of its own."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dest = root / 'src', root / 'dest'
        for g in ('TombRaider', 'TombRaider2', 'TombRaider2Gold'):
            touch(src / 'pc' / 'OpenLaraPreconfigFR' / g / 'OpenLara.exe')
            touch(src / 'pc' / 'OpenLaraPreconfigFR' / g / 'LEVEL1.PHD')
            touch(src / 'pc' / 'OpenLaraPreconfigFR' / g / 'readme.txt')
        for g in ('Sweet Surrender VR [FFA Repacks]', 'Tomb Raider Anniversary - [DODI Repack]'):
            touch(src / 'pc' / g / 'Setup.exe')
            touch(src / 'pc' / g / 'data1.dd')
        # a flat system still gets a per-game folder, or two games' parts pile up
        # loose in roms/wiiu/ together
        for g in ('Donkey Kong Country (US)', 'Mario Kart 8 (US)'):
            for i in (1, 2, 3):
                touch(src / 'WiiU Library' / g / f'{g}[Loadline].part0{i}.rar')
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(dest), '--mode', 'copy'], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
        pc = dest / 'roms' / 'pc'
        for g in ('TombRaider', 'TombRaider2', 'TombRaider2Gold'):
            assert (pc / g / 'OpenLara.exe').exists(), (g, r.stdout)
        assert not (pc / 'OpenLara').exists(), r.stdout
        assert (pc / 'Sweet Surrender VR [FFA Repacks]' / 'Setup.exe').exists(), r.stdout
        assert (pc / 'Tomb Raider Anniversary - [DODI Repack]' / 'Setup.exe').exists(), r.stdout
        assert not (pc / 'Setup').exists(), r.stdout
        wiiu = dest / 'roms' / 'wiiu'
        for g in ('Donkey Kong Country (US)', 'Mario Kart 8 (US)'):
            assert (wiiu / g / f'{g}[Loadline].part01.rar').exists(), (g, r.stdout)
        assert not list(wiiu.glob('*.rar')), r.stdout
        assert not (dest / 'duplicates').exists() and not (dest / 'extra').exists(), r.stdout


def test_config_dirs_keep_their_names():
    """Two LaunchELF title.cfg files must not land on top of each other."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dest = root / 'src', root / 'dest'
        apps = src / 'Sony PlayStation 2 (USA) [Redump]' / 'APPS'
        touch(apps / 'BOOT.ELF')
        touch(apps / 'LAUNCHDISC' / 'title.cfg', b'a\n')
        touch(apps / 'LAUNCHELF' / 'title.cfg', b'b\n')
        touch(src / 'Sony PlayStation 2 (USA) [Redump]' / 'ART' / 'BOOT.ELF_COV.png')
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(dest), '--mode', 'copy'], capture_output=True, text=True)
        out = dest / 'roms' / 'ps2' / 'APPS'
        assert (out / 'LAUNCHDISC' / 'title.cfg').read_bytes() == b'a\n', r.stdout
        assert (out / 'LAUNCHELF' / 'title.cfg').read_bytes() == b'b\n', r.stdout
        assert not (dest / 'duplicates').exists(), r.stdout
        # artwork is known to be droppable; it stays skipped
        assert 'extensions: .png' in r.stdout, r.stdout


def test_extracted_disc_is_one_game():
    """An extracted PS1 disc is 40+ nameless files; SYSTEM.CNF says it is one game.

    The library folder holding it must not be swallowed along with it."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dest = root / 'src', root / 'dest'
        games = src / 'Sony PlayStation 1 Redump' / 'Games'
        dump = games / 'TombRaiderDump'
        touch(dump / 'SYSTEM.CNF', b'BOOT = cdrom:\\SLUS_001.52;1\n')
        for f in ('SLUS_001.52', 'CACKLOGO.RAW', 'DELDATA/END.RAW',
                  'DELDATA/FMVTAB.DAT', 'PSXDATA/LEVEL1.PSX'):
            touch(dump / f)
        for n in ('Ape Escape (USA).7z', 'Tom and Jerry (USA).7z'):
            touch(games / n)
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(dest), '--mode', 'copy'], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
        out = dest / 'roms' / 'psx' / 'TombRaiderDump'
        assert (out / 'DELDATA' / 'FMVTAB.DAT').exists(), r.stdout
        assert (out / 'PSXDATA' / 'LEVEL1.PSX').exists(), r.stdout
        # the siblings are separate games, not part of the dump
        assert not (out / 'Ape Escape (USA).7z').exists(), r.stdout
        assert list((dest / 'roms' / 'psx').glob('Ape Escape*/*.7z')), r.stdout
        # a PS2 disc is the same layout; BOOT2 is what tells them apart
        touch(dump / 'SYSTEM.CNF', b'BOOT2 = cdrom0:\\SLUS_202.15;1\n')
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(dump), str(root / 'd2'), '--mode', 'copy'], capture_output=True, text=True)
        assert (root / 'd2' / 'roms' / 'ps2' / 'TombRaiderDump' / 'SYSTEM.CNF').exists(), r.stdout


def test_copy_and_move_end_to_end():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dest = root / 'src', root / 'dest'
        touch(src / 'PSX' / 'Game (Disc 1).cue')
        touch(src / 'PSX' / 'Game (Track 01).bin')
        touch(src / 'Mario.nes')

        run = lambda *a: subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(dest), *a],
            capture_output=True, text=True)

        r = run('--mode', 'copy')
        assert r.returncode == 0, r.stdout + r.stderr
        assert (src / 'Mario.nes').exists(), "copy must leave the source in place"
        assert (dest / 'roms' / 'nes' / 'Mario.nes').exists()
        assert (dest / 'roms' / 'psx' / 'Game' / 'Game (Disc 1).cue').exists()
        assert (dest / 'roms' / 'psx' / 'Game' / 'Game (Track 01).bin').exists()
        assert (dest / 'rom_organization_changelog.json').exists()

        r = run('--mode', 'move')
        assert not (src / 'Mario.nes').exists(), "move must remove the source"
        # identical content already at dest -> isolated as a duplicate
        assert (dest / 'duplicates' / 'nes' / 'Mario.nes').exists()
        assert 'duplicate: 3' in r.stdout, r.stdout


def test_dat_resolves_name_collision():
    import hashlib
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src, dest = root / 'src', root / 'dest'
        # Same name, different bytes: without a DAT this lands in extra/.
        touch(dest / 'roms' / 'nes' / 'Zelda.nes', b'rev-a-bytes')
        touch(src / 'Zelda.nes', b'rev-b-bytes')
        md5 = hashlib.md5(b'rev-b-bytes').hexdigest()
        (root / 'nointro.dat').write_text(
            '<datafile><game name="Zelda"><rom name="Legend of Zelda, The (USA) (Rev B).nes" '
            f'md5="{md5.upper()}"/></game></datafile>')

        run = lambda *a: subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(dest), *a], capture_output=True, text=True)

        r = run('--mode', 'copy')
        assert r.returncode == 0, r.stdout + r.stderr
        assert (dest / 'roms' / 'nes' / 'Zelda.nes').read_bytes() == b'rev-a-bytes', "must not clobber"
        assert (dest / 'extra' / 'nes' / 'Zelda.nes').exists(), "no DAT -> quarantine"

        r = run('--mode', 'copy', '--dat', str(root / 'nointro.dat'))
        assert r.returncode == 0, r.stdout + r.stderr
        assert (dest / 'roms' / 'nes' / 'Legend of Zelda, The (USA) (Rev B).nes').exists(), r.stdout


def test_parse_dat_formats():
    xml = (b'<datafile><game name="G"><rom name="G (USA).bin" md5="AABBCCDDEEFF00112233445566778899"/>'
           b'</game></datafile>')
    assert parse_dat(xml) == {'aabbccddeeff00112233445566778899': 'G (USA).bin'}

    cmp_text = b'game (\n\tname "G"\n\trom ( name "G (USA).nes" size 16400 crc 1234 md5 AABBCCDDEEFF00112233445566778899 sha1 CC )\n)\n'
    assert parse_dat(cmp_text) == {'aabbccddeeff00112233445566778899': 'G (USA).nes'}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('Sony - PlayStation - Datfile.dat', xml)
    assert parse_dat(buf.getvalue()) == {'aabbccddeeff00112233445566778899': 'G (USA).bin'}, "zipped dat must unwrap"


def test_headered_rom_matches_headerless_dat():
    import hashlib
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        body = b'\x01' * 64
        rom = touch(root / 'Zelda.nes', b'NES\x1a' + b'\0' * 12 + body)
        assert header_size(rom) == 16
        assert header_size(touch(root / 'plain.nes', body)) == 0
        assert header_size(touch(root / 'a.a78', b'\x01ATARI7800' + b'\0' * 200)) == 128

        headerless = hashlib.md5(body).hexdigest()
        whole = hashlib.md5(rom.read_bytes()).hexdigest()
        index = {headerless: 'Legend of Zelda, The (USA).nes'}
        # the file's own md5 is not in the DAT; only the headerless one is
        assert dat_lookup(index, rom, whole) == 'Legend of Zelda, The (USA).nes'
        assert dat_lookup(index, root / 'plain.nes', whole) is None


def test_copier_header_stripping():
    import hashlib
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # SNES: no magic, detected purely by the odd 512 bytes past a whole KB
        body = bytes(range(256)) * 128          # 32 KB
        smc = touch(root / 'g.smc', b'\xff' * 512 + body)
        assert header_size(smc) == 512
        assert header_size(touch(root / 'plain.sfc', body)) == 0, "clean dump has no header"
        index = {hashlib.md5(body).hexdigest(): 'Super Mario World (USA).sfc'}
        assert dat_lookup(index, smc, 'nope') == 'Super Mario World (USA).sfc'

        # Genesis SMD: 512-byte header plus 16 KB-block interleaving
        plain = bytes(range(256)) * 128         # 32 KB = two SMD blocks
        woven = bytearray()
        for i in range(0, len(plain), 16384):
            blk = plain[i:i + 16384]
            woven += blk[1::2] + blk[0::2]
        hdr = bytearray(512)
        hdr[8:10] = b'\xaa\xbb'
        smd = touch(root / 'g.smd', bytes(hdr) + bytes(woven))
        assert is_smd(smd) and not is_smd(smc)
        assert smd_md5(smd) == hashlib.md5(plain).hexdigest(), "deinterleave must restore the rom"
        index = {hashlib.md5(plain).hexdigest(): 'Sonic The Hedgehog (USA, Europe).md'}
        assert dat_lookup(index, smd, 'nope') == 'Sonic The Hedgehog (USA, Europe).md'
        # a plain headerless dump must not be mangled into a false match
        assert dat_lookup(index, touch(root / 'clean.md', plain), None) is None


def test_fetch_dat_uses_cache_and_refreshes(monkeypatch=None):
    import romganizer
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / 'dats'
        cache.mkdir()
        (cache / 'nes.dat').write_bytes(
            b'game (\n\trom ( name "Cached.nes" size 1 crc 0 md5 AABBCCDDEEFF00112233445566778899 sha1 CC )\n)\n')
        calls = []
        orig_cache, orig_open = romganizer.DAT_CACHE, romganizer.urllib.request.urlopen
        romganizer.DAT_CACHE = cache
        romganizer.urllib.request.urlopen = lambda *a, **k: calls.append(1)
        try:
            # fresh file -> no network
            assert fetch_dat('nes') == {'aabbccddeeff00112233445566778899': 'Cached.nes'}
            assert calls == [], "fresh cache must not download"
            # aged past the limit -> download attempted, stale copy still used on failure
            os.utime(cache / 'nes.dat', (0, 0))
            assert fetch_dat('nes') == {'aabbccddeeff00112233445566778899': 'Cached.nes'}
            assert calls == [1], "stale cache must trigger a download"
            assert fetch_dat('unknown-system') == {}
        finally:
            romganizer.DAT_CACHE = orig_cache
            romganizer.urllib.request.urlopen = orig_open


def test_skipped_files_exit_nonzero():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / 'src'
        touch(src / 'mystery.xyz')
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(root / 'dest')], capture_output=True, text=True)
        assert r.returncode == 1, r.stdout
        assert 'skipped: 1' in r.stdout, r.stdout


def test_bios_is_a_word_not_a_substring():
    # "bioship" is an arcade game; "biosnds7.bin" really is DS firmware.
    assert not is_bios_name('bioship.zip')
    assert not is_bios_name('bioship.png')
    assert not is_bios_name('BioShock.iso')
    assert is_bios_name('biosnds7.bin')
    assert is_bios_name('biosdsi9.bin')
    assert is_bios_name('STBIOS.bin')
    assert is_bios_name('gba_bios.bin')
    assert is_bios_name('scph1001.bin')
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / 'roms' / 'fbneo'
        assert determine_system(touch(src / 'bioship.zip')) == 'fbneo'
        # artwork stays artwork even when it sits in a bios directory
        assert determine_system(touch(Path(td) / 'bios' / 'bioship.png')) == 'unknown'
        assert determine_system(touch(Path(td) / 'bios' / 'readme.txt')) == 'unknown'


def test_bios_directory_rescues_any_extension():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pack = root / 'BIOS Pack' / 'system'
        for name in ('kick34005.A500', 'tos104uk.img', 'ROM1',
                     'keropi/iplrom.dat', 'xmil/IPLROM.X1', 'vice/PET/chargen'):
            f = touch(pack / name)
            assert bios_root(f) == pack
            assert 'name=bios' in classify(f)[1], classify(f)
        # a saves directory is not a bios directory
        assert bios_root(touch(root / 'saves' / 'system' / 'x.srm')) is None
        subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(root / 'BIOS Pack'), str(root / 'dest')], capture_output=True, text=True)
        # the layout the emulator looks for is kept, not flattened
        assert (root / 'dest' / 'bios' / 'kick34005.A500').exists()
        assert (root / 'dest' / 'bios' / 'keropi' / 'iplrom.dat').exists()
        assert (root / 'dest' / 'bios' / 'vice' / 'PET' / 'chargen').exists()


def test_track_files_are_named_by_their_folder():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / 'TOP 100 DREAMCAST'
        for game in ('13. Test Drive Le Mans (USA)', '22. Border Down (Japan)'):
            touch(src / game / 'track01.bin')
            touch(src / game / 'track02.raw')
            touch(src / game / f'{game[4:]}.gdi')
        subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(root / 'dest')], capture_output=True, text=True)
        dc = root / 'dest' / 'roms' / 'dreamcast'
        # each game keeps its own folder; nothing lands in a shared "track01/"
        assert (dc / 'Test Drive Le Mans (USA)' / 'track01.bin').exists()
        assert (dc / 'Border Down (Japan)' / 'track01.bin').exists()
        assert not (dc / 'track01').exists()


def test_scraped_media_is_left_behind():
    """roms/<system>/media/ is dropped; anything else below the library follows."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        lib = root / 'backup' / 'roms' / 'atari2600'
        touch(lib / 'Pitfall! (USA).a26')
        touch(lib / 'media' / 'box3d' / 'Pitfall! (USA).png')
        touch(lib / 'snap' / 'Pitfall! (USA).png')
        touch(lib / 'gamelist.xml')  # loose in the library root: still skipped
        subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(root / 'backup'), str(root / 'dest')], capture_output=True, text=True)
        out = root / 'dest' / 'roms' / 'atari2600'
        assert (out / 'Pitfall! (USA).a26').exists()
        assert not (out / 'media').exists()
        assert (out / 'snap' / 'Pitfall! (USA).png').exists()
        assert not (out / 'gamelist.xml').exists()


def test_dry_run_reports_collisions_between_its_own_files():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / 'src'
        touch(src / 'a' / 'roms' / 'snes' / 'Zelda (USA).sfc', b'one')
        touch(src / 'b' / 'roms' / 'snes' / 'Zelda (USA).sfc', b'two')
        touch(src / 'c' / 'roms' / 'snes' / 'Zelda (USA).sfc', b'one')
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(root / 'dest'), '--dry-run'], capture_output=True, text=True)
        assert 'organized: 1' in r.stdout, r.stdout   # not three onto one path
        assert 'duplicate: 1' in r.stdout, r.stdout   # same bytes
        assert 'extra: 1' in r.stdout, r.stdout       # same name, different bytes
        assert not (root / 'dest' / 'roms').exists(), 'a dry run wrote something'

def test_a_single_game_leaves_emulator_saves_behind():
    # A lone rom must still leave its save/state/nvram files where the emulator
    # already looks for them -- there is no sibling rom to hide that behind.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / 'backup'
        touch(src / 'roms' / 'arcade' / 'centiped.zip')
        for f in ('mame/mame2003/cfg/centiped.cfg', 'mame/mame2003/cfg/default.cfg',
                  'mame/mame2003/nvram/centiped.nv', 'neogeo/fbneo/kof97.fs',
                  'dreamcast/reicast/vmu_save_A1.bin'):
            touch(src / 'saves' / f)
        subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(root / 'dest')], capture_output=True, text=True)
        assert (root / 'dest' / 'roms' / 'arcade' / 'centiped.zip').exists()
        # an emulator looks for these where they already are; nothing follows
        assert not (root / 'dest' / 'roms' / 'arcade' / 'cfg').exists()
        assert not (root / 'dest' / 'roms' / 'neogeo').exists()
        assert not (root / 'dest' / 'roms' / 'dreamcast').exists()
        assert (src / 'saves' / 'neogeo' / 'fbneo' / 'kof97.fs').exists()

def test_saves_are_never_pulled_into_the_library():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / 'backup'
        touch(src / 'roms' / 'arcade' / 'centiped.zip')
        touch(src / 'roms' / 'arcade' / 'galaga.zip')
        for f in ('mame/mame2003/cfg/centiped.cfg', 'mame/mame2003/cfg/default.cfg',
                  'mame/mame2003/nvram/centiped.nv', 'neogeo/fbneo/kof97.fs',
                  'dreamcast/reicast/vmu_save_A1.bin'):
            touch(src / 'saves' / f)
        subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(root / 'dest')], capture_output=True, text=True)
        assert (root / 'dest' / 'roms' / 'arcade' / 'centiped.zip').exists()
        # an emulator looks for these where they already are; nothing follows
        assert not (root / 'dest' / 'roms' / 'arcade' / 'cfg').exists()
        assert not (root / 'dest' / 'roms' / 'neogeo').exists()
        assert not (root / 'dest' / 'roms' / 'dreamcast').exists()
        assert (src / 'saves' / 'neogeo' / 'fbneo' / 'kof97.fs').exists()

def test_volume_numbered_stem_joins_its_own_game():
    from romganizer import game_stem
    assert game_stem('SSX (USA).iso01.iso') == 'SSX (USA)'
    assert game_stem('Marvel vs. Capcom 2 (USA)..iso01.iso') == 'Marvel vs. Capcom 2 (USA)'
    assert game_stem('ICO (USA).cue') == 'ICO (USA)'
    assert game_stem('Mega Man X4.iso') == 'Mega Man X4'   # a trailing digit is not a volume
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / 'PS2 [Redump]'
        for name in ('SSX (USA).cue', 'SSX (USA).bin', 'SSX (USA).iso01.iso'):
            touch(src / name)
        subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(src), str(root / 'dest')], capture_output=True, text=True)
        game = root / 'dest' / 'roms' / 'ps2' / 'SSX (USA)'
        assert (game / 'SSX (USA).iso01.iso').exists()
        assert (game / 'SSX (USA).cue').exists()
        assert not (root / 'dest' / 'roms' / 'ps2' / 'SSX (USA).iso01').exists()

def test_whole_image_unpacking_stays_off_tmpfs():
    import romganizer
    # /tmp is tmpfs on most systemd distros; unpacking a 1.7 GB chd there is a
    # RAM allocation, not a disk one.
    assert romganizer.scratch_dir() != '/tmp'
    assert romganizer.scratch_dir() in (os.environ.get('TMPDIR'), '/var/tmp', None)

    with tempfile.TemporaryDirectory() as td:
        chd = touch(Path(td) / 'big.chd', b'x' * 4096)
        calls = []
        saved = (romganizer.subprocess.run, romganizer.shutil.disk_usage, romganizer.shutil.which)
        free = [0]
        romganizer.shutil.which = lambda tool: '/usr/bin/' + tool
        romganizer.shutil.disk_usage = lambda p: type('U', (), {'free': free[0]})()
        romganizer.subprocess.run = lambda argv, **kw: calls.append(kw)
        try:
            free[0] = 4096            # less than 4096 * EXTRACT_FACTOR
            assert romganizer.sniff_extracted(chd) == ()
            assert not calls, 'unpacked anyway with no room for it'

            free[0] = 1 << 40
            romganizer.sniff_extracted(chd)
            assert calls, 'never tried to unpack'
            for kw in calls:
                # half an hour of chdman progress redraws, buffered for nothing
                assert not kw.get('capture_output'), 'buffering output it never reads'
                assert kw['stdout'] is subprocess.DEVNULL
                assert kw['stderr'] is subprocess.DEVNULL
        finally:
            romganizer.subprocess.run, romganizer.shutil.disk_usage, romganizer.shutil.which = saved


def test_systems_that_had_no_extension_of_their_own():
    # Each of these only ever resolved because the path happened to name the
    # system; the extension identifies it now too.
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / 'unsorted'
        for name, system in (("Lock 'N' Chase (World).int", 'intellivision'),
                             ('Dragon Spirit (1988)(Dempa).dim', 'x68000'),
                             ('International 5-a-Side Football.z80', 'zxspectrum'),
                             ('osanas_revenge.solarus', 'solarus'),
                             ('Turrican II (1991).adz', 'amiga'),
                             ('Lotus Turbo Challenge.dms', 'amiga')):
            assert determine_system(touch(src / name)) == system, classify(src / name)


def test_kickstarts_are_firmware_anywhere():
    # .A500/.CD32/.CDTV are Kickstart extensions, not Amiga game formats
    for name in ('kick33180.A500', 'kick40060.CD32', 'kick34005.CDTV',
                 'kick40068.A4000', 'kick37350.A600', 'kick39106.A1200'):
        assert is_bios_name(name), name
    assert not is_bios_name('Kick Off 2 (1990).adf')   # a game that starts with "kick"
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # loose in a download folder, with no bios/ directory to give it away
        touch(root / 'src' / 'Amiga Kickstart ROMs' / 'kick40060.CD32')
        subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(root / 'src'), str(root / 'dest')], capture_output=True, text=True)
        assert not (root / 'dest' / 'roms' / 'amiga').exists()
        assert list((root / 'dest' / 'bios').rglob('kick40060.CD32'))

def test_quick_compare_skips_the_hash_unless_it_has_to():
    """Size decides outright; mtime decides a tie; only a real tie costs a read."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        a, b, c, d = (root / n for n in ('a.iso', 'b.iso', 'c.iso', 'd.iso'))
        touch(a, b'x' * 100)
        touch(b, b'y' * 200)          # different length
        touch(c, b'z' * 100)          # same length, same mtime
        touch(d, b'w' * 100)          # same length, older mtime
        os.utime(a, (1_000_000, 1_000_000))
        os.utime(c, (1_000_000, 1_000_000))
        os.utime(d, (2_000_000, 2_000_000))

        assert quick_compare(a, b) is False, "a size mismatch needs no hash"
        assert quick_compare(a, d) is None, "same size, different mtime must fall through to the hash"
        assert quick_compare(a, root / 'gone.iso') is None, "an unstattable file falls through"
        # Equal size + equal mtime is only trusted when asked for: a bulk copy
        # stamps a whole collection with one mtime, so this would file two
        # different revisions of equal length as duplicates.
        assert quick_compare(a, c) is None, "same size+mtime still hashes by default"
        assert quick_compare(a, c, trust_mtime=True) is True


def test_colliding_names_of_different_size_are_never_read():
    """The expensive case: a name collision that size alone already settles."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        touch(root / 'src' / 'one' / 'Zelda (USA).n64', b'A' * 4096)
        touch(root / 'src' / 'two' / 'Zelda (USA).n64', b'B' * 8192)
        out = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(root / 'src'), str(root / 'dest')], capture_output=True, text=True)
        # differing sizes -> not a duplicate -> the loser is preserved in extra/
        assert list((root / 'dest' / 'extra').rglob('Zelda (USA).n64')), out.stdout[-2000:]
        assert not (root / 'dest' / 'duplicates').exists(), "different lengths are not duplicates"


def test_identical_copies_are_deduped_without_hashing():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        one = root / 'src' / 'one' / 'Metroid (USA).n64'
        two = root / 'src' / 'two' / 'Metroid (USA).n64'
        touch(one, b'M' * 4096)
        touch(two, b'M' * 4096)
        os.utime(one, (1_500_000, 1_500_000))
        os.utime(two, (1_500_000, 1_500_000))
        out = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(root / 'src'), str(root / 'dest'), '--trust-mtime'], capture_output=True, text=True)
        assert list((root / 'dest' / 'duplicates').rglob('Metroid (USA).n64')), out.stdout[-2000:]
        log = json.loads((root / 'dest' / 'rom_organization_changelog.json').read_text())
        reasons = [e['reason'] for e in log if e['action'] == 'duplicate_isolate']
        assert reasons and all('size and mtime' in r for r in reasons), reasons


def _sevenzip(path, members):
    """Build a .7z via the same binary the script shells out to; skip if absent."""
    if not shutil.which('7z'):
        return False
    stage = path.parent / (path.stem + '_stage')
    stage.mkdir(parents=True, exist_ok=True)
    for name, data in members.items():
        (stage / name).write_bytes(data)
    r = subprocess.run(['7z', 'a', '-bso0', '-bsp0', str(path)] + [str(stage / n) for n in members],
                       capture_output=True, text=True)
    return r.returncode == 0 and path.exists()


def test_archive_crcs_reads_the_header_not_the_payload():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        z = root / 'a.zip'
        with zipfile.ZipFile(z, 'w') as zf:
            zf.writestr('game.iso', b'PAYLOAD' * 100)
        crcs = archive_crcs(z)
        assert crcs and list(crcs) == ['game.iso']
        assert crcs['game.iso'][0] == 700

        # not an archive, and a corrupt one -> refuse to answer, never guess
        touch(root / 'plain.iso', b'not an archive')
        assert archive_crcs(root / 'plain.iso') is None
        (root / 'bad.zip').write_bytes(b'PK\x03\x04 truncated garbage')
        assert archive_crcs(root / 'bad.zip') is None


def test_same_size_archives_are_settled_by_crc_without_hashing():
    """The GameCube case: equal size, different mtime, only the CRC can decide."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        same_a, same_b, other = root / 'x.zip', root / 'y.zip', root / 'z.zip'
        for f in (same_a, same_b):
            with zipfile.ZipFile(f, 'w', zipfile.ZIP_STORED) as zf:
                zf.writestr('game.iso', b'IDENTICAL' * 500)
        with zipfile.ZipFile(other, 'w', zipfile.ZIP_STORED) as zf:
            zf.writestr('game.iso', b'DIFFERENT' * 500)   # same length, other bytes
        assert same_a.stat().st_size == other.stat().st_size, "sizes must tie for this test"
        os.utime(same_a, (1_000_000, 1_000_000))
        os.utime(same_b, (2_000_000, 2_000_000))
        os.utime(other, (3_000_000, 3_000_000))

        assert quick_compare(same_a, same_b) is True, "matching CRCs settle it, no hash"
        assert quick_compare(same_a, other) is False, "differing CRCs settle it too"


def test_sevenzip_pairs_are_compared_on_their_header():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        a, b, c = root / 'a.7z', root / 'b.7z', root / 'c.7z'
        if not _sevenzip(a, {'game.iso': b'SAME' * 1000}):
            return                                  # no 7z binary on this box
        _sevenzip(b, {'game.iso': b'SAME' * 1000})
        _sevenzip(c, {'game.iso': b'DIFF' * 1000})
        assert archive_crcs(a) == archive_crcs(b)
        assert archive_crcs(a) != archive_crcs(c)
        assert quick_compare(a, b) is True
        if a.stat().st_size == c.stat().st_size:
            assert quick_compare(a, c) is False


def test_unreadable_archive_falls_through_to_the_hash():
    """Refusing to answer must mean 'hash it', never 'these are unique'."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        a, b = root / 'a.zip', root / 'b.zip'
        a.write_bytes(b'PK\x03\x04' + b'\x00' * 96)
        b.write_bytes(b'PK\x03\x04' + b'\xff' * 96)
        assert a.stat().st_size == b.stat().st_size
        assert quick_compare(a, b) is None, "unreadable headers must fall through"


def test_saves_beside_their_rom_are_not_carried_with_the_library():
    """The companion-carry path must not sweep up .srm/.eep sitting next to a rom.

    A library that was already organized keeps saves loose beside the game
    rather than in a saves/ folder, so the directory guard alone misses them.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        lib = root / 'src' / 'roms' / 'snes'
        touch(lib / 'Super Metroid.sfc')
        touch(lib / 'Super Metroid.srm')          # battery save, loose beside it
        touch(lib / 'Super Metroid.state1')       # numbered RetroArch slot
        touch(lib / 'media' / 'Super Metroid.png')  # scraped media: dropped
        subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(root / 'src'), str(root / 'dest')], capture_output=True, text=True)
        dest = root / 'dest'
        assert list(dest.rglob('Super Metroid.sfc')), "the rom itself must move"
        assert not list(dest.rglob('Super Metroid.png')), "scraped media must be left behind"
        assert not list(dest.rglob('*.srm')), "a battery save must be left in place"
        assert not list(dest.rglob('*.state1')), "a numbered save state must be left in place"
        assert (lib / 'Super Metroid.srm').exists(), "and left where the emulator expects it"


def test_save_extensions_are_recognised_outside_a_saves_directory():
    with tempfile.TemporaryDirectory() as td:
        for ext in ('.srm', '.eep', '.mpk', '.nv', '.state', '.state2', '.sav'):
            assert in_saves(Path(td) / f'game{ext}'), ext
        for ext in ('.sfc', '.iso', '.7z', '.nes', '.z80'):
            assert not in_saves(Path(td) / f'game{ext}'), ext
        # .z80 is a ZX Spectrum snapshot, not an ".nv"-style save -- guard the
        # numbered-suffix strip from eating real rom extensions.
        assert not in_saves(Path(td) / 'game.nes')


def test_folder_unit_leaves_its_saves_behind():
    """A folder that travels as one unit must still not carry nvram into the library.

    This is the path the companion-carry guard misses: the whole tree is renamed
    in one operation, so the saves inside ride along unless they are put back.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        # A PS2 disc folder: system.cnf makes it a unit, so the whole tree is
        # renamed in one go and the save inside rides along unless put back.
        game = root / 'src' / 'Gran Turismo 3'
        touch(game / 'system.cnf', b'BOOT2 = cdrom0:\\SLUS_200.62;1')
        touch(game / 'SLUS_200.62', b'exec')
        touch(game / 'gt3.nv', b'nvram')
        dest = root / 'dest'
        out = subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(root / 'src'), str(dest)], capture_output=True, text=True)
        assert 'as one unit' in out.stdout, "must exercise the folder-unit path\n" + out.stdout[-1500:]
        assert list(dest.rglob('system.cnf')), out.stdout[-1500:]
        assert not list(dest.rglob('*.nv')), "nvram must not enter the library"
        assert (game / 'gt3.nv').exists(), "and must be left where it was"
        log = json.loads((dest / 'rom_organization_changelog.json').read_text())
        nv = [e for e in log if e['source'].endswith('.nv')]
        assert nv and nv[0]['action'] == 'skip', nv
        assert 'save, state or nvram' in nv[0]['reason'], nv[0]['reason']


def test_skip_reason_names_the_save_not_the_extension():
    """The changelog is the audit trail; "not a recognized extension" hides why."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        touch(root / 'src' / 'snes' / 'Chrono Trigger.sfc')
        touch(root / 'src' / 'snes' / 'Chrono Trigger.srm', b'save')
        dest = root / 'dest'
        subprocess.run(
            [sys.executable, str(Path(__file__).parent / 'romganizer.py'),
             str(root / 'src'), str(dest)], capture_output=True, text=True)
        log = json.loads((dest / 'rom_organization_changelog.json').read_text())
        srm = [e for e in log if e['source'].endswith('.srm')]
        assert srm, "the save must appear in the changelog"
        assert 'save, state or nvram' in srm[0]['reason'], srm[0]['reason']


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn()
            print(f'ok  {name}')
    print('all passed')
