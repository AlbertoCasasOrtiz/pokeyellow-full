#!/usr/bin/env python3
"""Render Pokémon Yellow music and sound effects through the original Game Boy audio engine.

Examples:
    python tools/music_renderer/render.py Music_Celadon
    python tools/music_renderer/render.py Music_Celadon --loops 3 --fade-out 4
    python tools/music_renderer/render.py --all --loops 2 --fade-out 5 --format flac
    python tools/music_renderer/render.py --list
    python tools/music_renderer/render.py --sfx SFX_Level_Up
    python tools/music_renderer/render.py --all-sfx
    python tools/music_renderer/render.py --list-sfx

Dependencies:
    python -m pip install numpy pyboy soundfile
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
INIT_ASM = ROOT / "home" / "init.asm"
AUDIO_ASM = ROOT / "audio.asm"
ENGINE_ASM = ROOT / "audio" / "engine_1.asm"
WRAM_ASM = ROOT / "ram" / "wram.asm"
CONSTANTS = ROOT / "constants" / "music_constants.asm"

ROM = ROOT / "pokeyellow_debug.gbc"
SYM = ROOT / "pokeyellow_debug.sym"
MAP = ROOT / "pokeyellow_debug.map"

READY_MARKER = 0xA5
REQUEST_MARKER = 0x01
SAMPLE_RATE = 48_000
FPS = 59.7275

# The four SFX/music engines are in these ROM banks.
ENGINE_BANKS = {1: 0x02, 2: 0x08, 3: 0x1F, 4: 0x20}


RENDERER_SECTION = r"""
SECTION "Music Renderer", ROMX, BANK[2]

MusicRendererInit::
    ld a, $a5
    ld [wUnusedMusicByte], a
.musicRendererWait
    ld a, [wUnusedMusicByte]
    cp $a5
    jr z, .musicRendererWait

    ld hl, wUnusedAudioCounter
    ld a, [hli]
    ld c, [hl]
    call PlayMusic

.musicRendererLoop
    halt
    jr .musicRendererLoop

; Entry: A = low byte and B = high byte of the sound_loop 0 destination,
;        C = audio channel index (zero-based).
; The caller preserves AF, BC, DE and HL around this routine.
MusicRendererLoopHook::
    ; Find this channel's configured .mainloop address.
    ld d, 0
    ld e, c
    sla e
    rl d
    ld hl, wRendererLoopTargets
    add hl, de
    cp [hl]
    jr nz, .rendererLoopDone
    inc hl
    ld a, b
    cp [hl]
    jr nz, .rendererLoopDone

    ; Four independent saturating 4-bit counters, two per byte.
    ld a, c
    srl a
    ld e, a
    ld d, 0
    ld hl, wRendererLoopCounts
    add hl, de
    ld a, [hl]
    ld b, a

    ld a, c
    and 1
    jr nz, .rendererHighNibble

    ; Even channel: low nibble.
    ld a, b
    and $0f
    cp $0f
    jr z, .rendererLoopDone
    inc a
    ld c, a
    ld a, b
    and $f0
    ld d, a
    ld a, c
    and $0f
    or d
    ld [hl], a
    jr .rendererLoopDone

.rendererHighNibble
    ; Odd channel: high nibble.
    ld a, b
    swap a
    and $0f
    cp $0f
    jr z, .rendererLoopDone
    inc a
    ld c, a
    swap a
    and $f0
    ld d, a
    ld a, b
    and $0f
    or d
    ld [hl], a

.rendererLoopDone
    ret
