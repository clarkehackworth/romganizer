import os
import io
import sys
import zlib
import struct
import subprocess
import tempfile
import hashlib
import shutil
import zipfile
import json
import csv
import argparse
import re
import glob
import time
import xml.etree.ElementTree as ET
import urllib.request
import urllib.parse
from pathlib import Path

# Cartridge sets come from No-Intro via the libretro mirror (clrmamepro text);
# disc sets come from redump.org, which serves a zipped Logiqx XML. Both carry
# an md5 per rom, which is what calculate_hash already produces on a collision.
_NOINTRO = 'https://raw.githubusercontent.com/libretro/libretro-database/master/metadat/no-intro/{}.dat'
_REDUMP = 'http://redump.org/datfile/{}/'
DAT_SOURCES = {
    'atari2600': _NOINTRO.format('Atari - 2600'),
    'atari5200': _NOINTRO.format('Atari - 5200'),
    'atari7800': _NOINTRO.format('Atari - 7800'),
    'atari800': _NOINTRO.format('Atari - 8-bit Family'),
    'atarijaguar': _NOINTRO.format('Atari - Jaguar'),
    'atarist': _NOINTRO.format('Atari - ST'),
    'lynx': _NOINTRO.format('Atari - Lynx'),
    'wonderswan': _NOINTRO.format('Bandai - WonderSwan'),
    'coleco': _NOINTRO.format('Coleco - ColecoVision'),
    'c64': _NOINTRO.format('Commodore - 64'),
    'amiga': _NOINTRO.format('Commodore - Amiga'),
    'vectrex': _NOINTRO.format('GCE - Vectrex'),
    'pcengine': _NOINTRO.format('NEC - PC Engine - TurboGrafx 16'),
    'fds': _NOINTRO.format('Nintendo - Family Computer Disk System'),
    'gba': _NOINTRO.format('Nintendo - Game Boy Advance'),
    'gbc': _NOINTRO.format('Nintendo - Game Boy Color'),
    'gb': _NOINTRO.format('Nintendo - Game Boy'),
    'n3ds': _NOINTRO.format('Nintendo - Nintendo 3DS'),
    'n64': _NOINTRO.format('Nintendo - Nintendo 64'),
    'nds': _NOINTRO.format('Nintendo - Nintendo DS'),
    'nes': _NOINTRO.format('Nintendo - Nintendo Entertainment System'),
    'snes': _NOINTRO.format('Nintendo - Super Nintendo Entertainment System'),
    'ngp': _NOINTRO.format('SNK - Neo Geo Pocket'),
    'ngpc': _NOINTRO.format('SNK - Neo Geo Pocket Color'),
    'sega32x': _NOINTRO.format('Sega - 32X'),
    'gamegear': _NOINTRO.format('Sega - Game Gear'),
    'mastersystem': _NOINTRO.format('Sega - Master System - Mark III'),
    'megadrive': _NOINTRO.format('Sega - Mega Drive - Genesis'),
    'dreamcast': _REDUMP.format('dc'),
    'ngc': _REDUMP.format('gc'),
    'ps2': _REDUMP.format('ps2'),
    'psp': _REDUMP.format('psp'),
    'psx': _REDUMP.format('psx'),
    'saturn': _REDUMP.format('ss'),
    'segacd': _REDUMP.format('mcd'),
    'wii': _REDUMP.format('wii'),
    'xbox': _REDUMP.format('xbox'),
    'xbox360': _REDUMP.format('xbox360'),
}
DAT_CACHE = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'romganizer' / 'dats'
DAT_MAX_AGE = 30 * 86400

# Formats whose dumps are distributed with a prepended header that No-Intro
# excludes from its hashes. Offset 0 magic -> header length in bytes.
ROM_HEADERS = {b'NES\x1a': 16, b'FDS\x1a': 16}
# Copier formats whose 512-byte header has no magic to detect it by.
COPIER_EXTS = {'.smc', '.sfc', '.fig', '.swc', '.smd', '.md', '.gen', '.bs'}

# Expanded system mapping using canonical platform folder slugs
SYSTEM_MAPPING = {
    'amiga': ['.adf', '.adz', '.dms', '.hdf', '.hdz', '.lha', '.uae', '.ipf'],
    'arcade': ['.7z'],  # .zip files handled dynamically via file-inspection
    'atari800': ['.atr', '.xfd', '.xex', '.car'],  # .xex shared with xbox360, .car with atari5200
    'atarist': ['.st', '.msa'],
    'atari2600': ['.a26', '.rom', '.bin'],
    'atari5200': ['.a52', '.car'],
    'atari7800': ['.a78'],
    'atarijaguar': ['.j64', '.jag'],
    'coleco': ['.col'],
    'intellivision': ['.int'],
    'x68000': ['.dim', '.xdf', '.d88', '.88d', '.hdm'],
    'zxspectrum': ['.z80', '.tap', '.tzx', '.sna', '.szx'],
    'solarus': ['.solarus'],
    'c64': ['.d64', '.t64', '.crt', '.g64'],
    'dreamcast': ['.cdi', '.gdi'],  # .chd handled with context parsing
    'fds': ['.fds'],
    'gamegear': ['.gg'],
    'gb': ['.gb'],
    'gbc': ['.gbc'],
    'gba': ['.gba'],
    'lynx': ['.lnx'],
    'ngp': ['.ngp'],
    'ngpc': ['.ngc', '.npc'],
    'neogeo':[],  # NeoGeo arcade romsets handled via contextual fallback
    'nes': ['.nes'],
    'n64': ['.n64', '.v64', '.z64'],
    'nds': ['.nds'],
    'n3ds': ['.3ds', '.cia'],
    'ngc': ['.iso', '.gcm'],
    'wii': ['.wbfs', '.iso'],
    'wiiu': ['.wux', '.rpx'],
    'switch': ['.xci', '.nsp'],
    'pc': ['.exe', '.bat', '.conf', '.wad', '.pk3'],
    'pcengine': ['.pce'],
    # .m3u just lists the discs of a set -- x68000 floppy sets use it too, so it
    # names no system on its own and has to let the path or its siblings decide.
    'psx': ['.pbp'],  # .bin, .cue, .chd handled via contextual layout validation
    'ps2': ['.iso', '.cso'],
    # A PS3 title is a folder, not a file: .pkg is the only standalone form, and
    # the rest of a disc rip is identified by its layout (see FOLDER_MARKERS).
    'ps3': ['.pkg', '.psarc', '.self'],
    'psp': ['.iso', '.cso'],
    'xbox': ['.iso'],
    'xbox360': ['.iso', '.xex'],
    'mastersystem': ['.sms'],
    'megadrive': ['.md', '.smd'],
    'segacd': [],
    'sega32x': ['.32x'],
    'saturn': [],
    'snes': ['.sfc', '.smc'],
    'vectrex': ['.vec'],
    'wonderswan': ['.ws', '.wsc']
}

# Extensions shared by several platforms. Anything listed under more than one
# system is ambiguous by construction; the literals are the ones a single
# SYSTEM_MAPPING entry can't express (a bare .bin is a 2600 cart, a PSX track,
# or a BIOS, and .cue/.chd name no system at all).
# Artwork, docs and scene junk that ships alongside a dump. These would otherwise
# inherit their folder's system once path hints can rescue an unknown extension.
NON_ROM_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.mp4', '.avi',
                '.txt', '.nfo', '.md', '.pdf', '.html', '.htm', '.url', '.log',
                '.dat', '.xml', '.sfv', '.md5', '.sha1', '.torrent', '.part',
                '.dll', '.so', '.dylib'}

# Inside a bios directory the same list holds, minus the ones that are firmware
# there rather than metadata: keropi ships the X68000 boot rom as iplrom.dat.
BIOS_JUNK_EXTS = NON_ROM_EXTS - {'.dat'}

# .7z is arcade only in a MAME context; everywhere else it is just a container
# someone zipped a dump into, so it must never outweigh the path or the contents.
AMBIGUOUS_EXTS = {'.m3u', '.bin', '.cue', '.chd', '.xex', '.7z'} | {
    e for e in {e for exts in SYSTEM_MAPPING.values() for e in exts}
    if sum(e in exts for exts in SYSTEM_MAPPING.values()) > 1
}

# Everything that can be a dump in its own right. Anything else found next to one
# (.ini, .cfg, .lic, .mcr, artwork) is a sidecar belonging to that game, not a ROM.
ROM_EXTS = {e for exts in SYSTEM_MAPPING.values() for e in exts} | AMBIGUOUS_EXTS | COPIER_EXTS | {
    '.zip', '.7z', '.rar', '.rvz', '.gz', '.wbfs', '.iso', '.img', '.rom'}

# Some dumps are a folder layout rather than a file: nothing inside a PS3 rip
# has a telling extension, but PS3_DISC.SFB only ever sits at the root of one.
# Finding a marker names the system AND the folder that is the whole game.
def _disc_cnf(path):
    """SYSTEM.CNF sits at the root of every PS1/PS2 disc. BOOT2 means PS2."""
    try:
        with open(path, 'rb') as fh:
            return 'ps2' if b'BOOT2' in fh.read(512).upper() else 'psx'
    except OSError:
        return 'psx'

def _param_sfo(path):
    """PARAM.SFO means PS3 only directly inside PS3_GAME. PSP savedata and PSP
    games carry one too, and those are not a disc rip."""
    return 'ps3' if path.parent.name.upper() == 'PS3_GAME' else None

FOLDER_MARKERS = {
    'system.cnf': _disc_cnf,
    'ps3_disc.sfb': 'ps3',
    'param.sfo': _param_sfo,
    'default.xbe': 'xbox',
    'default.xex': 'xbox360',
}

