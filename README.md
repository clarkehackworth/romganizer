# romganizer

Sorts a messy ROM collection into `dest/roms/{system}/...` and `dest/bios/{system}/...`,
routing by header bytes, file extension, path hints, and — for archives and disc
images — what's inside them. Self-contained game folders move as a unit; saves,
split archives and non-ROM files are left where they are.

## Usage

```
python3 romganizer.py <source>... <dest> [--mode move|copy] [--dry-run] [--trust-mtime]
                      [--changelog-format json|csv] [--dat PATH ...] [--dat-auto]
```

- `source` — one or more folders, files, or globs to scan (recursively) for
  ROMs/BIOS files. A glob is expanded by the script too, so a quoted
  `'dumps/*/roms'` works even where the shell wouldn't expand it. The same file
  reached twice is only handled once.
- `dest` — base folder to organize into.
- `--mode move` (default) moves files out of `source`; `--mode copy` copies them,
  leaving `source` untouched.
- `--dry-run` — print what would happen without touching any files.
- `--trust-mtime` — treat a same-size, same-mtime collision as a duplicate without
  hashing. Fast, but a bulk copy stamps a whole collection with one mtime, so two
  different revisions of equal length would be filed as duplicates. Off by default.
- `--dat` — DAT file(s), a glob, or a directory of them. Logiqx XML or clrmamepro
  text, zipped or not.
- `--dat-auto` — download the matching DAT on demand instead, cached in
  `~/.cache/romganizer/dats` and refreshed when older than 30 days.
- `--changelog-format` — write `rom_organization_changelog.json` (default) or `.csv`
  **into `dest`**, listing every action taken and the path actually written.

Duplicate files (identical content to an existing file at the destination) are
moved/copied into `dest/duplicates/{system}/`; same-name-but-different-content files
go into `dest/extra/{system}/`. A run prints a summary — counts and bytes per system —
and exits `1` if anything was skipped or failed to write.

### What a collision costs

Only a same-name collision is ever compared, and the comparison stops at the first
thing that answers it:

1. **Size.** Different lengths is proof they differ, and it's free.
2. **Stored CRC32.** For two archives of the same type, `.zip` and `.7z` both record a
   CRC32 per member in a header, so two 1.8 GB dumps are settled on a few KB of
   metadata instead of 3.6 GB of decompression. On a real collection this was 98.7% of
   the bytes the script was still reading after the size check.
3. **MD5**, with a byte-level progress bar, for whatever the first two can't decide.

`--trust-mtime` inserts an mtime check after the size check, with the caveat above.

## Identifying collisions with DAT files

With `--dat` or `--dat-auto`, a same-name-different-content file is looked up by MD5
before it is quarantined. A hit means the collision was only a bad filename, so the
file is filed normally under its canonical name — `Zelda.nes` becomes
`Legend of Zelda, The (USA) (Rev A).nes`. A miss still goes to `dest/extra/{system}/`.

Only collisions are looked up, and `--dat-auto` only downloads the system it needs, so
a clean run costs nothing. Cartridge sets come from No-Intro (via the libretro mirror),
disc sets from redump.org.

Dumps carrying a header that DATs exclude from their hashes are re-hashed without it on
a miss, so they still match:

| Format | Header | Detected by |
|---|---|---|
| iNES, FDS | 16 bytes | `NES\x1a` / `FDS\x1a` magic |
| Atari 7800 | 128 bytes | `ATARI7800` at offset 1 |
| SNES copier (`.smc`, `.fig`, `.swc`) | 512 bytes | no magic — size is 512 past a whole KB |
| Genesis SMD (`.smd`) | 512 bytes + interleaved body | `AA BB` at offset 8 |

An SMD body also stores each 16 KB block's odd bytes before its even ones, so it is
deinterleaved before hashing. The headerless size rule is only trusted on cart
extensions, since a disc image can land on it by coincidence.

## How a file is classified