"""


def parse_music_constants() -> dict[str, str]:
    if not CONSTANTS.is_file():
        raise FileNotFoundError(f"Cannot find {CONSTANTS}")
    source = CONSTANTS.read_text(encoding="utf-8")
    return {
        label: constant
        for constant, label in re.findall(
            r"music_const\s+(MUSIC_[A-Z0-9_]+)\s*,\s*(Music_[A-Za-z0-9_]+)",
            source,
        )
    }


def parse_symbols(path: Path) -> dict[str, tuple[int, int]]:
    symbols: dict[str, tuple[int, int]] = {}
    pattern = re.compile(r"^\s*([0-9a-fA-F]{2}):([0-9a-fA-F]{4})\s+(\S+)")
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.match(line)
        if match:
            symbols[match.group(3)] = (
                int(match.group(1), 16),
                int(match.group(2), 16),
            )
    return symbols


def resolve_song(name: str, songs: dict[str, str]) -> str:
    aliases = {label.lower(): label for label in songs}
    aliases.update({constant.lower(): label for label, constant in songs.items()})
    key = name.strip().lower()
    if key not in aliases:
        raise ValueError(
            f"Unknown song '{name}'. Use --list to see available songs."
        )
    return aliases[key]


def parse_song_channels(song_label: str) -> dict[int, str]:
    """Return {zero_based_engine_channel: channel_label} from music headers."""
    header_files = sorted((ROOT / "audio" / "headers").glob("musicheaders*.asm"))
    label_re = re.compile(r"^\s*(Music_[A-Za-z0-9_]+)::\s*$")
    channel_re = re.compile(
        r"^\s*channel\s+([1-4]),\s*(Music_[A-Za-z0-9_]+)\s*$"
    )

    for header_file in header_files:
        lines = header_file.read_text(encoding="utf-8").splitlines()
        in_song = False
        channels: dict[int, str] = {}
        for line in lines:
            label_match = label_re.match(line)
            if label_match:
                if in_song:
                    break
                in_song = label_match.group(1) == song_label
                continue
            if in_song:
                match = channel_re.match(line)
                if match:
                    channels[int(match.group(1)) - 1] = match.group(2)
        if in_song:
            if not channels:
                raise RuntimeError(f"No channel declarations found for {song_label}.")
            return channels
    raise RuntimeError(f"Could not find the header for {song_label}.")


def parse_sfx_headers() -> dict[str, tuple[int, dict[int, str]]]:
    """Return {SFX label: (engine number, {zero-based channel: channel label})}."""
    effects: dict[str, tuple[int, dict[int, str]]] = {}
    header_dir = ROOT / "audio" / "headers"
    label_re = re.compile(r"^\s*(SFX_[A-Za-z0-9_]+)::\s*$")
    channel_re = re.compile(r"^\s*channel\s+([1-8]),\s*([A-Za-z0-9_.]+)")
    for engine in range(1, 5):
        path = header_dir / f"sfxheaders{engine}.asm"
        if not path.is_file():
            continue
        current: str | None = None
        channels: dict[int, str] = {}

        def save() -> None:
            if current and channels:
                effects[current] = (engine, dict(channels))

        for line in path.read_text(encoding="utf-8").splitlines():
            label_match = label_re.match(line)
            if label_match:
                save()
                current = label_match.group(1)
                channels = {}
                continue
            if current:
                channel_match = channel_re.match(line)
                if channel_match:
                    channel_number = int(channel_match.group(1))
                    channels[channel_number - 1] = channel_match.group(2)
        save()
    if not effects:
        raise RuntimeError("No SFX headers found in audio/headers/sfxheaders*.asm.")
    return effects


def resolve_sfx(name: str, effects: dict[str, tuple[int, dict[int, str]]]) -> str:
    aliases = {label.lower(): label for label in effects}
    key = name.strip().lower()
    if key not in aliases:
        raise ValueError(f"Unknown sound effect '{name}'. Use --list-sfx.")
    return aliases[key]


def patch_engine_source(source: str) -> str:
    """Split sound_loop 0 from finite loops and instrument only the former."""
    pattern = re.compile(r"(?ms)^Audio1_sound_loop:\n.*?(?=^Audio1_note_type:)")
    replacement = """Audio1_sound_loop:
    cp sound_loop_cmd
    jp nz, Audio1_note_type
    call Audio1_GetNextMusicByte
    ld e, a
    and a
    jr z, .infiniteLoop

    ld b, 0
    ld hl, wChannelLoopCounters
    add hl, bc
    ld a, [hl]
    cp e
    jr nz, .loopAgain
    ld a, $1
    ld [hl], a
    call Audio1_GetNextMusicByte
    call Audio1_GetNextMusicByte
    jp Audio1_sound_ret

.loopAgain
    inc a
    ld [hl], a
    ; Finite sound_loop: update the command pointer without counting it.
    call Audio1_GetNextMusicByte
    push af
    call Audio1_GetNextMusicByte
    ld b, a
    ld d, $0
    ld a, c
    add a
    ld e, a
    ld hl, wChannelCommandPointers
    add hl, de
    pop af
    ld [hli], a
    ld [hl], b
    jp Audio1_sound_ret