# "Collection.zip.001", "Collection.zip.002", "set.z01": one archive cut into
# volumes. No single volume is a dump, and none opens without the others, so the
# set is left alone rather than filed as that many games. Deliberately narrow:
# ".rNN" is left out (it rides along with its .rar inside a game folder), and a
# numeric extension alone is not enough -- "SLUS_001.52" is a PS1 executable.
# The split-zip ".z01" form is left out too: ".z80" is a ZX Spectrum snapshot.
SPLIT_VOLUME = re.compile(r'\.(?:zip|rar|7z|tar|gz|iso)\.\d{2,3}$', re.I)

# Battery saves, save states, nvram, MAME high scores and shader caches. Named
# by extension as well as by directory, because a library that was already
# organized keeps these loose beside the rom they belong to rather than in a
# saves/ folder of their own -- and there they would ride along with the scraped
# media into the new library.
SAVE_EXTS = {'.sav', '.srm', '.sra', '.state', '.auto', '.eep', '.eeprom',
             '.mpk', '.nv', '.nvmem', '.nvmem2', '.rtc', '.fla', '.hi',
             '.dif', '.glshadercache'}

def in_saves(file_path):
    """Save states, nvram, high scores and shader caches are not dumps."""
    lowered = [p.lower() for p in file_path.parts[:-1]]
    if 'saves' in lowered or 'savestates' in lowered:
        return True
    # ".state1", ".state2" -- RetroArch numbers its slots.
    suffix = re.sub(r'\d+$', '', file_path.suffix.lower())
    return suffix in SAVE_EXTS

# Directory names that name a system without being spelled like its key. Matched
# whole, never as a substring, so a "Sports" folder is not mistaken for "ports"
# (the EmulationStation/Batocera folder where pc ports live).
DIR_ALIASES = {'ports': 'pc', 'dos': 'pc', 'msdos': 'pc', 'dosbox': 'pc'}

# Checked in order against the full lowercased path, first match wins, so more
# specific hints must come before the prefixes they contain (wiiu before wii).
CONTEXT_HINTS = [
    ('psx', ['psx', 'ps1', 'playstation1', 'playstation 1', 'playstation']),
    ('ps2', ['ps2', 'playstation2', 'playstation 2']),
    ('ps3', ['ps3', 'playstation3', 'playstation 3']),
    ('psp', ['psp']),
    ('segacd', ['segacd', 'sega-cd', 'sega cd', 'megacd', 'mega-cd', 'mega cd']),
    ('saturn', ['saturn']),
    ('dreamcast', ['dreamcast']),
    ('wiiu', ['wiiu', 'wii u', 'wii-u']),
    ('wii', ['wii']),
    ('ngc', ['gamecube', 'game cube', 'ngc', 'gcn']),
    ('xbox360', ['xbox360', 'xbox 360', 'xbox-360']),
    ('xbox', ['xbox']),
    ('neogeo', ['neogeo', 'neo-geo', 'neo geo']),
    ('arcade', ['mame', 'arcade']),
    ('atarist', ['atarist', 'atari st', 'atari-st']),
    ('atari5200', ['atari5200', 'atari 5200']),
    ('atari7800', ['atari7800', 'atari 7800']),
    ('atari2600', ['atari2600', 'atari 2600', 'vcs']),
    ('atari800', ['xegs', 'atari800', 'atari 800', 'atari 8-bit', 'atari8bit']),
]

# Comprehensively mapped BIOS firmware naming variants across platforms
KNOWN_BIOS = {
    # Sony PlayStation 1 & 2
    'scph1001.bin', 'scph5501.bin', 'scph7001.bin', 'scph5502.bin', 'scph101.bin',
    'ps2-0230a-20080220.bin', 'scph39001.bin', 'scph70008.bin', 'scph90006.bin',
    # Sega CD & Saturn
    'bios_cd_u.bin', 'bios_cd_e.bin', 'bios_cd_j.bin', 'mega_cd_sub.bin',
    'sega_101.bin', 'mpr-17933.bin', 'mpr-18811.bin', 'mpr-19367.bin',
    # Nintendo GBA & Famicom Disk System
    'gba_bios.bin', 'disksys.rom',
    # Sega Dreamcast
    'dc_boot.bin', 'dc_flash.bin', 'bootrom.bin',
    # Amiga Kickstarts
    'kick310.rom', 'kick130.rom', 'kick205.rom',
    # NeoGeo BIOS
    'neogeo.zip'
}

# "track01.bin", "Track 03.raw", "disc2.iso": a numbered piece of one dump.
# A GDI rip is a named .gdi beside these, and a raw one is nothing but these --
# either way the folder holds one game and the folder is what names it.
GENERIC_STEM = re.compile(r'^(?:track|disc|cd|dvd|side)\s*\d+$', re.I)

SPECIAL_TAGS = ['dlc', 'hack', 'manual', 'mod', 'patch', 'update', 'demo', 'translation', 'prototype']

def _hms(seconds):
    if seconds is None or seconds != seconds or seconds < 0 or seconds > 356400:  # >99h reads as noise
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

def _size(num_bytes):
    for suffix in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024 or suffix == "TB":
            return f"{num_bytes:.1f} {suffix}"
        num_bytes /= 1024

def _rate(per_sec, unit):
    return f"{_size(per_sec)}/s" if unit == "B" else f"{per_sec:.1f}/s"

# Progress goes to stderr so stdout stays a clean, pipeable log.
# ponytail: no tqdm; total=0 means "unknown", so it just counts.
# Rate and ETA are whole-run averages, not a sliding window -- fine for NFS, where
# the slow part is uniformly slow. Swap in a windowed average if throughput is bursty.
def progress(label, done, total=0, width=28, start=None, unit=""):
    if not sys.stderr.isatty():
        return
    tail = ""
    if start is not None:
        elapsed = time.monotonic() - start
        if elapsed > 0.5 and done:  # too early to extrapolate before this
            per_sec = done / elapsed
            eta = (total - done) / per_sec if total else None
            tail = f" {_rate(per_sec, unit)} ETA {_hms(eta)}" if total else f" {_rate(per_sec, unit)}"
    if total:
        frac = done / total
        fill = int(width * frac)
        sys.stderr.write(f"\r\x1b[K{label} |{'#' * fill}{'-' * (width - fill)}| {frac:5.1%} ({done}/{total}){tail}")
    else:
        sys.stderr.write(f"\r\x1b[K{label} {done}{tail}")
    sys.stderr.flush()

def progress_clear():
    if sys.stderr.isatty():
        sys.stderr.write("\r\x1b[K")
        sys.stderr.flush()

def calculate_hash(file_path, label=None, offset=0):
    hasher = hashlib.md5()
    try:
        total = file_path.stat().st_size if label else 0
        done, start = 0, time.monotonic()
        with open(file_path, 'rb') as f:
            f.seek(offset)
            for i, chunk in enumerate(iter(lambda: f.read(65536), b'')):
                hasher.update(chunk)
                done += len(chunk)
                if label and i % 256 == 0:  # ~16 MB between redraws
                    progress(label, done, total, start=start, unit="B")
        if label:
            progress_clear()
        return hasher.hexdigest()
    except Exception:
        return None

def archive_crcs(file_path):
    """{member: (size, crc32)} read from an archive's directory, None if unreadable.

    The whole point is to not touch the payload. Zip and 7z both record a CRC32
    per member in a header, so two 1.8 GB dumps can be compared on a few KB of
    metadata instead of 3.6 GB of decompression -- which is where this script
    spent nearly all of its wall clock on a real collection.
    """
    ext = file_path.suffix.lower()
    if ext == '.zip':
        try:
            with zipfile.ZipFile(file_path) as z:
                return {i.filename: (i.file_size, i.CRC)
                        for i in z.infolist() if not i.is_dir()} or None
        except (OSError, zipfile.BadZipFile, NotImplementedError):
            return None
    for tool in ARCHIVE_TOOLS.get(ext, ()):
        # unrar's listing has no CRC column; 7z reads .rar anyway.
        if tool == 'unrar' or not shutil.which(tool):
            continue
        try:
            out = subprocess.run([tool, 'l', '-ba', '-slt', str(file_path)],
                                 timeout=120, capture_output=True, text=True,
                                 errors='replace').stdout
        except (OSError, subprocess.SubprocessError):
            continue
        entries, name, attrs = {}, None, {}
        for line in out.splitlines():
            key, sep, val = line.partition(' = ')
            if not sep:
                continue
            key, val = key.strip(), val.strip()
            if key == 'Path':
                name, attrs = val, {}
            elif name is not None and key in ('Size', 'CRC'):
                attrs[key] = val
                if len(attrs) == 2:
                    entries[name] = (attrs['Size'], attrs['CRC'])
        # An encrypted or solid-header archive lists paths with no CRC; refusing
        # to answer sends it to the hash rather than calling it unique.
        return entries or None
    return None

def quick_compare(a, b, trust_mtime=False):
    """False when size alone proves they differ, None when only a hash can tell.

    A size mismatch is proof, and it is the cheap case worth catching: reading
    two multi-GB images over NFS to learn they differ in length was the single
    most expensive thing this script did.

    Equal size plus equal mtime is NOT proof -- a bulk copy stamps a whole
    collection with the same mtime, so two different revisions of equal length
    look identical under that rule and a unique dump would be filed as a
    duplicate. Only --trust-mtime opts into it.

    Equal size sends archives to their stored CRC32s, which settle it on a few
    KB of header. Measured on a real collection, that is 98.7% of the bytes this
    script was reading: .7z pairs alone were 189 GB of the 192 GB it still had
    to hash after the size check.

    ponytail: no partial-content sampling for non-archives (compare first/last
    64 KB, hash on tie). The measured collisions were 98.7% archives, so the
    remaining raw .bin/.iso pairs are not worth the extra path yet.
    """
    try:
        sa, sb = a.stat(), b.stat()
    except OSError:
        return None
    if sa.st_size != sb.st_size:
        return False
    # NFS and FAT round mtime differently on either side, so compare whole seconds.
    if trust_mtime and int(sa.st_mtime) == int(sb.st_mtime):
        return True
    if a.suffix.lower() == b.suffix.lower():
        ca = archive_crcs(a)
        if ca is not None:
            cb = archive_crcs(b)
            if cb is not None:
                return ca == cb
    return None