Every signal votes and the highest total wins, so conflicting evidence is resolved
instead of decided by whichever check ran first:

| Signal | Weight |
|---|---|
| **header magic bytes** | 4 |
| extension unique to one system (`.nes`, `.sfc`) | 3 |
| zip contents / BIOS filename | decisive |
| directory or filename naming a system, nearest first | 2 (1 for a distant ancestor) |
| extension shared by several systems (`.bin`, `.iso`, `.cue`, `.chd`, `.xex`) | 1 |

So `.../PSX/game.bin` is psx (shared extension defers to the folder), `.../Wii U/g.iso`
is wiiu, and `.../SNES/Mario.nes` is **nes** — a unique extension outranks a folder
name, since folders lie. That last case is a conflict: it's printed as `[CONFLICT]`
during the run and recorded as the reason in the changelog, so you can review what
the extension overruled.

A shared extension with nothing backing it scores 1, too weak to file: it's reported
as `ambiguous` and left alone rather than misfiled. Put it in a folder named after
the platform and re-run.

## Content sniffing

The first 64KB of every file is read and matched against a table of header magics
(NES/UNIF, FDS headered and headerless, N64 in all four byte orders, GB/GBC/GBA/
NDS/3DS/Switch, GameCube, Wii, Mega Drive, 32X, Master System, Game Gear, Sega CD,
Saturn, Dreamcast, Lynx, 7800, Neo Geo Pocket/Color, Xbox and Xbox 360). Bytes
can't lie, so a match outranks both the extension and the folder — a `.rom`
holding an NES header is filed as nes, and the mislabel is logged as a
`[CONFLICT]`. Signatures are taken from `file(1)`'s `magic/Magdir/console`,
plutiedev.com/rom-header and smspower.org/Development/ROMHeader.

Three headers need a second byte to finish the job: GB vs GBC (CGB flag at 0x143),
Neo Geo Pocket vs Color (0x23), and Master System vs Game Gear (region nibble in
the last byte of the `TMR SEGA` header, which itself floats between 0x1FF0, 0x3FF0
and 0x7FF0).

Disc images go further: if the file is ISO9660 (both plain 2048-byte images and
raw 2352-byte `.bin` dumps), the root directory is parsed and `SYSTEM.CNF` read —
`BOOT=` means psx, `BOOT2=` means ps2, and `PSP_GAME`/`UMD_DATA.BIN` means psp.
That's the definitive answer for the `.bin`/`.iso` pile, no folder hint needed. A
`.cue` is resolved through the track file it names.

The Xbox media marker doesn't distinguish xbox from xbox360, so both vote and the
folder breaks the tie (otherwise `ambiguous`).

### Compressed formats

| Format | How it's read | Needs |
|---|---|---|
| `.zip` | member names | — |
| `.cso` / `.ciso` | blocks inflated on demand, so the ISO9660 probe works in full | — |
| `.7z` | first member piped through the magic table, member names as fallback | `7z` |
| `.rar` | same | `7z` or `unrar` |
| `.chd` | unpacked to a temp copy | `chdman` |
| `.rvz` | unpacked to a temp copy | `dolphin-tool` |

The first four are cheap and always run. The last two are not — they need temp
space the size of the game — so they only run for a file that nothing cheaper
could place, and only if the tool is installed. A `.chd` sitting in a folder named
after its system is identified from the folder and never unpacked. Missing tools
are not an error; those files just fall back to extension and folder evidence.

An extension the script doesn't recognize at all isn't skipped — the path is then the
only evidence there is, so any hint in it wins (`.../Sega CD/Sonic.xyz` → segacd, even
from a subfolder). These are printed as `[INFERRED]` and recorded in the changelog,
since nothing corroborated them. Artwork, readmes, dat files and other non-ROM
extensions are excluded from this so they don't inherit their folder's system.