.infiniteLoop
    ; Only sound_loop 0 reaches this path.
    call Audio1_GetNextMusicByte
    push af
    call Audio1_GetNextMusicByte
    ld b, a
    ld d, $0
    ld a, c
    add a
    ld e, a
    ld hl, wChannelCommandPointers
    add hl, de
    pop af
    ld [hli], a
    ld [hl], b

    push af
    push bc
    push de
    push hl
    call MusicRendererLoopHook
    pop hl
    pop de
    pop bc
    pop af
    jp Audio1_sound_ret

"""
    patched, count = pattern.subn(replacement, source)
    if count != 1:
        raise RuntimeError(
            "Could not locate exactly one Audio1_sound_loop routine in "
            "audio/engine_1.asm; refusing to patch an unexpected source layout."
        )
    return patched


def patch_wram_source(source: str) -> str:
    """Reuse the original 11-byte reservation without changing WRAM size."""
    pattern = re.compile(r"(wUnusedAudioCounter::\s+dw\s*\n)([ \t]*ds\s+11\b)")
    replacement = (
        r"\1"
        + "; Temporary renderer state reuses the original 11-byte reservation.\n"
        + "wRendererLoopTargets:: ds 8\n"
        + "wRendererLoopCounts:: ds 2\n"
        + "wRendererLoopSpare:: db"
    )
    patched, count = pattern.subn(replacement, source, count=1)
    if count != 1:
        raise RuntimeError(
            "Could not find the 11-byte reservation after wUnusedAudioCounter."
        )
    return patched


def patch_sources() -> dict[Path, bytes]:
    paths = (INIT_ASM, AUDIO_ASM, ENGINE_ASM, WRAM_ASM)
    originals = {path: path.read_bytes() for path in paths}
    init_source = originals[INIT_ASM].decode("utf-8")
    audio_source = originals[AUDIO_ASM].decode("utf-8")
    engine_source = originals[ENGINE_ASM].decode("utf-8")
    wram_source = originals[WRAM_ASM].decode("utf-8")

    intro_line = re.compile(r"(?m)^[ \t]*predef PlayIntro[ \t]*$")
    title_line = re.compile(r"(?m)^[ \t]*jp PrepareTitleScreen[ \t]*$")
    if len(intro_line.findall(init_source)) != 1:
        raise RuntimeError("Expected exactly one 'predef PlayIntro' in home/init.asm.")
    if len(title_line.findall(init_source)) != 1:
        raise RuntimeError("Expected exactly one 'jp PrepareTitleScreen' in home/init.asm.")

    init_source = intro_line.sub("    ; skipped by the temporary renderer", init_source)
    init_source = title_line.sub(
        '    ld a, BANK("Audio Engine 1")\n'
        "    call BankswitchCommon\n"
        "    jp MusicRendererInit",
        init_source,
    )
    if 'SECTION "Music Renderer"' in audio_source:
        raise RuntimeError('audio.asm already contains SECTION "Music Renderer".')

    audio_source = audio_source.rstrip() + "\n\n" + RENDERER_SECTION.lstrip()
    engine_source = patch_engine_source(engine_source)
    wram_source = patch_wram_source(wram_source)

    # Write only after all patches have been validated.
    INIT_ASM.write_text(init_source, encoding="utf-8", newline="\n")
    AUDIO_ASM.write_text(audio_source, encoding="utf-8", newline="\n")
    ENGINE_ASM.write_text(engine_source, encoding="utf-8", newline="\n")
    WRAM_ASM.write_text(wram_source, encoding="utf-8", newline="\n")
    return originals


def build_renderer_rom(temp_dir: Path) -> tuple[Path, Path]:
    tracked = [
        INIT_ASM, AUDIO_ASM, ENGINE_ASM, WRAM_ASM,
        ROOT / "home_debug.o", ROOT / "audio_debug.o", ROOT / "ram_debug.o",
        ROOT / "home.o", ROOT / "audio.o", ROOT / "ram.o",
        ROM, SYM, MAP,
    ]
    backups: dict[Path, bytes | None] = {
        path: path.read_bytes() if path.exists() else None for path in tracked
    }
    try:
        patch_sources()
        for obj in tracked[4:10]:
            if obj.exists():
                obj.unlink()

        print("Building temporary audio-renderer ROM...", flush=True)
        subprocess.run(["make", "yellow_debug", "DEBUG=1"], cwd=ROOT, check=True)
        if not ROM.is_file() or not SYM.is_file():
            raise RuntimeError("Build finished but ROM or .sym file is missing.")

        temp_rom = temp_dir / "pokeyellow_audio_renderer.gbc"
        temp_sym = temp_dir / "pokeyellow_audio_renderer.sym"
        shutil.copy2(ROM, temp_rom)
        shutil.copy2(SYM, temp_sym)
        return temp_rom, temp_sym
    finally:
        for path, content in backups.items():
            if content is None:
                if path.exists():
                    path.unlink()
            else:
                path.write_bytes(content)
        for source in (INIT_ASM, AUDIO_ASM, ENGINE_ASM, WRAM_ASM):
            os.utime(source, None)


def convert_pcm16(samples: np.ndarray) -> np.ndarray:
    samples = np.asarray(samples)
    if samples.dtype == np.int8:
        return samples.astype(np.int16) << 8
    if samples.dtype == np.int16:
        return samples
    if np.issubdtype(samples.dtype, np.floating):
        return np.round(np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    raise RuntimeError(f"Unsupported PyBoy audio dtype: {samples.dtype}")


def write_wav(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    pcm16 = np.asarray(convert_pcm16(samples), dtype="<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(pcm16.tobytes())


def write_flac(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    try:
        import soundfile as sf
    except ImportError as exc:
        raise RuntimeError(
            "FLAC export requires soundfile. Install it with: "
            "python -m pip install soundfile"
        ) from exc
    sf.write(
        str(path), convert_pcm16(samples), sample_rate,
        format="FLAC", subtype="PCM_16",
    )


def save_audio(
    output_path: Path, samples: np.ndarray, sample_rate: int, output_format: str
) -> None:
    if samples.ndim != 2 or samples.shape[1] != 2:
        raise RuntimeError(f"Expected stereo audio, received shape {samples.shape}.")
    peak = int(np.max(np.abs(samples.astype(np.int32))))
    if peak == 0:
        raise RuntimeError("The captured audio is completely silent.")
    if output_format == "flac":
        write_flac(output_path, samples, sample_rate)
    else:
        write_wav(output_path, samples, sample_rate)
    print(
        f"  Saved {output_path} ({len(samples) / sample_rate:.2f}s, peak={peak})",
        flush=True,
    )


def apply_fade_out(
    samples: np.ndarray, sample_rate: int, fade_seconds: float
) -> np.ndarray:
    result = np.asarray(samples).copy()
    if fade_seconds > 0 and len(result) > 0:
        fade_frames = min(len(result), round(fade_seconds * sample_rate))
        if fade_frames > 0:
            start = len(result) - fade_frames
            gain = np.linspace(1.0, 0.0, fade_frames, endpoint=True, dtype=np.float64)
            faded = result[start:].astype(np.float64) * gain[:, None]
            if np.issubdtype(result.dtype, np.integer):
                limits = np.iinfo(result.dtype)
                result[start:] = np.clip(
                    np.round(faded), limits.min, limits.max
                ).astype(result.dtype)
            else:
                result[start:] = faded.astype(result.dtype)
        silence = np.zeros(
            (round(0.5 * sample_rate), result.shape[1]), dtype=result.dtype
        )
        result = np.concatenate((result, silence), axis=0)
    return result


def new_pyboy(rom_path: Path):
    try:
        from pyboy import PyBoy
    except ImportError as exc:
        raise RuntimeError(
            "PyBoy is required. Install it with: python -m pip install pyboy soundfile"
        ) from exc
    pyboy = PyBoy(
        str(rom_path), window="null", sound_emulated=True,
        sound_sample_rate=SAMPLE_RATE, sound_volume=100,
    )
    pyboy.set_emulation_speed(0)
    return pyboy


def wait_for_renderer(pyboy, ready_address: int) -> None:
    for _ in range(round(FPS * 20)):
        if not pyboy.tick(1, False, True):
            raise RuntimeError("The emulator stopped during initialization.")
        if pyboy.memory[ready_address] == READY_MARKER:
            return
    raise RuntimeError("Renderer ROM did not reach its ready loop within 20 seconds.")


def request_sound(pyboy, symbols: dict[str, tuple[int, int]], sound_id: int, bank: int):
    ready_address = symbols["wUnusedMusicByte"][1]
    request_address = symbols["wUnusedAudioCounter"][1]
    wait_for_renderer(pyboy, ready_address)
    pyboy.memory[request_address] = sound_id
    pyboy.memory[request_address + 1] = bank
    pyboy.memory[ready_address] = REQUEST_MARKER


def capture_song(
    rom_path: Path, symbols_path: Path, song_label: str, output_dir: Path,
    loops: int, fade_out: float, max_seconds: float, output_format: str,
) -> Path:
    symbols = parse_symbols(symbols_path)
    required = (
        "SFX_Headers_1", "wUnusedMusicByte", "wUnusedAudioCounter",
        "wRendererLoopTargets", "wRendererLoopCounts", "wChannelSoundIDs",
        "MusicRendererInit",
    )
    missing = [name for name in required if name not in symbols]
    if missing:
        raise RuntimeError(f"Missing required symbols: {', '.join(missing)}")
    if song_label not in symbols:
        raise RuntimeError(f"{song_label} is missing from the generated .sym file.")

    _, header_base = symbols["SFX_Headers_1"]
    bank, address = symbols[song_label]
    offset = address - header_base
    if offset < 0 or offset % 3:
        raise RuntimeError(
            f"Cannot calculate music ID for {song_label}: "
            f"header=${address:04X}, base=${header_base:04X}."
        )
    sound_id = offset // 3
    if not 0 <= sound_id < 0xFE:
        raise RuntimeError(f"Invalid music ID ${sound_id:02X} for {song_label}.")

    channels = parse_song_channels(song_label)
    mainloop_targets: dict[int, int] = {}
    for channel_index, channel_label in channels.items():
        marker = f"{channel_label}.mainloop"
        if marker in symbols:
            mainloop_targets[channel_index] = symbols[marker][1]
            continue
        candidates = [
            (name, sym_address)
            for name, (sym_bank, sym_address) in symbols.items()
            if sym_bank == bank and name.startswith(f"{channel_label}.")
            and name.rsplit(".", 1)[-1] == "loop"
        ]
        if len(candidates) == 1:
            mainloop_targets[channel_index] = candidates[0][1]
        elif len(candidates) > 1:
            raise RuntimeError(
                f"Multiple infinite-loop labels for {channel_label}: "
                + ", ".join(name for name, _ in candidates)
            )

    one_shot = not mainloop_targets
    effective_loops = 1 if one_shot else loops
    effective_fade = 0.0 if one_shot else fade_out
    if one_shot:
        print("  No infinite-loop targets: rendering once; loops/fade ignored.", flush=True)

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{song_label}.{output_format}"
    print(
        f"Rendering {song_label} (ID=${sound_id:02X}, bank=${bank:02X}): "
        + ("one-shot playback" if one_shot else
           f"{effective_loops} main-loop cycle(s), fade={effective_fade:g}s"),
        flush=True,
    )

    pyboy = new_pyboy(rom_path)
    try:
        ready_address = symbols["wUnusedMusicByte"][1]
        request_address = symbols["wUnusedAudioCounter"][1]
        target_base = symbols["wRendererLoopTargets"][1]
        count_base = symbols["wRendererLoopCounts"][1]
        sound_ids_base = symbols["wChannelSoundIDs"][1]
        wait_for_renderer(pyboy, ready_address)

        for index in range(4):
            pyboy.memory[target_base + index * 2] = 0
            pyboy.memory[target_base + index * 2 + 1] = 0
        pyboy.memory[count_base] = 0
        pyboy.memory[count_base + 1] = 0
        for channel_index, target in mainloop_targets.items():
            if not 0 <= channel_index < 4:
                raise RuntimeError(f"Unexpected music channel index {channel_index}.")
            pyboy.memory[target_base + channel_index * 2] = target & 0xFF
            pyboy.memory[target_base + channel_index * 2 + 1] = target >> 8

        pyboy.memory[request_address] = sound_id
        pyboy.memory[request_address + 1] = bank
        pyboy.memory[ready_address] = REQUEST_MARKER

        chunks: list[np.ndarray] = []
        started = False
        finished = False
        max_frames = max(1, round(max_seconds * FPS))
        for frame in range(max_frames):
            if not pyboy.tick(1, False, True):
                raise RuntimeError("The emulator stopped unexpectedly.")
            audio = np.asarray(pyboy.sound.ndarray)
            if audio.size:
                chunks.append(audio.copy())

            if one_shot:
                active = [
                    pyboy.memory[sound_ids_base + index] for index in channels
                ]
                if any(value != 0 for value in active):
                    started = True
                elif started:
                    finished = True
            else:
                finished = all(
                    (
                        pyboy.memory[count_base + index // 2]
                        >> (4 if index % 2 else 0)
                    ) & 0x0F >= effective_loops
                    for index in mainloop_targets
                )

            if finished:
                break
            if (frame + 1) % round(FPS * 10) == 0:
                elapsed = (frame + 1) / FPS
                print(f"  {elapsed:.0f}/{max_seconds:g}s ...", flush=True)

        if not finished:
            raise RuntimeError(
                f"{song_label} did not complete within {max_seconds:g}s. "
                "Increase --seconds if necessary."
            )
        if not chunks:
            raise RuntimeError("PyBoy produced no audio samples.")
        samples = np.concatenate(chunks, axis=0)
        sample_rate = pyboy.sound.sample_rate
        samples = apply_fade_out(samples, sample_rate, effective_fade)
        save_audio(output_path, samples, sample_rate, output_format)
        return output_path
    finally:
        pyboy.stop()


def capture_sfx(
    rom_path: Path, symbols_path: Path, sfx_label: str, engine: int,
    channels: dict[int, str], output_dir: Path, max_seconds: float,
    output_format: str,
) -> Path:
    symbols = parse_symbols(symbols_path)
    required = (
        "SFX_Headers_1", "SFX_Headers_2", "SFX_Headers_3", "SFX_Headers_4",
        "wUnusedMusicByte", "wUnusedAudioCounter", "wChannelSoundIDs",
        "MusicRendererInit",
    )
    missing = [name for name in required if name not in symbols]
    if missing:
        raise RuntimeError(f"Missing required symbols: {', '.join(missing)}")
    if sfx_label not in symbols:
        raise RuntimeError(f"{sfx_label} is missing from the generated .sym file.")

    bank, address = symbols[sfx_label]
    expected_bank = ENGINE_BANKS[engine]
    if bank != expected_bank:
        raise RuntimeError(
            f"{sfx_label} is in bank ${bank:02X}, but its header file "
            f"indicates engine {engine} (bank ${expected_bank:02X})."
        )
    header_base = symbols[f"SFX_Headers_{engine}"][1]
    offset = address - header_base
    if offset < 0 or offset % 3:
        raise RuntimeError(
            f"Cannot calculate SFX ID for {sfx_label}: "
            f"header=${address:04X}, base=${header_base:04X}."
        )
    sound_id = offset // 3
    if not 0 <= sound_id < 0xFE:
        raise RuntimeError(f"Invalid SFX ID ${sound_id:02X} for {sfx_label}.")
    if not channels or any(index not in range(8) for index in channels):
        raise RuntimeError(f"Invalid SFX channel declarations for {sfx_label}.")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{sfx_label}.{output_format}"
    channel_names = ", ".join(f"CH{i + 1}" for i in sorted(channels))
    print(
        f"Rendering {sfx_label} (ID=${sound_id:02X}, bank=${bank:02X}, "
        f"channels={channel_names})",
        flush=True,
    )

    pyboy = new_pyboy(rom_path)
    try:
        request_sound(pyboy, symbols, sound_id, bank)
        sound_ids_base = symbols["wChannelSoundIDs"][1]
        chunks: list[np.ndarray] = []
        started = False
        tail_frames = round(0.2 * FPS)
        finished = False
        max_frames = max(1, round(max_seconds * FPS))

        for frame in range(max_frames):
            if not pyboy.tick(1, False, True):
                raise RuntimeError("The emulator stopped unexpectedly.")
            audio = np.asarray(pyboy.sound.ndarray)
            if audio.size:
                chunks.append(audio.copy())

            active = [
                pyboy.memory[sound_ids_base + index] for index in channels
            ]
            matching = any(value == sound_id for value in active)
            if matching:
                started = True
                tail_frames = round(0.2 * FPS)
            elif started:
                tail_frames -= 1
                if tail_frames <= 0:
                    finished = True

            if finished:
                break
            if (frame + 1) % round(FPS * 5) == 0:
                print(
                    f"  {(frame + 1) / FPS:.0f}/{max_seconds:g}s "
                    f"({'playing' if started else 'waiting for start'})",
                    flush=True,
                )

        if not started:
            raise RuntimeError(
                f"{sfx_label} never became active on its declared channels. "
                "Check the SFX ID, engine bank and header."
            )
        if not finished:
            raise RuntimeError(
                f"{sfx_label} did not finish within {max_seconds:g}s."
            )
        if not chunks:
            raise RuntimeError("PyBoy produced no audio samples.")

        samples = np.concatenate(chunks, axis=0)
        save_audio(output_path, samples, pyboy.sound.sample_rate, output_format)
        return output_path
    finally:
        pyboy.stop()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render Pokémon Yellow music and SFX through the original Game Boy audio engine."
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("song", nargs="?", help="Music label, e.g. Music_Celadon")
    selection.add_argument("--all", action="store_true", help="Render every music entry")
    selection.add_argument("--sfx", metavar="LABEL", help="Render one SFX, e.g. SFX_Level_Up")
    selection.add_argument("--all-sfx", action="store_true", help="Render every SFX")

    parser.add_argument("--list", action="store_true", help="List music entries")
    parser.add_argument("--list-sfx", action="store_true", help="List SFX entries and channels")
    parser.add_argument("--loops", type=int, default=1,
                        help="Main-loop cycles after intro (1..14; default: 1)")
    parser.add_argument("--fade-out", type=float, default=0.0,
                        help="Linear fade-out duration in seconds (default: 0)")
    parser.add_argument("--seconds", type=float, default=300.0,
                        help="Safety timeout per sound in seconds (default: 300)")
    parser.add_argument("--format", choices=("wav", "flac"), default="wav")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "audio" / "rendered",
                        help="Output directory (default: audio/rendered)")
    args = parser.parse_args()

    try:
        songs = parse_music_constants()
        effects = parse_sfx_headers()

        if args.list:
            for label in sorted(songs):
                print(label)
            return 0
        if args.list_sfx:
            for label in sorted(effects):
                engine, channels = effects[label]
                channel_text = ", ".join(f"CH{i + 1}" for i in sorted(channels))
                print(f"{label:<42} engine {engine}  {channel_text}")
            return 0

        if not (args.song or args.all or args.sfx or args.all_sfx):
            parser.error("Specify a song, --all, --sfx, --all-sfx, --list or --list-sfx.")
        if not 1 <= args.loops <= 14:
            parser.error("--loops must be between 1 and 14.")
        if args.fade_out < 0:
            parser.error("--fade-out cannot be negative.")
        if args.seconds <= 0:
            parser.error("--seconds must be greater than zero.")
        if (args.sfx or args.all_sfx) and (args.loops != 1 or args.fade_out != 0):
            parser.error("--loops and --fade-out apply only to music, not SFX.")

        selected_songs = (
            sorted(songs) if args.all else
            [resolve_song(args.song, songs)] if args.song else []
        )
        selected_sfx = (
            sorted(effects) if args.all_sfx else
            [resolve_sfx(args.sfx, effects)] if args.sfx else []
        )

        with tempfile.TemporaryDirectory(prefix="pokeyellow_audio_renderer_") as tmp:
            rom_path, sym_path = build_renderer_rom(Path(tmp))
            for label in selected_songs:
                capture_song(
                    rom_path, sym_path, label, args.output_dir,
                    args.loops, args.fade_out, args.seconds, args.format,
                )
            for label in selected_sfx:
                engine, channels = effects[label]
                capture_sfx(
                    rom_path, sym_path, label, engine, channels,
                    args.output_dir, args.seconds, args.format,
                )
        return 0

    except (
        OSError, RuntimeError, ValueError, subprocess.CalledProcessError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