def header_size(file_path):
    """Length of the dump header No-Intro strips before hashing, 0 if none."""
    try:
        size = file_path.stat().st_size
        with open(file_path, 'rb') as f:
            head = f.read(128)
    except Exception:
        return 0
    if head[1:10] == b'ATARI7800':  # A7800 header is 128 bytes, magic at offset 1
        return 128
    if head[:4] in ROM_HEADERS:
        return ROM_HEADERS[head[:4]]
    # SNES and Genesis copier headers carry no magic at all. A cart dump is a
    # whole number of KB, so the leftover 512 bytes are the only tell -- which
    # a disc image can match by coincidence, so only trust it on cart formats.
    if file_path.suffix.lower() in COPIER_EXTS and size > 512 and size % 1024 == 512:
        return 512
    return 0

def is_smd(file_path):
    """True for a Super Magic Drive dump, whose body is also interleaved."""
    try:
        with open(file_path, 'rb') as f:
            return f.read(10)[8:10] == b'\xaa\xbb'
    except Exception:
        return False

def smd_md5(file_path):
    """MD5 of an SMD dump deinterleaved back into plain Mega Drive byte order.

    Each 16 KB block after the 512-byte copier header holds that block's odd
    bytes first, then its even bytes, so stripping the header is not enough to
    match a DAT -- the body has to be woven back together.
    """
    hasher = hashlib.md5()
    try:
        with open(file_path, 'rb') as f:
            f.seek(512)
            for block in iter(lambda: f.read(16384), b''):
                half = len(block) // 2
                out = bytearray(2 * half)
                out[0::2] = block[half:2 * half]
                out[1::2] = block[:half]
                hasher.update(out)
    except Exception:
        return None
    return hasher.hexdigest()

def dat_lookup(index, file_path, file_md5):
    """Canonical name for a rom, retrying headerless for headered formats."""
    if not index:
        return None
    hit = index.get(file_md5 or "")
    if hit:
        return hit
    candidates = []
    skip = header_size(file_path)
    if skip:
        candidates.append(calculate_hash(file_path, offset=skip))
    if is_smd(file_path):
        candidates.append(smd_md5(file_path))
    for md5 in candidates:
        hit = index.get(md5 or "")
        if hit:
            return hit
    return None

def parse_dat(data):
    """{md5: rom name} from Logiqx XML or clrmamepro text, unwrapping a zip."""
    if data[:2] == b'PK':
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(('.dat', '.xml'))]
            if not names:
                return {}
            data = z.read(names[0])
    if data.lstrip()[:1] == b'<':
        root = ET.fromstring(data)
        pairs = ((r.get('md5'), r.get('name')) for r in root.iter('rom'))
    else:
        text = data.decode('utf-8', 'replace')
        pairs = ((m[2], m[1]) for m in
                 re.finditer(r'rom \(\s*name "(.*?)".*?md5 ([0-9A-Fa-f]{32})', text))
    return {md5.lower(): name for md5, name in pairs if md5 and name}

def fetch_dat(system, max_age=DAT_MAX_AGE):
    """Cached DAT index for a system, re-downloaded when missing or stale."""
    url = DAT_SOURCES.get(system)
    if not url:
        return {}
    cached = DAT_CACHE / f"{system}.dat"
    if not cached.exists() or time.time() - cached.stat().st_mtime > max_age:
        try:
            print(f"[DAT] Fetching {system} from {urllib.parse.urlsplit(url).netloc}...")
            req = urllib.request.Request(urllib.parse.quote(url, safe=':/?&=%'),
                                         headers={'User-Agent': 'romganizer'})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            DAT_CACHE.mkdir(parents=True, exist_ok=True)
            cached.write_bytes(data)
        except Exception as e:
            # A stale copy still identifies almost everything, so prefer it to nothing.
            print(f"[WARN] Could not fetch {system} DAT: {e}")
            if not cached.exists():
                return {}
    try:
        index = parse_dat(cached.read_bytes())
    except Exception as e:
        print(f"[WARN] Unreadable cached DAT {cached}: {e}")
        return {}
    print(f"[DAT] {system}: {len(index)} entries")
    return index

def load_dats(paths):
    """Build {md5: canonical rom name} from Logiqx DAT files (No-Intro, Redump, TOSEC).

    Keyed on MD5 because that is what calculate_hash already produces at a name
    collision, so identification costs no extra read of a multi-GB ISO.

    ponytail: no CRC32/SHA-1 fallback. No-Intro and Redump both publish md5 on
    every rom entry; add the other keys if a DAT source turns up that omits it.
    """
    index = {}
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files = sorted(set(p.rglob('*.dat')) | set(p.rglob('*.xml')) | set(p.rglob('*.zip')))
        else:
            files = sorted(Path(g) for g in glob.glob(raw)) or [p]
        for dat in files:
            try:
                parsed = parse_dat(dat.read_bytes())
            except Exception as e:
                print(f"[WARN] Unreadable DAT {dat}: {e}")
                continue
            before = len(index)
            for md5, name in parsed.items():
                index.setdefault(md5, name)
            print(f"[DAT] {dat.name}: {len(index) - before} new entries")
    return index

def inspect_zip(file_path):
    try:
        with zipfile.ZipFile(file_path, 'r') as z:
            namelist = [n.lower() for n in z.namelist() if not n.endswith('/')]
            if not namelist:
                return 'arcade', None 
            for name in namelist:
                if any(bios_name in name for bios_name in KNOWN_BIOS):
                    return 'bios', None
                for system, exts in SYSTEM_MAPPING.items():
                    for ext in exts:
                        if name.endswith(ext):
                            return system, ext
            return 'arcade', '.zip'
    except zipfile.BadZipFile:
        return None, None

# --- Content sniffing ------------------------------------------------------
# A header read beats shelling out to file(1): no subprocess per file, and far
# better rom coverage. (offset, magic) -- offset None means "search the window",
# for disc images whose markers shift with the sector layout.
NINTENDO_LOGO_GB = b'\xce\xed\x66\x66\xcc\x0d\x00\x0b'
NINTENDO_LOGO_GBA = b'\x24\xff\xae\x51\x69\x9a\xa2\x21'
HEAD_BYTES = 0x10100  # far enough in for the Xbox media marker at 0x10000

# Verified against file(1)'s magic/Magdir/console, plutiedev.com/rom-header and
# smspower.org/Development/ROMHeader. Disc markers come first: they sit at or
# near byte 0 and would otherwise be shadowed by the looser rules below.
MAGIC_RULES = [
    (('segacd',), None, b'SEGADISCSYSTEM'),      # at 0 (2048) or 0x10 (raw 2352)
    (('segacd',), None, b'SEGABOOTDISC'),
    (('saturn',), None, b'SEGA SEGASATURN'),
    (('dreamcast',), None, b'SEGA SEGAKATANA'),
    # The media marker doesn't say which Xbox; both vote and the folder breaks
    # the tie, or it lands as ambiguous rather than silently misfiled.
    (('xbox', 'xbox360'), None, b'MICROSOFT*XBOX*MEDIA'),
    (('nes',), 0, b'NES\x1a'),                   # iNES / NES 2.0
    (('nes',), 0, b'UNIF'),
    (('fds',), 0, b'FDS\x1a'),
    (('fds',), 1, b'*NINTENDO-HVC*'),            # headerless disk image
    (('n64',), 0, b'\x80\x37\x12\x40'),          # .z64, native byte order
    (('n64',), 0, b'\x37\x80\x40\x12'),          # .v64, byte-swapped
    (('n64',), 0, b'\x40\x12\x37\x80'),          # .n64, 32-bit byteswapped
    (('n64',), 0, b'\x12\x40\x80\x37'),          # wordswapped
    (('lynx',), 0, b'LYNX'),
    (('atari7800',), 1, b'ATARI7800'),           # byte 0 is the header version
    (('ngc',), 0x1c, b'\xc2\x33\x9f\x3d'),
    (('wii',), 0x18, b'\x5d\x1c\x9e\xa3'),
    (('wii',), 0, b'WBFS'),
    (('n3ds',), 0x100, b'NCSD'),
    (('n3ds',), 0x100, b'NCCH'),
    (('switch',), 0x100, b'HEAD'),               # XCI cartridge image
    (('switch',), 0, b'PFS0'),                   # NSP
    (('gb',), 0x104, NINTENDO_LOGO_GB),          # gbc split out in sniff()
    (('gba',), 0x04, NINTENDO_LOGO_GBA),
    (('nds',), 0xc0, NINTENDO_LOGO_GBA),
    (('xbox',), 0, b'XBEH'),                     # .xbe executable
    (('xbox360',), 0, b'XEX2'),
    (('xbox360',), 0, b'XEX1'),
    (('ngp',), 0x0a, b'BY SNK CORPORATION'),     # ngpc split out in sniff()
    # TMSS refuses to boot a cart whose 0x100 field doesn't start with "SEGA",
    # so that prefix -- not the full "SEGA MEGA DRIVE"/"SEGA GENESIS" strings --
    # is the reliable test. 32X carts announce themselves in the same field.
    (('sega32x',), 0x100, b'SEGA 32X'),
    (('megadrive',), 0x100, b'SEGA'),
    (('megadrive',), 0x101, b'SEGA'),            # some dumps are shifted a byte
]

# Archives whose first member can be piped out cheaply. 7z reads .rar too when the
# codec is installed; unrar is the fallback.
ARCHIVE_TOOLS = {'.7z': ['7z', '7zz'], '.rar': ['7z', '7zz', 'unrar']}
ARCHIVE_PEEK = 4 << 20  # enough for the iso9660 root directory, not the whole game