Sidecar files inherit the system their folder resolved to, so a `.cue` and its
`.bin` tracks stay together. For disc-based systems each game gets its own folder,
with `(Disc N)` and `(Track NN)` labels stripped from the folder name.

## Folders that travel as one unit

A self-contained game folder is moved as one operation rather than file by file — on
the same filesystem that's a single rename, and in move mode it leaves no empty
source folder behind. A folder qualifies when it holds dumps that all resolve to one
system and whose names agree (a title plus its DLC and unlock packages counts as one
game; two unrelated titles side by side does not). The folder keeps its own name,
since the directory names the game and the files inside often don't — six
preconfigured Tomb Raider folders all ship `OpenLara.exe`, and every repack ships
`Setup.exe`.

Certain files mark the folder above them as a disc rip outright, and that answer
outranks everything else in it:

| Marker | System |
|---|---|
| `SYSTEM.CNF` | psx / ps2 / psp, by its contents |
| `PS3_DISC.SFB`, `PARAM.SFO` inside `PS3_GAME` | ps3 |
| `default.xbe` | xbox |
| `default.xex` | xbox360 |

A `PS3_GAME`/`PS3_UPDATE`/`USRDIR` marker sits inside the rip, so the rip's own root
is what moves. A release folder wrapping a marked rip keeps its name too, bringing
the `.rar` set and `.nfo` beside it along.

Directories holding no dump at all — a PC port's data folder, an emulator's config
directory — still travel whole, claimed by the outermost folder that names only that
game. Their names are generic (`APPS/LAUNCHDISC/title.cfg`), so flattening them would
collide.

## What is deliberately left behind

- **Saves.** Battery saves, save states, nvram, MAME high scores and shader caches
  (`.sav`, `.srm`, `.state`*N*, `.nv`, `.hi`, `.glshadercache`, …, or anything under a
  `saves/`/`savestates/` directory) are not dumps. They stay where they are, including
  when a folder unit moves out from over them.
- **Split archive volumes.** `Collection.zip.001`, `set.7z.02`: no single volume is a
  dump and none opens without the others, so the set is untouched rather than filed as
  that many games. Narrow on purpose — `.rNN` rides along inside a game folder,
  `SLUS_001.52` is a PS1 executable, and `.z80` is a Spectrum snapshot.
- **Artwork, readmes and DAT files**, which are skipped rather than inheriting their
  folder's system.

Two things do move, at the same relative path they were found at:

- **Scraped media.** Box art, snaps, wheels and gamelists already under
  `roms/<system>/media/...` belong to the ROMs being moved out from under them, so
  they are carried across intact.
- **BIOS packs.** Firmware under a `bios/` or `system/` directory keeps the layout
  below it (`system/vice/PET/chargen`, `keropi/iplrom.dat`), because that's the path
  the emulator looks for it at. Firmware carries every extension there is, so the
  directory is the signal, not the extension.

## Companion scripts

Two standalone scripts for the other half of the job — finding copies that the
organizer can't see, because they don't share a name:

```
python3 find_dups.py <candidates.pkl> <dups.pkl>    # identical bytes, different names
python3 plan_dedup.py <dups.pkl> <delete.sh>        # turn that into a reviewable script
```

`find_dups.py` takes a pickled `{size: [paths]}` mapping of same-size candidates and
narrows it in three passes — size, first 4 MB, full MD5 — so the full read only
happens for the handful that really are copies. `plan_dedup.py` never deletes: it
emits a shell script you review, keeping the copy with the most readable name
(`Lara Croft Tomb Raider - Anniversary (USA).iso` over `SLUS-21555 (1.02).iso`) and
holding back four categories where the copy is the point — CD tracks named inside a
`.cue`, MAME parent/clone CHDs, firmware an emulator finds by exact path, and
anything a sibling `.cue`/`.gdi`/`.m3u` names by hand.

## Tests

```
python3 test_romganizer.py
python3 test_progress.py
```

## Requirements

Python 3.7+, standard library only — see `requirements.txt`.