# Formats that can only be read by unpacking the whole image. Each entry is
# (tool, [(argv, file to sniff afterwards), ...]) tried in order.
EXTRACTORS = {
    '.chd': ('chdman', lambda src, d: [
        (['chdman', 'extractcd', '-f', '-i', src, '-o', f'{d}/o.cue', '-ob', f'{d}/o.bin'], f'{d}/o.bin'),
        (['chdman', 'extractraw', '-f', '-i', src, '-o', f'{d}/o.img'], f'{d}/o.img'),
    ]),
    '.rvz': ('dolphin-tool', lambda src, d: [
        (['dolphin-tool', 'convert', '-f', 'iso', '-i', src, '-o', f'{d}/o.iso'], f'{d}/o.iso'),
    ]),
}

# Nothing here can be read at all: no seekable format, no tool.
SEALED_EXTS = {'.gz', '.wbfs'}

class CsoReader:
    """Seekable read-only view of a .cso/.ciso image, inflating blocks on demand.
    stdlib zlib only -- maxcso can't stream a partial image anyway, and the whole
    point is to reach sector 16 and the root directory without unpacking 4GB."""

    def __init__(self, fh):
        self.fh, self.pos = fh, 0
        fh.seek(0)
        magic, _hdr, self.total, self.block, _ver, self.align = struct.unpack(
            '<4sIQIBB', fh.read(0x16))
        if magic != b'CISO':
            raise ValueError('not a cso')

    def _index(self, n):
        self.fh.seek(0x18 + n * 4)
        return struct.unpack('<I', self.fh.read(4))[0]

    def _read_block(self, n):
        start, end = self._index(n), self._index(n + 1)
        self.fh.seek((start & 0x7fffffff) << self.align)
        raw = self.fh.read(max(((end & 0x7fffffff) << self.align) -
                               ((start & 0x7fffffff) << self.align), 0))
        if start & 0x80000000:                   # stored, not deflated
            return raw[:self.block]
        return zlib.decompressobj(-15).decompress(raw, self.block)

    def seek(self, pos, whence=0):
        self.pos = pos

    def read(self, size):
        out = b''
        while size > 0 and self.pos < self.total:
            chunk = self._read_block(self.pos // self.block)[self.pos % self.block:][:size]
            if not chunk:
                break
            out, self.pos, size = out + chunk, self.pos + len(chunk), size - len(chunk)
        return out

def _iso9660_root(fh):
    """Root directory of an ISO9660 image as {NAME: (lba, size)}, plus the sector
    geometry, or ({}, 0, 0). Raw 2352-byte dumps (.bin) prefix each sector with a
    16-byte sync header, which shifts every offset -- so try both layouts."""
    for sector, skip in ((2048, 0), (2352, 16)):
        fh.seek(16 * sector + skip)              # sector 16 = primary volume descriptor
        pvd = fh.read(190)
        if len(pvd) < 190 or pvd[1:6] != b'CD001':
            continue
        lba = int.from_bytes(pvd[158:162], 'little')   # root record's extent
        size = int.from_bytes(pvd[166:170], 'little')
        data = _read_iso_extent(fh, sector, skip, lba, min(size, 1 << 20))
        entries, pos = {}, 0
        while pos + 33 < len(data):
            rec_len = data[pos]
            if rec_len == 0:                     # padding to the next sector
                pos = (pos // 2048 + 1) * 2048
                continue
            name_len = data[pos + 32]
            name = data[pos + 33:pos + 33 + name_len].split(b';')[0].upper()
            entries[name] = (int.from_bytes(data[pos + 2:pos + 6], 'little'),
                             int.from_bytes(data[pos + 10:pos + 14], 'little'))
            pos += rec_len
        return entries, sector, skip
    return {}, 0, 0

def _read_iso_extent(fh, sector, skip, lba, size):
    """Read `size` bytes from sector `lba`, stepping sector by sector so the raw
    dump's per-sector headers get skipped rather than landing in the data."""
    out = b''
    for i in range((size + 2047) // 2048):
        fh.seek((lba + i) * sector + skip)
        out += fh.read(2048)
    return out[:size]

def sniff_disc(fh):
    """PSX, PS2 and PSP all look identical from the outside: same extensions,
    same ISO9660. The boot file in the root directory is what tells them apart."""
    entries, sector, skip = _iso9660_root(fh)
    if not entries:
        return ()
    if b'SYSTEM.CNF' in entries:
        lba, size = entries[b'SYSTEM.CNF']
        cnf = _read_iso_extent(fh, sector, skip, lba, min(size, 4096))
        # Inside an archive we only ever see the first few MB, and SYSTEM.CNF
        # usually lives past that: the read comes back empty. Say nothing then,
        # because "no BOOT2" would otherwise call every PS2 disc a PS1 one.
        if b'BOOT2' in cnf:
            return ('ps2',)
        return ('psx',) if b'BOOT' in cnf else ()
    if b'PSP_GAME' in entries or b'UMD_DATA.BIN' in entries:
        return ('psp',)
    return ()

def sniff(file_path, deep=False):
    """Identify a file by its own bytes. Content can't lie about what it is, so
    this outranks both the extension and the folder name. Returns a tuple of
    candidate systems (usually one, empty when nothing matches).

    `deep` permits unpacking a whole image with an external tool -- slow, so the
    caller only asks once the cheap evidence has come up short."""
    ext = file_path.suffix.lower()
    if ext in SEALED_EXTS or ext == '.zip':
        return ()
    if ext in ARCHIVE_TOOLS:
        return sniff_archive(file_path)
    if ext in EXTRACTORS:
        return sniff_extracted(file_path) if deep else ()
    try:
        with open(file_path, 'rb') as fh:
            return sniff_stream(CsoReader(fh) if ext in ('.cso', '.ciso') else fh)
    except (OSError, ValueError, zlib.error, struct.error):
        return ()

def sniff_archive(file_path):
    """Pipe the first member's header through the same magic table. Falls back to
    the member names, which is all inspect_zip ever had to go on."""
    for tool in ARCHIVE_TOOLS[file_path.suffix.lower()]:
        if not shutil.which(tool):
            continue
        listing, extract = ((['lb'], ['p', '-inul']) if tool == 'unrar'
                            else (['l', '-ba', '-slt'], ['e', '-so']))
        try:
            out = subprocess.run([tool] + listing + [str(file_path)], timeout=60,
                                 capture_output=True, text=True, errors='replace').stdout
            names = [l[7:].strip() for l in out.splitlines() if l.startswith('Path = ')]
            names = names or [l.strip() for l in out.splitlines() if l.strip()]
            if not names:
                return ()
            proc = subprocess.Popen([tool] + extract + [str(file_path), names[0]],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                systems = sniff_stream(io.BytesIO(proc.stdout.read(ARCHIVE_PEEK)))
            finally:
                proc.kill()
                proc.wait()
            if systems:
                return systems
        except (OSError, subprocess.SubprocessError):
            continue
        # Bytes said nothing; let a member's own extension speak.
        for name in names:
            inner = Path(name).suffix.lower()
            if inner not in AMBIGUOUS_EXTS:
                hit = [s for s, exts in SYSTEM_MAPPING.items() if inner in exts]
                if len(hit) == 1:
                    return tuple(hit)
        return ()
    return ()

# A chd unpacks to roughly three times its own size, and an rvz to about two.
# Three is the safe multiplier to ask for before starting.
EXTRACT_FACTOR = 3

def scratch_dir():
    """Where to unpack a whole game image.

    Not the default temp dir: on systemd distros /tmp is tmpfs, so unpacking a
    1.7 GB chd there is a multi-gigabyte RAM allocation and the OOM killer ends
    the run. /var/tmp is disk-backed by the FHS and is what large temp data is
    for. An explicit TMPDIR still wins -- the user knows their disks."""
    if os.environ.get('TMPDIR'):
        return os.environ['TMPDIR']
    return '/var/tmp' if os.path.isdir('/var/tmp') else None

def sniff_extracted(file_path):
    """chd and rvz only give up their contents by being unpacked whole, into a
    temp copy as big as the game. Gated on nothing cheaper having worked."""
    tool, attempts = EXTRACTORS[file_path.suffix.lower()]
    if not shutil.which(tool):
        return ()
    scratch = scratch_dir()
    try:
        need = file_path.stat().st_size * EXTRACT_FACTOR
        free = shutil.disk_usage(scratch or tempfile.gettempdir()).free
    except OSError:
        return ()
    if free < need:
        # Better to file it on the path than to fill the disk finding out.
        print(f"[WARN] {file_path.name}: need {_size(need)} scratch to unpack it, "
              f"{_size(free)} free; classifying without looking inside")
        return ()
    with tempfile.TemporaryDirectory(dir=scratch) as td:
        for argv, target in attempts(str(file_path), td):
            try:
                # Output is never read, and chdman redraws a progress bar for up
                # to half an hour: capturing it buffers all of that for nothing.
                subprocess.run(argv, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=1800)
            except (OSError, subprocess.SubprocessError):
                continue
            if os.path.exists(target) and os.path.getsize(target):
                return sniff(Path(target))
    return ()

def sniff_stream(fh):
    """The magic table plus the iso9660 probe, against anything with read/seek."""
    fh.seek(0)
    head = fh.read(HEAD_BYTES)
    # Sega 8-bit: the header floats, and its last byte gives the machine.
    # Region nibble 3/4 = Master System, 5/6/7 = Game Gear.
    for at in (0x1ff0, 0x3ff0, 0x7ff0):
        if head.startswith(b'TMR SEGA', at) and len(head) > at + 0x0f:
            region = head[at + 0x0f] & 0xf0
            return ('mastersystem',) if region in (0x30, 0x40) else ('gamegear',)
    for systems, offset, magic in MAGIC_RULES:
        found = (head.startswith(magic, offset) if offset is not None
                 else magic in head)
        if found:
            if systems == ('gb',) and head[0x143:0x144] and head[0x143] & 0x80:
                return ('gbc',)
            if systems == ('ngp',) and head[0x23:0x24] == b'\x10':
                return ('ngpc',)
            return systems
    return sniff_disc(fh) if b'CD001' in head[:0x9400] else ()

def sniff_cue(file_path):
    """A .cue is a text pointer to the track it describes; sniff that instead."""
    try:
        text = file_path.read_text(errors='replace')
    except OSError:
        return ()
    match = re.search(r'^\s*FILE\s+"?([^"\n]+?)"?\s+\w+\s*$', text, re.M)
    if not match:
        return ()
    track = file_path.parent / Path(match.group(1).strip()).name
    return sniff(track) if track.exists() else ()

# Vote weights. Bytes beat everything. An extension unique to one system outranks
# a directory name (dirs lie: "Nintendo" holds eight consoles), but a directory
# name outranks a shared extension, which claims almost nothing on its own.
W_MAGIC, W_EXT_UNIQUE, W_EXT_SHARED, W_HINT_NEAR, W_HINT_FAR = 4, 3, 1, 2, 1
# "roms/<system>/" is a deliberate statement, not a guess, so it outranks even
# the file's own contents: an .nes filed under roms/fds/ is where its owner put it.
W_SYSTEM_DIR = W_MAGIC + 1

def system_dir(file_path):
    """The system named by a "roms/<system>/" directory on this path, if any.

    Frontends (EmulationStation, Batocera, RetroPie) lay a library out that way,
    and they name systems this script has never heard of -- intellivision,
    openbor, zxspectrum, fbneo. The name is taken as written: it is a deliberate
    statement by whoever built the library, not something to second-guess."""
    root = system_dir_root(file_path)
    if not root:
        return None
    bare = re.sub(r'[^a-z0-9]', '', root.name.lower())
    return DIR_ALIASES.get(bare, bare) if bare else None

def system_dir_root(file_path):
    """The "roms/<system>/" directory itself, so what sits below it can be
    carried across at the same relative path."""
    lowered = [p.lower() for p in file_path.parts[:-1]]
    for i, part in enumerate(lowered[:-1]):
        if part == 'roms' and re.sub(r'[^a-z0-9]', '', lowered[i + 1]):
            return Path(*file_path.parts[:i + 2])
    return None

def _hint_votes(file_path, candidates):
    """System hints per path component, innermost first. The filename and its own
    directory are near evidence; a grandparent naming the whole dump is not.

    One vote per component, because a component names one system: the longest
    matching hint wins ("Wii U" is wiiu, not wii), and a component that matches
    several equally ("Atari 2600 5200 7800 XEGS") goes to whichever the
    extension also allows."""
    votes = {}
    # The frontend layout: the directory directly under "roms" names the system,
    # and names it for systems this script has never heard of -- intellivision,
    # openbor, zxspectrum, fbneo. Take the name as written rather than dropping
    # the file into whatever an ancestor directory happened to be called.
    named = system_dir(file_path)
    if named:
        votes[named] = W_SYSTEM_DIR

    # The filename itself never votes. It is a title, and titles contain words
    # like "arcade", "saturn" or "wii" that name nothing: "Gran Turismo 2 (USA)
    # (Arcade Mode).cue", "L.dash.xex.arcade.xzp". Directories are how people
    # actually sort by system, so only those are evidence.
    for depth, part in enumerate(reversed([p.lower() for p in file_path.parts[:-1]]), 1):
        # Bracketed groups describe the dump, not the machine: "(Arcade Mode)",
        # "(Wii Virtual Console)", "[b]". A system folder is never bracketed.
        part = re.sub(r'[\(\[][^\)\]]*[\)\]]', ' ', part)
        # Nearer directories are more specific about the file, so a hint's weight
        # decays with distance: "roms/ports/Doom" is a pc port even with an
        # "arcade" directory sitting five levels above it.
        weight = W_HINT_FAR + (W_HINT_NEAR - W_HINT_FAR) / depth
        matches = [(h, s) for s, hints in CONTEXT_HINTS for h in hints if h in part]
        # A hint contained in a longer one is the same name seen twice, not a
        # second system: "Wii U" matches 'wii' and 'wii u' -- only 'wii u' counts.
        matches = [(h, s) for h, s in matches
                   if not any(h != o and h in o for o, _ in matches)]
        if not matches:
            # CONTEXT_HINTS only covers systems that share an extension. A dir
            # named after any other system ("SNES", "gamegear") still counts.
            bare = DIR_ALIASES.get(re.sub(r'[^a-z0-9]', '', part), re.sub(r'[^a-z0-9]', '', part))
            if bare in SYSTEM_MAPPING:
                votes.setdefault(bare, weight)
            continue
        top = [s for _, s in matches]  # CONTEXT_HINTS order preserved
        system = next((s for s in top if s in candidates), top[0])
        votes.setdefault(system, weight)
    return votes

# "bios" followed by more letters is usually another word -- "bioship" is an
# arcade game, "bioshock" is not firmware. It is still a real prefix when what
# follows is a machine abbreviation: biosnds7.bin, biosdsi9.bin. Letters before
# it are fine either way (STBIOS.bin).
BIOS_WORD = re.compile(r'bios(?![a-z])|bios[a-z]{0,4}\d')

# Every Amiga Kickstart is "kick" + its version, and the machine it is for is the
# extension: kick33180.A500, kick40060.CD32, kick34005.CDTV. Firmware, not a
# game, wherever it turns up -- outside a bios directory too.
KICKSTART = re.compile(r'^kick\d+\b')

def is_bios_name(file_name):
    """A bios is a bios whatever machine it belongs to -- that is a separate
    question, answered by the usual signals."""
    name = file_name.lower()
    return (name in KNOWN_BIOS or bool(BIOS_WORD.search(name))
            or bool(KICKSTART.match(name)))

def bios_root(file_path):
    """The "bios/" or "system/" directory this file lives under, if any.

    Firmware carries every extension there is -- .A500 kickstarts, Atari .img
    TOS images, keropi's iplrom.dat, VICE's extensionless PET roms -- so no
    extension table will ever cover it. The directory is the reliable signal,
    and it also says where the file goes: RetroArch and Batocera both want the
    layout below it kept (system/vice/PET/chargen), not flattened."""
    if in_saves(file_path):
        return None
    return next((d for d in file_path.parents if d.name.lower() in ('bios', 'system')), None)

def classify(file_path, deep=False):
    """Which system, and the evidence for it. A bios keeps its system: that is
    what tells bios/arcade/ apart from bios/psx/."""
    system, evidence = _classify(file_path, deep)
    in_bios = bios_root(file_path) is not None
    junk = file_path.suffix.lower() in (BIOS_JUNK_EXTS if in_bios else NON_ROM_EXTS)
    # Artwork and readmes sit in bios directories too, and a game whose name
    # merely contains "bios" is not firmware: neither gets rescued.
    if not junk and (in_bios or is_bios_name(file_path.name)):
        # Nothing else identified it, so all we know is that it is a bios.
        if system in ('unknown', 'ambiguous'):
            system = 'bios'
        evidence = ['name=bios'] + evidence
    return system, evidence

def _classify(file_path, deep=False):
    """Score every signal and let the highest total win, instead of trusting
    whichever signal happens to be checked first.

    Returns (system, evidence). `evidence` is a list of "source=system(+weight)"
    strings, prefixed with a 'conflict: ...' note when a unique extension and a
    directory name name different systems -- the extension still wins, but the
    disagreement is recorded rather than swallowed."""
    ext = file_path.suffix.lower()
    name = file_path.name.lower()

    # Ahead of the extension table: a .srm is a save wherever it sits, and the
    # generic "not a rom" answer would hide why it was left behind.
    if in_saves(file_path):
        # Skipping leaves them where they are, which is where an emulator
        # expects to find them.
        return 'unknown', ['a save, state or nvram file, not a dump']
    if ext in NON_ROM_EXTS and not (ext not in BIOS_JUNK_EXTS and bios_root(file_path)):
        # Box art, dat files and readmes sit in the same system-named folders as
        # the roms. Without this they'd inherit the folder's system below.
        # .md is both Markdown and a Mega Drive dump, so let the bytes decide:
        # TMSS won't boot a cart without "SEGA" at 0x100, so a real dump has it.
        if not (ext == '.md' and sniff(file_path)):
            return 'unknown', [f'ext{ext}=not a rom']
    if SPLIT_VOLUME.search(name):
        return 'unknown', ['one volume of a split archive, not a dump']
    named = system_dir(file_path)
    if named:
        # Outranks the contents too, so it is answered before reading the file.
        return named, [f'roms/{named}/=system dir(+{W_SYSTEM_DIR})']
    if ext == '.zip':
        system, _ = inspect_zip(file_path)
        # "bios" names no machine -- a multi-system pack looks like that -- so
        # keep scoring and let the path say which shelf it belongs on.
        if system and system != 'bios':
            return system, [f'zip={system}']

    sniffed = sniff_cue(file_path) if ext == '.cue' else sniff(file_path, deep)
    candidates = [s for s, exts in SYSTEM_MAPPING.items() if ext in exts]
    # A .7z is a container, not a system. MAME sets ship as .7z, but so does any
    # dump someone archived. If we couldn't read inside, the path decides alone --
    # otherwise a psx redump set would file itself as arcade.
    if ext in ARCHIVE_TOOLS and not sniffed:
        candidates = []
    hints = _hint_votes(file_path, set(candidates) | set(sniffed))
    w = W_EXT_SHARED if ext in AMBIGUOUS_EXTS else W_EXT_UNIQUE

    scores, evidence = {}, []
    for system in sniffed:
        scores[system] = scores.get(system, 0) + W_MAGIC
        evidence.append(f'magic={system}(+{W_MAGIC})')
    for system in candidates:
        scores[system] = scores.get(system, 0) + w
        evidence.append(f'ext{ext}={system}(+{w})')
    for system, weight in hints.items():
        scores[system] = scores.get(system, 0) + weight
        evidence.append(f'path={system}(+{weight})')

    if not scores:
        return 'unknown', evidence
    # sorted() first so equal scores break deterministically, not by dict order.
    ranked = sorted(scores, key=lambda s: (-scores[s], s))
    best = ranked[0]
    if len(ranked) > 1 and scores[ranked[1]] == scores[best]:
        # Two systems with identical support: pick neither. This is the Xbox
        # media marker with no folder to break the tie.
        return 'ambiguous', evidence
    if candidates and scores[best] < 2:
        # A shared extension with nothing backing it is a guess, not an answer.
        return 'ambiguous', evidence
    if sniffed:
        # Bytes win outright; only report where the label disagreed with them.
        # A shared extension isn't a claim, so it can't disagree.
        disagree = []
        if w == W_EXT_UNIQUE and candidates and best not in candidates:
            disagree.append(f"{ext} says {'/'.join(candidates)}")
        if hints and best not in hints:
            disagree.append(f"path says {'/'.join(sorted(hints))}")
        if disagree:
            evidence.insert(0, f"conflict: contents say {best}, " + ', '.join(disagree))
        return best, evidence
    if not candidates:
        # Extension means nothing to us (a bad dumper's .rom variant, a system we
        # don't list). The path is then the only evidence there is, so any hint
        # beats skipping the file -- but say so, since it wasn't corroborated.
        evidence.insert(0, f'inferred: unrecognized {ext or "(no extension)"}, '
                           f'filed as {best} from the path alone')
        return best, evidence
    if w == W_EXT_UNIQUE and hints and best not in hints:
        evidence.insert(0, f"conflict: {ext} says {best}, path says {'/'.join(sorted(hints))}")
    return best, evidence

def determine_system(file_path):
    return classify(file_path)[0]

def _strip_tags(text):
    """Drop disc/track/special-tag brackets, so a set collapses to one name."""
    # 'track' keeps multi-track .bin sets in the same folder as their .cue
    out = re.sub(r'\s*[\(\[][^\]\)]*(disc|cd|dvd|side|track)\s*\d+[^\]\)]*[\)\]]', '', text, flags=re.IGNORECASE).strip()
    return re.sub(r'\s*[\(\[][^\]\)]*(' + '|'.join(SPECIAL_TAGS) + r')[^\]\)]*[\)\]]', '', out, flags=re.IGNORECASE).strip()

# Rippers that cut a disc into volumes number them in the stem rather than the
# extension: "SSX (USA).iso01.iso" beside "SSX (USA).cue". One volume or thirty,
# it is the same game, so the number comes off before the name is compared.
VOLUME_SUFFIX = re.compile(r'\.(?:iso|bin|img|cue|7z|zip|rar|part)\d{1,3}$', re.I)

def game_stem(file_name):
    """Game name with disc/track/tag brackets stripped -- the folder a set belongs in."""
    return _strip_tags(VOLUME_SUFFIX.sub('', Path(file_name).stem).rstrip(' .'))

def names_a_system(path):
    """True for "ports", "psx", "Sony PlayStation 2 [Redump]" -- a directory that
    sorts games rather than being one. Its name can never be a game's name."""
    part = re.sub(r'[\(\[][^\)\]]*[\)\]]', ' ', path.name.lower())
    bare = re.sub(r'[^a-z0-9]', '', part)
    return (DIR_ALIASES.get(bare, bare) in SYSTEM_MAPPING
            or any(h in part for _, hints in CONTEXT_HINTS for h in hints))

def folder_label(dir_name):
    """The game a directory holds, from the directory's own name.

    Not game_stem(): a release folder is not a filename, so "ICO.and.Shadow.of
    .the.Colossus.PS3 DUPLEX" must keep everything after its last dot. A leading
    list index goes ("52. Digimon World" -> "Digimon World") and "(Disc 1)" goes
    with it, so the discs of one game still land in one folder."""
    return _strip_tags(re.sub(r'^\d+\s*[.\-_)]\s*', '', dir_name))

def build_destination_path(dest_base, system, file_name, is_bios=False, force_folder=False):
    """Builds the destination path: /dest/roms/{platform}/ or /dest/bios/{platform}/."""
    name_lower = file_name.lower()
    clean_folder_name = game_stem(file_name)

    # Capture sub-tag nesting
    sub_tag_folder = None
    for tag in SPECIAL_TAGS:
        if f"({tag})" in name_lower or f"[{tag}]" in name_lower or f"_{tag}" in name_lower or f"-{tag}" in name_lower:
            sub_tag_folder = tag
            break

    # /dest/roms/{platform}/ or /dest/bios/{platform}/
    # "bios" as the system means nothing named the machine, so there is no
    # second level to add: bios/, not bios/bios/.
    root_dir = dest_base / ("bios" if is_bios else "roms")
    if not (is_bios and system == 'bios'):
        root_dir = root_dir / system

    # Package files for disc-based environments inside dedicated directories
    if (force_folder or system in ['psx', 'ps2', 'segacd', 'saturn', 'dreamcast', 'ngc']) and not is_bios:
        if sub_tag_folder:
            target_dir = root_dir / clean_folder_name / sub_tag_folder
        else:
            target_dir = root_dir / clean_folder_name
    else:
        target_dir = root_dir

    return target_dir, target_dir / file_name

def main():
    parser = argparse.ArgumentParser(description="ROM library organizer.")
    parser.add_argument("source", nargs='+', help="One or more source directories, files, or globs containing unorganized assets")
    parser.add_argument("dest", help="Destination base path library folder")
    parser.add_argument("--dry-run", action="store_true", help="Simulate execution pathways safely")
    parser.add_argument("--trust-mtime", action="store_true", help="Treat same-size same-mtime collisions as duplicates without hashing. Fast, but a bulk copy gives a whole collection one mtime, so two different revisions of equal length would be filed as duplicates")
    parser.add_argument("--mode", choices=['move', 'copy'], default='move', help="Move (default) or copy files, leaving source in place")
    parser.add_argument("--changelog-format", choices=['json', 'csv'], default='json', help="Output logs extension configuration array")
    parser.add_argument("--dat", nargs='+', default=[], metavar="PATH", help="DAT file(s), glob, or directory of them (Logiqx XML or clrmamepro, zipped or not). Used to identify the exact revision behind a name collision instead of quarantining it in extra/")
    parser.add_argument("--dat-auto", action="store_true", help=f"Download the matching No-Intro/Redump DAT on demand, cached in {DAT_CACHE} and refreshed when older than {DAT_MAX_AGE // 86400} days")
    
    args = parser.parse_args()
    dest_base = Path(args.dest)

    # The shell expands globs when it can; glob.glob only has work to do for
    # patterns that arrive intact (quoted, or nullglob-off with no match).
    sources = []
    for raw in args.source:
        if os.path.exists(raw):
            sources.append(Path(raw))
            continue
        matched = sorted(glob.glob(raw, recursive=True))
        if matched:
            sources.extend(Path(m) for m in matched)
        else:
            print(f"[ERROR] Source '{raw}' does not exist and matched nothing.")
            sys.exit(1)

    dat_index = load_dats(args.dat) if args.dat else {}
    auto_dats = {}  # system -> index, fetched lazily so a clean run downloads nothing

    changelog = []
    summary = {"organized": 0, "duplicate": 0, "extra": 0, "skipped": 0, "write_error": 0}
    bytes_by = {"organized": 0, "duplicate": 0, "extra": 0}
    # system -> [count, bytes], organized only: this answers "what is filling the
    # library", and duplicates/extras never land in roms/.
    by_system = {}
    skipped_exts = set()

    for s in sources:
        print(f"Scanning: {s} -> {dest_base}")
    if args.dry_run: print("!!! DRY-RUN PATH ACTIVE: SIMULATION ONLY !!!\n")

    # Phase 1: walk. No total to compare against, so this counts rather than bars.
    # Overlapping sources are legal input, so dedupe on resolved path: the same
    # file reached twice would otherwise be "organized", then flagged its own duplicate.
    seen = set()
    files = []
    start = time.monotonic()
    for s in sources:
        candidates = s.rglob('*') if s.is_dir() else [s]
        for p in candidates:
            if p.is_file() and not p.name.startswith('.'):
                key = p.resolve()
                if key in seen:
                    continue
                seen.add(key)
                files.append(p)
                progress("Scanning... found", len(files), start=start)
    files.sort()
    progress_clear()

    # Phase 2: classify (opens every .zip, so it is not free).
    resolved, notes = {}, {}
    start = time.monotonic()
    for i, p in enumerate(files, 1):
        resolved[p], evidence = classify(p)
        tool = EXTRACTORS.get(p.suffix.lower(), (None,))[0]
        if resolved[p] in ('ambiguous', 'unknown') and tool and shutil.which(tool):
            # Last resort: unpack the whole image with chdman/dolphin-tool. Slow
            # and needs temp space the size of the game, so only for files the
            # cheap evidence couldn't place.
            progress_clear()
            print(f"[EXTRACT] unpacking {p.name} to identify it...")
            resolved[p], evidence = classify(p, deep=True)
        kind = evidence[0].split(':')[0] if evidence else ''
        if kind in ('conflict', 'inferred'):
            notes[p] = evidence[0]
            progress_clear()
            print(f"[{kind.upper()}] {p.resolve()}")
            print(f"{'->':>10} {evidence[0]}; filing as {resolved[p]}")
        progress("Identifying", i, len(files), start=start)
    progress_clear()

    # Sidecars (.bin tracks beside a .cue) inherit whatever their folder resolved
    # to, so a disc set is not split across systems or dropped as ambiguous.
    # ponytail: first resolved system per folder wins; revisit if you keep mixed
    # systems in one directory.
    folder_system = {}
    for p, s in resolved.items():
        if s not in ('unknown', 'ambiguous', 'bios'):
            folder_system.setdefault(p.parent, s)
    for p, s in resolved.items():
        if s == 'ambiguous' and p.parent in folder_system:
            resolved[p] = folder_system[p.parent]

    # A game that ships in its own folder brings generic sidecars with it
    # (Game.ini, pcsx.cfg, memcard files). Pooled into a shared system dir those
    # collide between games, so the whole folder travels as a unit instead.
    # ponytail: detected per directory, not per emulator; add an emulator-layout
    # table only if a real layout breaks this.
    dir_files = {}
    for p in files:
        dir_files.setdefault(p.parent, []).append(p)

    dir_dest = {}  # source dir -> (target dir, system) for whole-folder moves

    # Layout-identified dumps first: the marker claims its whole folder, so the
    # per-directory rule below never gets to split one up.
    marker_roots = {}
    for p in files:
        marker = FOLDER_MARKERS.get(p.name.lower())
        if not marker or in_saves(p):
            continue
        if callable(marker):
            marker = marker(p)
        if not marker:
            continue
        root = p.parent
        while root.name.upper() in ('PS3_GAME', 'PS3_UPDATE', 'USRDIR'):
            root = root.parent  # the marker sits inside the rip, not at its root
        marker_roots[root] = marker
        # The folder name is the release name; keep it verbatim rather than
        # running it through game_stem, which would eat "[...]" title brackets.
        dir_dest[root] = (dest_base / 'roms' / marker / root.name, marker)
        for q in files:
            if root in q.parents:
                resolved[q] = marker

    for parent, group in dir_files.items():
        if parent in dir_dest or any(a in dir_dest for a in parent.parents):
            continue  # already claimed by a folder marker
        if in_saves(parent / '_'):
            # A folder move bypasses classify(), which is the only thing keeping
            # nvram, save states and mame cfg out of the library. Guard it here
            # too, or "saves/mame/mame2003/cfg/" lands in "roms/arcade/cfg/".
            continue
        if system_dir_root(parent / 'x') == parent:
            # "roms/<system>/" sorts games, it is never one of them. A library
            # that happens to hold a single title would otherwise be folded into
            # a folder named after it, taking the whole media tree with it.
            continue
        roms = [p for p in group
                if p.suffix.lower() in ROM_EXTS and resolved[p] not in ('unknown', 'ambiguous', 'bios')]
        sidecars = [p for p in group if p.suffix.lower() not in ROM_EXTS]
        if not roms or (len(roms) == 1 and not sidecars):
            continue  # a lone file in a folder is just a file; leave it alone
        # One game per folder, but a title ships with its DLC and unlock packages
        # ("Darkstalkers.Resurrection.pkg" beside "...Resurrection.Unlock.pkg"),
        # so names only have to agree on a substantial prefix -- enough that two
        # unrelated titles in a library directory can never pass.
        # More supporting files than dumps also means one game: a pc port is a
        # pile of data files around a couple of executables (WOLF3D.EXE beside
        # CATALOG.EXE). A library directory is the other way round.
        stems = {s for s in (game_stem(p.name) for p in roms) if not GENERIC_STEM.match(s)}
        if len({resolved[p] for p in roms}) > 1 or (
                len(stems) > 1 and len(sidecars) <= len(roms)
                and len(os.path.commonprefix(list(stems))) < 8):
            continue  # more than one game in here; don't guess
        # One game here, and a marked rip below: this is the release folder that
        # wraps it, so it keeps its own name and the rar set and .nfo beside the
        # rip come with it. Checked after the one-game guards so a library
        # directory that merely contains a rip is never swallowed whole.
        wrapped = next((s for r, s in marker_roots.items() if parent in r.parents), None)
        if wrapped:
            dir_dest[parent] = (dest_base / 'roms' / wrapped / parent.name, wrapped)
            for q in group:
                resolved[q] = wrapped
            continue
        # Shortest name first, so the system is read off the base game rather
        # than its DLC; largest file breaks a tie between equal-length names.
        primary = min(roms, key=lambda p: (
            len(game_stem(p.name)), -(p.stat().st_size if p.exists() else 0)))
        system = resolved[primary]
        # The directory names the game, never a file inside it. Six preconfigured
        # Tomb Raider folders all ship "OpenLara.exe", and every repack ships
        # "Setup.exe" -- naming by file collapses unrelated games into one folder.
        label = '' if names_a_system(parent) else folder_label(parent.name)
        if not label:
            # The directory sorts games rather than being one ("PSX/"), so the
            # files are all there is to go on.
            label = game_stem(primary.name)
        # A folder unit always gets a folder: dropping 44 loose Wii U files into
        # roms/wiiu/ beside the next game's is how a library gets destroyed.
        dir_dest[parent] = (dest_base / 'roms' / system / label, system)

    # Directories holding no dump at all, just loose data files -- a pc port's
    # data folder, an emulator's config directory. Their names are generic, so
    # two of them ("APPS/LAUNCHDISC/title.cfg", "APPS/LAUNCHELF/title.cfg")
    # collide unless each keeps its own directory. Artwork and readmes stay
    # skipped: those are known to be droppable, an unrecognized extension is not.
    # Outermost first, so "Quake/id1" is claimed by "Quake" rather than itself.
    roots = {s.resolve() for s in sources if s.is_dir()}

    def holds_a_dump(path):
        return any(p.suffix.lower() in ROM_EXTS and resolved[p] not in ('unknown', 'ambiguous')
                   for p in dir_files.get(path, ()))

    for parent in sorted(dir_files, key=lambda p: len(p.parts)):
        group = dir_files[parent]
        if parent in dir_dest or parent.resolve() in roots:
            continue
        if any(a in dir_dest for a in parent.parents):
            continue  # rides along with the folder above it
        if in_saves(parent / '_'):
            # A folder move bypasses classify(), which is the only thing keeping
            # nvram, save states and mame cfg out of the library. Guard it here
            # too, or "saves/mame/mame2003/cfg/" lands in "roms/arcade/cfg/".
            continue
        if holds_a_dump(parent):
            continue  # a library the rule above already judged
        if not any(p.suffix.lower() not in NON_ROM_EXTS for p in group):
            continue
        # The game is the outermost folder that still names only this game: walk
        # up past "Quake/id1", but stop at a system directory such as ports/.
        root = parent
        while (root.parent != root and root.parent not in dir_dest
               and root.parent.resolve() not in roots
               and not names_a_system(root.parent) and not holds_a_dump(root.parent)):
            root = root.parent
        members = [p for p in files if root in p.parents]
        systems = {resolved[p] for p in members} - {'unknown', 'ambiguous', 'bios'}
        if len(systems) > 1:
            continue
        if systems:
            system = systems.pop()
        else:
            # Nothing inside names a system (a pc port is all opaque data), so
            # the directories above are the only evidence there is.
            votes = _hint_votes(root / 'x', set())
            if not votes:
                continue
            system = max(votes, key=votes.get)
        dir_dest[root] = (dest_base / 'roms' / system / root.name, system)
        for p in members:
            resolved[p] = system

    # Nested folders inside a claimed folder ride along at the same relative path.
    for parent in dir_files:
        if parent in dir_dest:
            continue
        for anc in parent.parents:
            if anc in dir_dest:
                target_dir, system = dir_dest[anc]
                dir_dest[parent] = (target_dir / parent.relative_to(anc), system)
                break

    # Phase 3a: a unit whose destination is still free travels as one folder
    # operation -- on the same filesystem that is a single rename, and in move
    # mode it leaves no empty source folder behind.
    # ponytail: destination already present -> fall through to the per-file loop
    # so its hash/duplicate/extra handling still applies.
    # Nothing is written in a dry run, so dest.exists() can never see two files
    # of this same run landing on one path -- which is the collision worth
    # reporting. Every destination taken so far is remembered here, mapped to the
    # source that took it, so the duplicate/extra handling below runs either way.
    claimed = {}       # destination file -> source file that claimed it
    claimed_dirs = set()

    def occupant(path):
        """What is already at `path`: the file on disk, or the source about to
        be written there by this run."""
        return path if path.exists() else claimed.get(path)

    unit_done = set()
    for parent in sorted(dir_dest, key=lambda p: len(p.parts)):
        target_dir, system = dir_dest[parent]
        if any(a in dir_dest for a in parent.parents) or target_dir.exists() \
                or target_dir in claimed_dirs:
            continue
        members = [p for p in files if parent in p.parents]
        sizes = {}
        for p in members:
            try:
                sizes[p] = p.stat().st_size
            except OSError:
                sizes[p] = 0

        status = "SIMULATED" if args.dry_run else "EXECUTED"
        if not args.dry_run:
            try:
                target_dir.parent.mkdir(parents=True, exist_ok=True)
                if args.mode == 'copy':
                    shutil.copytree(str(parent), str(target_dir))
                else:
                    # shutil.move, not Path.rename: rename fails across filesystems
                    shutil.move(str(parent), str(target_dir))
            except Exception as e:
                print(f"[WRITE_ERROR] Failed to {args.mode} folder {parent.name}: {e}")
                continue  # leave it to the per-file loop

        claimed_dirs.add(target_dir)
        # The tree moved whole, saves included -- one rename is the entire point
        # of this path. Put those back afterwards, so a folder unit leaves the
        # same files behind as the per-file loop would.
        strays = [p for p in members if in_saves(p)]
        if strays and not args.dry_run:
            for p in strays:
                try:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target_dir / p.relative_to(parent)), str(p))
                except OSError as e:
                    print(f"[WRITE_ERROR] Failed to leave {p.name} in place: {e}")
        members = [p for p in members if p not in set(strays)]
        for p in strays:
            summary["skipped"] += 1
            skipped_exts.add(p.suffix.lower() or '(no extension)')
            changelog.append({"source": str(p), "destination": "N/A", "action": "skip",
                              "status": "SKIP",
                              "reason": "a save, state or nvram file, not a dump"})
            unit_done.add(p)

        tag = f"[{args.mode.upper()}]"
        print(f"{tag} {parent.resolve()}{os.sep}  ({len(members)} files as one unit)")
        print(f"{'->':>{len(tag)}} {target_dir.resolve()}{os.sep}")
        row = by_system.setdefault(system, [0, 0])
        for p in members:
            summary["organized"] += 1
            bytes_by["organized"] += sizes[p]
            row[0] += 1
            row[1] += sizes[p]
            landed = target_dir / p.relative_to(parent)
            claimed[landed] = p
            changelog.append({"source": str(p), "destination": str(landed),
                              "action": args.mode, "status": status,
                              "reason": "Self-contained game folder moved as a unit"})
            unit_done.add(p)

    # Phase 3: the actual work. Bar is redrawn after each file; cleared before any
    # log line so the two never collide on the same terminal row.
    start = time.monotonic()
    for done, file_path in enumerate(files, 1):
        if file_path in unit_done:
            continue
        progress("Organizing", done - 1, len(files), start=start)
        system = resolved[file_path]
        folder_dest = dir_dest.get(file_path.parent)
        if folder_dest and system in ('unknown', 'ambiguous'):
            system = folder_dest[1]  # sidecar of a self-contained game folder
        is_bios = (system == 'bios' or is_bios_name(file_path.name)
                   or bios_root(file_path) is not None)
        if is_bios:
            folder_dest = None  # bios keeps its own root, game folder or not
        media_note = None
        if system == 'unknown' and not folder_dest and not in_saves(file_path):
            # Box art, snaps, wheels and gamelists already filed under
            # "roms/<system>/media/..." belong to the roms being moved out from
            # under them. Carried at the same relative path, or the library
            # moves and everything a frontend scraped for it stays behind.
            library = system_dir_root(file_path)
            if library and file_path.parent != library:
                system = system_dir(file_path)
                folder_dest = (dest_base / 'roms' / system
                               / file_path.parent.relative_to(library), system)
                media_note = "Scraped media carried with its library at the same relative path"
        # Stat before any move: after shutil.move the source path is gone.
        try:
            file_size = file_path.stat().st_size
        except OSError:
            file_size = 0

        if system in ['unknown', 'ambiguous']:
            # 'unknown' = extension isn't a ROM at all (.png, .cfg) -- not an error.
            # 'ambiguous' = a ROM container we failed to place, which is.
            if system == 'unknown':
                # Say which kind of "not a rom" it was: the changelog is the only
                # record of why a file was left behind.
                status, reason = "SKIP", ("a save, state or nvram file, not a dump"
                                          if in_saves(file_path)
                                          else "Not a recognized ROM extension")
            else:
                status, reason = "ERROR", "Shared container extension with no system hint in path"
            progress_clear()
            print(f"[{status}] {file_path.resolve()}")
            print(f"{'->':>7} {reason}")
            summary["skipped"] += 1
            skipped_exts.add(file_path.suffix.lower() or "(no extension)")
            changelog.append({"source": str(file_path), "destination": "N/A", "action": "skip", "status": status, "reason": reason})
            continue

        firmware_root = bios_root(file_path) if is_bios else None
        if folder_dest:
            target_dir = folder_dest[0]
            dest_path = target_dir / file_path.name
        elif firmware_root and file_path.parent != firmware_root:
            # keropi/, xmil/, vice/PET/: emulators look for their firmware at the
            # path the bios pack ships it at, so the layout is kept.
            target_dir = dest_base / 'bios' / file_path.parent.relative_to(firmware_root)
            dest_path = target_dir / file_path.name
        else:
            target_dir, dest_path = build_destination_path(dest_base, system, file_path.name, is_bios)
        default_reason = (media_note or "Self-contained game folder moved as a unit"
                          if folder_dest else "Sorted cleanly into matching system directory architecture")
        action_taken, log_reason = args.mode, (
            notes.get(file_path, default_reason) if not folder_dest else default_reason)

        taken = occupant(dest_path)
        organized = taken is None
        if taken is not None:
            # Hashing a multi-GB ISO over NFS is its own long task, so size and
            # mtime get first refusal and the hash only runs when they tie.
            identical = quick_compare(file_path, taken, args.trust_mtime)
            needs_dat = bool(dat_index) or args.dat_auto
            source_hash = None
            if identical is None or (identical is False and needs_dat):
                # byte-level bar rather than looking like a freeze.
                source_hash = calculate_hash(file_path, f"Hashing {file_path.name[:40]} (src)")
            if identical is None:
                dest_hash = calculate_hash(taken, f"Hashing {file_path.name[:40]} (dst)")
                identical = bool(source_hash and dest_hash and source_hash == dest_hash)
            if identical:
                target_dir = dest_base / "duplicates" / system
                dest_path = target_dir / file_path.name
                action_taken = "duplicate_isolate"
                log_reason = ("Cryptographically identical duplicate copy detected" if source_hash
                              else "Duplicate copy detected by matching archive contents (CRC32)"
                              if archive_crcs(file_path) is not None
                              else "Duplicate copy detected by matching size and mtime")
                summary["duplicate"] += 1
                bytes_by["duplicate"] += file_size
            else:
                canonical = dat_lookup(dat_index, file_path, source_hash)
                if not canonical and args.dat_auto:
                    if system not in auto_dats:
                        auto_dats[system] = fetch_dat(system)
                    canonical = dat_lookup(auto_dats[system], file_path, source_hash)
                if canonical:
                    # The DAT names the exact revision, so this was never a real
                    # collision -- only two files that shared a bad name.
                    target_dir, dest_path = build_destination_path(dest_base, system, canonical, is_bios)
                    action_taken, log_reason = args.mode, f"Name collision resolved by DAT match: {canonical}"
                    organized = True
                else:
                    target_dir = dest_base / "extra" / system
                    dest_path = target_dir / file_path.name
                    action_taken, log_reason = "extra_isolate", "Name collision identified; unique hash variant preserved safely"
                    summary["extra"] += 1
                    bytes_by["extra"] += file_size

        if organized:
            summary["organized"] += 1
            bytes_by["organized"] += file_size
            row = by_system.setdefault(system, [0, 0])
            row[0] += 1
            row[1] += file_size

        # Resolve the collision suffix before logging so the changelog records the
        # path actually written, not the one we first intended.
        counter = 1
        final_dest = dest_path
        while occupant(final_dest) is not None:
            final_dest = dest_path.with_name(f"{dest_path.stem}_{counter}{dest_path.suffix}")
            counter += 1
        claimed[final_dest] = file_path

        status = "SIMULATED" if args.dry_run else "EXECUTED"
        if not args.dry_run:
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                if args.mode == 'copy':
                    shutil.copy2(file_path, final_dest)
                else:
                    # shutil.move, not Path.rename: rename fails across filesystems
                    shutil.move(str(file_path), str(final_dest))
            except Exception as e:
                progress_clear()
                print(f"[WRITE_ERROR] Failed to {args.mode} {file_path.name}: {e}")
                status, log_reason = "WRITE_ERROR", str(e)
                summary["write_error"] += 1
                bucket = {"duplicate_isolate": "duplicate", "extra_isolate": "extra"}.get(action_taken, "organized")
                summary[bucket] -= 1
                bytes_by[bucket] -= file_size
                if bucket == "organized" and system in by_system:
                    by_system[system][0] -= 1
                    by_system[system][1] -= file_size

        if status != "WRITE_ERROR":
            progress_clear()
            # Two lines, source and destination starting in the same column, so the
            # paths can be diffed by eye. Both absolute: a relative dest against an
            # absolute source is exactly the comparison that hides a mistake.
            tag = f"[{action_taken.upper()}]"
            print(f"{tag} {file_path.resolve()}")
            print(f"{'->':>{len(tag)}} {final_dest.resolve()}")
        changelog.append({"source": str(file_path), "destination": str(final_dest), "action": action_taken, "status": status, "reason": log_reason})

    progress_clear()
    log_filename = str(dest_base / f"rom_organization_changelog.{args.changelog_format}")
    dest_base.mkdir(parents=True, exist_ok=True)
    if args.changelog_format == 'json':
        with open(log_filename, 'w', encoding='utf-8') as jf: json.dump(changelog, jf, indent=4)
    else:
        with open(log_filename, 'w', newline='', encoding='utf-8') as cf:
            writer = csv.DictWriter(cf, fieldnames=["source", "destination", "action", "status", "reason"])
            writer.writeheader(); writer.writerows(changelog)

    # ponytail: re-filter the changelog instead of keeping a parallel error list.
    errors = [e for e in changelog if e["status"] in ("ERROR", "WRITE_ERROR")]
    if errors:
        print(f"\n--- Errors ({len(errors)}) ---")
        for e in errors:
            print(f"  [{e['status']}] {e['source']}")
            print(f"      {e['reason']}")

    print(f"\n--- Summary ({args.mode}{', dry-run' if args.dry_run else ''}) ---")
    for key, count in summary.items():
        size = f"  ({_size(bytes_by[key])})" if key in bytes_by else ""
        print(f"  {key}: {count}{size}")
        if key == "skipped" and skipped_exts:
            print(f"    extensions: {', '.join(sorted(skipped_exts))}")
    verb = "copied" if args.mode == 'copy' else "moved"
    # Only the organized bucket lands in the library proper; duplicates and extras
    # get isolated, so folding them into one number would overstate what you gain.
    print(f"  total into library: {_size(bytes_by['organized'])}")
    print(f"  total {verb}: {_size(sum(bytes_by.values()))}")
    print(f"  changelog: {log_filename}")

    if by_system:
        print("\n--- Into library, by system ---")
        width = max(len(s) for s in by_system)
        total = bytes_by['organized']
        # Biggest first: the point of this table is spotting what eats the disk.
        for system, (count, size) in sorted(by_system.items(), key=lambda kv: -kv[1][1]):
            share = f"{size / total:5.1%}" if total else "    -"
            print(f"  {system:<{width}}  {_size(size):>9}  {share}  ({count} file{'' if count == 1 else 's'})")

    if summary["skipped"] or summary["write_error"]:
        sys.exit(1)


if __name__ == "__main__":
    main()


