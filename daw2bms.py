#!/usr/bin/env python3
"""Convert exported DAW MIDI or FL Studio projects into a playable BMS draft.

This is intentionally conservative: direct DAW project formats such as .flp,
.als, and .cpr are not stable interchange formats. FLP support is best-effort
through pyflp and extracts note timing, not rendered plugin audio.
"""

from __future__ import annotations

import argparse
import bisect
import array
import csv
import hashlib
import json
import math
import shutil
import struct
import sys
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterable, List, Optional, Sequence, Tuple


BASE36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
DEFAULT_7K_CHANNELS = ["11", "12", "13", "14", "15", "18", "19"]
DEFAULT_5K_CHANNELS = ["11", "12", "13", "14", "15"]
DEFAULT_8K_CHANNELS = ["16", "11", "12", "13", "14", "15", "18", "19"]
DEFAULT_14K_CHANNELS = [
    "11",
    "12",
    "13",
    "14",
    "15",
    "18",
    "19",
    "21",
    "22",
    "23",
    "24",
    "25",
    "28",
    "29",
]
DEFAULT_SYNTH_SAMPLE_RATE = 44100
PIANO_LOW_NOTE = 21
PIANO_NOTE_COUNT = 88
PIANO_DURATION_PREFIXES = ("s", "m", "l")
MIDI_CHANNEL_VOLUME_CC = 7
MIDI_SUSTAIN_PEDAL_CC = 64
MIDI_SOSTENUTO_PEDAL_CC = 66
MIDI_PEDAL_ON_VALUE = 64


class MidiError(RuntimeError):
    pass


@dataclass(frozen=True)
class MidiNote:
    tick: int
    track: int
    channel: int
    note: int
    velocity: int
    duration_ticks: int = 0
    channel_volume: int = 127


@dataclass(frozen=True)
class ControlEvent:
    tick: int
    track: int
    channel: int
    controller: int
    value: int


@dataclass(frozen=True)
class TempoEvent:
    tick: int
    bpm: float


@dataclass(frozen=True)
class TimeSignatureEvent:
    tick: int
    numerator: int
    denominator: int


@dataclass
class MidiData:
    ticks_per_quarter: int
    notes: List[MidiNote]
    controls: List[ControlEvent]
    tempos: List[TempoEvent]
    time_signatures: List[TimeSignatureEvent]
    track_names: Dict[int, str]
    title: Optional[str] = None
    artist: Optional[str] = None
    genre: Optional[str] = None
    source_format: str = "midi"


@dataclass(frozen=True)
class PositionedEvent:
    measure: int
    slot: int
    channel: str
    code: str
    layer: int = 0


def base36_code(number: int) -> str:
    if number <= 0 or number >= 36 * 36:
        raise ValueError(f"code index out of BMS range: {number}")
    return BASE36[number // 36] + BASE36[number % 36]


def base36_number(code: str) -> int:
    if len(code) != 2:
        raise ValueError(f"invalid BMS WAV code: {code}")
    return BASE36.index(code[0]) * 36 + BASE36.index(code[1])


def next_free_wav_code(wav_defs: Dict[str, str]) -> str:
    used = {base36_number(code) for code in wav_defs}
    for number in range(1, 36 * 36):
        if number not in used:
            return base36_code(number)
    raise SystemExit("too many WAV definitions for one BMS namespace")


def read_exact(f: BinaryIO, size: int) -> bytes:
    data = f.read(size)
    if len(data) != size:
        raise MidiError("unexpected end of file")
    return data


def read_varlen(data: bytes, index: int) -> Tuple[int, int]:
    value = 0
    for _ in range(4):
        if index >= len(data):
            raise MidiError("unterminated variable-length value")
        byte = data[index]
        index += 1
        value = (value << 7) | (byte & 0x7F)
        if not (byte & 0x80):
            return value, index
    raise MidiError("variable-length value is too long")


def parse_midi(path: Path) -> MidiData:
    with path.open("rb") as f:
        if read_exact(f, 4) != b"MThd":
            raise MidiError("not a Standard MIDI file: missing MThd")
        header_size = struct.unpack(">I", read_exact(f, 4))[0]
        header = read_exact(f, header_size)
        if header_size < 6:
            raise MidiError("invalid MIDI header")
        fmt, track_count, division = struct.unpack(">HHH", header[:6])
        if fmt not in (0, 1):
            raise MidiError(f"unsupported MIDI format {fmt}; use format 0 or 1")
        if division & 0x8000:
            raise MidiError("SMPTE time division is not supported; export as ticks per beat")
        ticks_per_quarter = division

        notes: List[MidiNote] = []
        controls: List[ControlEvent] = []
        tempos = [TempoEvent(0, 120.0)]
        time_signatures = [TimeSignatureEvent(0, 4, 4)]
        track_names: Dict[int, str] = {}

        for track_index in range(track_count):
            chunk_type = read_exact(f, 4)
            chunk_size = struct.unpack(">I", read_exact(f, 4))[0]
            chunk = read_exact(f, chunk_size)
            if chunk_type != b"MTrk":
                continue
            parse_track(
                chunk,
                track_index,
                notes,
                controls,
                tempos,
                time_signatures,
                track_names,
            )

    tempos = dedupe_tick_events(sorted(tempos, key=lambda event: event.tick))
    time_signatures = dedupe_tick_events(sorted(time_signatures, key=lambda event: event.tick))
    return MidiData(
        ticks_per_quarter=ticks_per_quarter,
        notes=sorted(notes, key=lambda note: (note.tick, note.track, note.channel, note.note)),
        controls=sorted(controls, key=lambda event: (event.tick, event.track, event.channel, event.controller)),
        tempos=tempos,
        time_signatures=time_signatures,
        track_names=track_names,
        source_format="midi",
    )


def parse_flp(path: Path, args: argparse.Namespace) -> MidiData:
    try:
        import pyflp  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "FLP input requires pyflp. Run install_flp_support.bat or: python -m pip install pyflp"
        ) from exc
    except Exception as exc:
        raise SystemExit(
            "pyflp failed while loading. This is commonly caused by pyflp/Python 3.13 compatibility. "
            "Use Python 3.12 for FLP input, e.g. py -3.12 -m pip install pyflp, then "
            "py -3.12 daw2bms.py project.flp ... "
            f"Original error: {type(exc).__name__}: {exc}"
        ) from exc

    patch_pyflp_event_enum(pyflp)

    try:
        project = pyflp.parse(path)
    except Exception as exc:
        raise SystemExit(
            "pyflp could not parse this FLP. Export MIDI from FL Studio and use the .mid path, "
            "or try Python 3.12 with the latest pyflp. "
            f"Original error: {type(exc).__name__}: {exc}"
        ) from exc
    ppq = int(getattr(project, "ppq", 96) or 96)
    bpm = float(getattr(project, "tempo", None) or args.flp_default_bpm)
    numerator, denominator = flp_time_signature(project)
    title = string_or_none(getattr(project, "title", None))
    artist = string_or_none(getattr(project, "artists", None))
    genre = string_or_none(getattr(project, "genre", None))

    notes: List[MidiNote] = []
    track_names: Dict[int, str] = {}
    if not args.flp_patterns_only:
        notes, track_names = extract_flp_arrangement_notes(project)
    if not notes:
        notes, track_names = extract_flp_pattern_notes(project)

    return MidiData(
        ticks_per_quarter=ppq,
        notes=sorted(notes, key=lambda note: (note.tick, note.track, note.channel, note.note)),
        controls=[],
        tempos=[TempoEvent(0, bpm)],
        time_signatures=[TimeSignatureEvent(0, numerator, denominator)],
        track_names=track_names,
        title=title,
        artist=artist,
        genre=genre,
        source_format="flp",
    )


class UnknownPyflpEventID(int):
    type = None

    @property
    def value(self) -> int:
        return int(self)


class PyflpEventEnumCompat:
    def __init__(self, original: object):
        self.original = original

    def __call__(self, value: object) -> object:
        if isinstance(value, int):
            for subclass in self.__subclasses__():
                try:
                    return subclass(value)
                except (TypeError, ValueError):
                    continue
            if 0 <= value <= 255:
                return UnknownPyflpEventID(value)
        return value

    def __subclasses__(self) -> List[type]:
        try:
            return list(self.original.__subclasses__())  # type: ignore[attr-defined]
        except Exception:
            return []


def patch_pyflp_event_enum(pyflp_module: object) -> None:
    try:
        original = getattr(pyflp_module, "EventEnum")
        original(0)
        return
    except TypeError as exc:
        if "has no members" not in str(exc):
            return
    except Exception:
        return

    compat = PyflpEventEnumCompat(original)
    setattr(pyflp_module, "EventEnum", compat)
    try:
        import pyflp._events as pyflp_events  # type: ignore

        setattr(pyflp_events, "EventEnum", compat)
    except Exception:
        pass


def string_or_none(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def attr_or(value: object, name: str, default: object = None) -> object:
    try:
        return getattr(value, name)
    except Exception:
        return default


def int_attr(value: object, name: str, default: int = 0) -> int:
    try:
        return int(round(float(attr_or(value, name, default) or default)))
    except Exception:
        return default


def flp_time_signature(project: object) -> Tuple[int, int]:
    try:
        ts = attr_or(attr_or(project, "arrangements"), "time_signature")
        numerator = int(attr_or(ts, "num", 4) or 4)
        denominator = int(attr_or(ts, "beat", 4) or 4)
        if numerator > 0 and denominator > 0:
            return numerator, denominator
    except Exception:
        pass
    return 4, 4


def extract_flp_arrangement_notes(project: object) -> Tuple[List[MidiNote], Dict[int, str]]:
    notes: List[MidiNote] = []
    track_names: Dict[int, str] = flp_channel_names(project)
    arrangement_collection = attr_or(project, "arrangements")
    arrangements = list_safe(arrangement_collection)
    try:
        current = attr_or(arrangement_collection, "current")
    except Exception:
        current = None
    if current is not None:
        arrangements = [current]

    for arrangement_index, arrangement in enumerate(arrangements):
        for track_index, track in enumerate(list_safe(attr_or(arrangement, "tracks", []))):
            name = string_or_none(attr_or(track, "name"))
            if name:
                track_names[track_index] = name
            for item in list_safe(track):
                if bool(attr_or(item, "muted", False)):
                    continue
                pattern = attr_or(item, "pattern")
                if pattern is None:
                    continue
                item_position = int_attr(item, "position")
                item_length = int_attr(item, "length")
                start_offset = 0
                try:
                    start_offset = int(round(float(attr_or(item, "offsets", [0])[0] or 0)))  # type: ignore[index]
                except Exception:
                    pass
                for note in list_safe(attr_or(pattern, "notes", [])):
                    rack_channel = int_attr(note, "rack_channel", track_index)
                    note_position = int_attr(note, "position")
                    if note_position < start_offset:
                        continue
                    local_position = note_position - start_offset
                    if item_length and local_position > item_length:
                        continue
                    notes.append(
                        MidiNote(
                            tick=item_position + local_position,
                            track=rack_channel,
                            channel=flp_note_channel(note),
                            note=flp_note_number(note),
                            velocity=flp_note_velocity(note),
                            duration_ticks=flp_note_duration(note),
                        )
                    )
    return notes, track_names


def extract_flp_pattern_notes(project: object) -> Tuple[List[MidiNote], Dict[int, str]]:
    notes: List[MidiNote] = []
    track_names: Dict[int, str] = flp_channel_names(project)
    for pattern_index, pattern in enumerate(list_safe(attr_or(project, "patterns", []))):
        name = string_or_none(attr_or(pattern, "name"))
        if name:
            track_names.setdefault(pattern_index, name)
        for note in list_safe(attr_or(pattern, "notes", [])):
            rack_channel = int_attr(note, "rack_channel", pattern_index)
            notes.append(
                MidiNote(
                    tick=int_attr(note, "position"),
                    track=rack_channel,
                    channel=flp_note_channel(note),
                    note=flp_note_number(note),
                    velocity=flp_note_velocity(note),
                    duration_ticks=flp_note_duration(note),
                )
            )
    return notes, track_names


def flp_channel_names(project: object) -> Dict[int, str]:
    names: Dict[int, str] = {}
    for fallback_index, channel in enumerate(list_safe(attr_or(project, "channels", []))):
        channel_index = int_attr(channel, "iid", fallback_index)
        name = string_or_none(attr_or(channel, "name"))
        if name:
            names[channel_index] = name
    return names


def list_safe(value: object) -> List[object]:
    try:
        return list(value)  # type: ignore[arg-type]
    except Exception:
        return []


def flp_note_number(note: object) -> int:
    try:
        value = note["key"]  # type: ignore[index]
        return int(value)
    except Exception:
        pass
    key = attr_or(note, "key", 60)
    if isinstance(key, str):
        return parse_flp_key_name(key)
    return int(key)


def parse_flp_key_name(key: str) -> int:
    names = {"C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "F": 5, "F#": 6, "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11}
    for name in sorted(names, key=len, reverse=True):
        if key.startswith(name):
            return int(key[len(name) :]) * 12 + names[name]
    raise ValueError(f"invalid FL note key: {key}")


def flp_note_velocity(note: object) -> int:
    try:
        velocity = int(attr_or(note, "velocity", 100))
    except Exception:
        velocity = 100
    return max(1, min(127, velocity))


def flp_note_duration(note: object) -> int:
    try:
        duration = int(round(float(attr_or(note, "length", 0))))
    except Exception:
        duration = 0
    return max(0, duration)


def flp_note_channel(note: object) -> int:
    try:
        channel = int(attr_or(note, "rack_channel", 0)) + 1
    except Exception:
        channel = 1
    return max(1, min(16, channel))


def dedupe_tick_events(events: Sequence) -> List:
    by_tick = {}
    for event in events:
        by_tick[event.tick] = event
    return [by_tick[tick] for tick in sorted(by_tick)]


def parse_track(
    chunk: bytes,
    track_index: int,
    notes: List[MidiNote],
    controls: List[ControlEvent],
    tempos: List[TempoEvent],
    time_signatures: List[TimeSignatureEvent],
    track_names: Dict[int, str],
) -> None:
    tick = 0
    index = 0
    running_status: Optional[int] = None
    active_notes: Dict[Tuple[int, int], List[Tuple[int, int, int]]] = {}
    channel_volumes: Dict[int, int] = {}

    while index < len(chunk):
        delta, index = read_varlen(chunk, index)
        tick += delta
        if index >= len(chunk):
            break

        status = chunk[index]
        if status & 0x80:
            index += 1
            running_status = status
        elif running_status is not None:
            status = running_status
        else:
            raise MidiError(f"track {track_index}: running status before status byte")

        if status == 0xFF:
            if index >= len(chunk):
                raise MidiError(f"track {track_index}: truncated meta event")
            meta_type = chunk[index]
            index += 1
            length, index = read_varlen(chunk, index)
            payload = chunk[index : index + length]
            index += length
            if meta_type == 0x2F:
                break
            if meta_type == 0x03:
                track_names[track_index] = payload.decode("utf-8", errors="replace")
            elif meta_type == 0x51 and len(payload) == 3:
                microseconds = int.from_bytes(payload, byteorder="big")
                if microseconds:
                    tempos.append(TempoEvent(tick, 60_000_000.0 / microseconds))
            elif meta_type == 0x58 and len(payload) >= 2:
                denominator = 2 ** payload[1]
                time_signatures.append(TimeSignatureEvent(tick, payload[0], denominator))
            continue

        if status in (0xF0, 0xF7):
            length, index = read_varlen(chunk, index)
            index += length
            continue

        event_type = status & 0xF0
        channel = (status & 0x0F) + 1
        data_len = 1 if event_type in (0xC0, 0xD0) else 2
        payload = chunk[index : index + data_len]
        index += data_len
        if len(payload) != data_len:
            raise MidiError(f"track {track_index}: truncated MIDI event")

        if event_type == 0x90 and payload[1] > 0:
            active_notes.setdefault((channel, payload[0]), []).append(
                (tick, payload[1], channel_volumes.get(channel, 127))
            )
        elif event_type == 0x80 or (event_type == 0x90 and payload[1] == 0):
            key = (channel, payload[0])
            pending = active_notes.get(key)
            if pending:
                start_tick, velocity, channel_volume = pending.pop(0)
                if not pending:
                    active_notes.pop(key, None)
                notes.append(
                    MidiNote(
                        start_tick,
                        track_index,
                        channel,
                        payload[0],
                        velocity,
                        max(0, tick - start_tick),
                        channel_volume,
                    )
                )
        elif event_type == 0xB0:
            controller = payload[0]
            value = payload[1]
            if controller in (MIDI_CHANNEL_VOLUME_CC, MIDI_SUSTAIN_PEDAL_CC, MIDI_SOSTENUTO_PEDAL_CC):
                controls.append(ControlEvent(tick, track_index, channel, controller, value))
            if controller == MIDI_CHANNEL_VOLUME_CC:
                channel_volumes[channel] = value

    for (channel, note_number), pending_notes in active_notes.items():
        for start_tick, velocity, channel_volume in pending_notes:
            notes.append(
                MidiNote(
                    start_tick,
                    track_index,
                    channel,
                    note_number,
                    velocity,
                    max(0, tick - start_tick),
                    channel_volume,
                )
            )


def parse_lane_channels(args: argparse.Namespace) -> List[str]:
    if args.lane_channels:
        channels = [part.strip().upper() for part in args.lane_channels.split(",") if part.strip()]
        if not channels:
            raise SystemExit("--lane-channels cannot be empty")
        for channel in channels:
            if len(channel) != 2 or any(ch not in BASE36 for ch in channel):
                raise SystemExit(f"invalid BMS channel code: {channel}")
        return channels
    if args.keys == 5:
        return DEFAULT_5K_CHANNELS
    if args.keys == 7:
        return DEFAULT_7K_CHANNELS
    if args.keys == 8:
        return DEFAULT_8K_CHANNELS
    if args.keys == 14:
        return DEFAULT_14K_CHANNELS
    raise SystemExit("--keys must be 5, 7, 8, or 14 unless --lane-channels is set")


def parse_note_lanes(value: str, lane_count: int) -> Dict[int, int]:
    parts = [part.strip() for part in value.split(",") if part.strip()]
    if len(parts) != lane_count:
        raise SystemExit(f"--lane-notes must contain exactly {lane_count} MIDI note numbers")
    mapping = {}
    for lane_index, part in enumerate(parts):
        note = int(part)
        if note < 0 or note > 127:
            raise SystemExit(f"invalid MIDI note number in --lane-notes: {note}")
        mapping[note] = lane_index
    return mapping


def filter_notes(
    notes: Iterable[MidiNote],
    args: argparse.Namespace,
    use_track_filter: bool = True,
    use_channel_filter: bool = True,
    use_piano_soft_filter: bool = True,
) -> List[MidiNote]:
    allowed_channels = parse_int_set(args.midi_channels)
    allowed_tracks = parse_int_set(args.tracks, zero_based=True)
    result = []
    for note in notes:
        if getattr(args, "piano_bms", False):
            if note.channel == 10:
                continue
            if note.note < args.piano_low_note or note.note >= args.piano_low_note + PIANO_NOTE_COUNT:
                continue
            if use_piano_soft_filter:
                if note.velocity < args.piano_min_velocity:
                    continue
                if note.channel_volume < args.piano_min_channel_volume:
                    continue
        if use_channel_filter and allowed_channels is not None and note.channel not in allowed_channels:
            continue
        if use_track_filter and allowed_tracks is not None and note.track not in allowed_tracks:
            continue
        if args.min_note is not None and note.note < args.min_note:
            continue
        if args.max_note is not None and note.note > args.max_note:
            continue
        result.append(note)
    return result


def filter_soft_piano_notes(notes: Iterable[MidiNote], args: argparse.Namespace) -> List[MidiNote]:
    return [
        note
        for note in notes
        if note.velocity >= args.piano_min_velocity and note.channel_volume >= args.piano_min_channel_volume
    ]


def split_player_background_notes(notes: Sequence[MidiNote], args: argparse.Namespace) -> Tuple[List[MidiNote], List[MidiNote]]:
    player_tracks = parse_int_set(args.player_tracks, zero_based=True)
    player_channels = parse_int_set(args.player_midi_channels)
    if player_tracks is None and player_channels is None:
        return list(notes), []

    player_notes = []
    background_notes = []
    for note in notes:
        is_player = True
        if player_tracks is not None and note.track not in player_tracks:
            is_player = False
        if player_channels is not None and note.channel not in player_channels:
            is_player = False
        if is_player:
            player_notes.append(note)
        else:
            background_notes.append(note)
    return player_notes, background_notes


def parse_int_set(value: Optional[str], zero_based: bool = False) -> Optional[set[int]]:
    if not value:
        return None
    result: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        number = int(part)
        if not zero_based and number < 1:
            raise SystemExit(f"expected positive number in filter: {number}")
        if zero_based and number < 0:
            raise SystemExit(f"expected non-negative track number: {number}")
        result.add(number)
    return result


def build_lane_mapper(notes: Sequence[MidiNote], lane_count: int, args: argparse.Namespace) -> Tuple[Dict[int, int], str]:
    if args.lane_notes:
        return parse_note_lanes(args.lane_notes, lane_count), "explicit lane-notes"

    unique_notes = sorted({note.note for note in notes})
    if not unique_notes:
        return {}, "empty"
    if args.mapping == "pitch-order":
        return {note: index % lane_count for index, note in enumerate(unique_notes)}, "pitch-order"
    if args.mapping == "pitch-mod":
        return {note: note % lane_count for note in unique_notes}, "pitch-mod"
    raise SystemExit(f"unknown mapping mode: {args.mapping}")


def tick_to_measure_slot(
    tick: int,
    ticks_per_quarter: int,
    numerator: int,
    denominator: int,
    resolution: int,
) -> Tuple[int, int]:
    beats_per_measure = numerator * (4.0 / denominator)
    ticks_per_measure = ticks_per_quarter * beats_per_measure
    measure_float = tick / ticks_per_measure
    measure = int(math.floor(measure_float + 1e-9))
    position = (measure_float - measure) * resolution
    slot = int(round(position))
    if slot >= resolution:
        measure += 1
        slot = 0
    return measure, slot


def read_sample_map(path: Optional[Path]) -> Dict[int, str]:
    if not path:
        return {}
    mapping: Dict[int, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if "note" not in (reader.fieldnames or []) or "wav" not in (reader.fieldnames or []):
            raise SystemExit("sample map CSV must contain note,wav columns")
        for row in reader:
            note = int(row["note"])
            wav_name = row["wav"].strip()
            if wav_name:
                mapping[note] = wav_name
    return mapping


def parse_track_audio_map(value: Optional[str]) -> Dict[int, Path]:
    if not value:
        return {}
    mapping: Dict[int, Path] = {}
    normalized = value.replace(";", ",")
    for raw_entry in normalized.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise SystemExit("track audio map entries must look like track=wav, e.g. 12=Lead.wav")
        raw_track, raw_path = entry.split("=", 1)
        raw_track = raw_track.strip()
        raw_path = raw_path.strip().strip('"')
        if not raw_track or not raw_path:
            raise SystemExit("track audio map entries must include both track number and WAV path")
        try:
            track = int(raw_track)
        except ValueError as exc:
            raise SystemExit(f"invalid track number in --track-audio-map: {raw_track}") from exc
        mapping[track] = Path(raw_path)
    return mapping


def parse_track_gain_map(value: Optional[str]) -> Dict[int, float]:
    if not value:
        return {}
    mapping: Dict[int, float] = {}
    normalized = value.replace(";", ",")
    for raw_entry in normalized.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise SystemExit("track gain map entries must look like track=db, e.g. 11=3")
        raw_track, raw_gain = entry.split("=", 1)
        raw_track = raw_track.strip()
        raw_gain = raw_gain.strip().lower().removesuffix("db").strip()
        if not raw_track or not raw_gain:
            raise SystemExit("track gain map entries must include both track number and dB gain")
        try:
            track = int(raw_track)
            gain_db = float(raw_gain)
        except ValueError as exc:
            raise SystemExit(f"invalid --track-gain-map entry: {entry}") from exc
        mapping[track] = gain_db
    return mapping


def parse_track_ms_map(value: Optional[str], option_name: str) -> Dict[int, int]:
    if not value:
        return {}
    mapping: Dict[int, int] = {}
    normalized = value.replace(";", ",")
    for raw_entry in normalized.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise SystemExit(f"{option_name} entries must look like track=ms, e.g. 2=500")
        raw_track, raw_ms = entry.split("=", 1)
        raw_track = raw_track.strip()
        raw_ms = raw_ms.strip().lower().removesuffix("ms").strip()
        if not raw_track or not raw_ms:
            raise SystemExit(f"{option_name} entries must include both track number and milliseconds")
        try:
            track = int(raw_track)
            milliseconds = int(round(float(raw_ms)))
        except ValueError as exc:
            raise SystemExit(f"invalid {option_name} entry: {entry}") from exc
        if track < 0:
            raise SystemExit(f"{option_name} track numbers must be non-negative: {track}")
        if milliseconds < 1:
            raise SystemExit(f"{option_name} milliseconds must be positive: {entry}")
        mapping[track] = milliseconds
    return mapping


def normalize_audio_name(value: str) -> str:
    return "".join(char.lower() for char in value if char.isalnum())


def find_matching_wav(track: int, name: str, wavs: Sequence[Path]) -> Optional[Path]:
    normalized_name = normalize_audio_name(name)
    if not normalized_name:
        return None
    normalized_with_track = normalize_audio_name(f"{track}{name}")
    indexed = [(path, normalize_audio_name(path.stem)) for path in wavs]

    for path, normalized_stem in indexed:
        if normalized_stem in {normalized_name, normalized_with_track}:
            return path

    candidates = []
    for path, normalized_stem in indexed:
        if normalized_stem.endswith(normalized_name) or normalized_name in normalized_stem:
            candidates.append(path)
    if len(candidates) == 1:
        return candidates[0]
    return None


def discover_track_audio_map(directory_value: Optional[str], track_names: Dict[int, str]) -> Dict[int, Path]:
    if not directory_value:
        return {}
    directory = Path(directory_value)
    if not directory.exists() or not directory.is_dir():
        raise SystemExit(f"auto track audio directory not found: {directory}")
    wavs = sorted(directory.glob("*.wav"), key=lambda path: path.name.lower())
    if not wavs:
        raise SystemExit(f"auto track audio directory has no WAV files: {directory}")

    mapping: Dict[int, Path] = {}
    for track, name in sorted(track_names.items()):
        match = find_matching_wav(track, name, wavs)
        if match is not None:
            mapping[track] = match
    return mapping


def allocate_wavs(
    args: argparse.Namespace,
    notes: Sequence[MidiNote],
    sample_map: Dict[int, str],
    output_dir: Path,
) -> Tuple[Dict[str, str], Dict[int, str], str, Optional[str]]:
    wav_defs: Dict[str, str] = {}
    note_codes: Dict[int, str] = {}
    next_code = 1

    default_note_wav = args.default_note_wav
    if not default_note_wav and args.make_silence_wav:
        default_note_wav = "silence.wav"
        write_silence_wav(output_dir / default_note_wav)

    if default_note_wav:
        code = base36_code(next_code)
        next_code += 1
        wav_defs[code] = default_note_wav
        default_note_code = code
    else:
        default_note_code = "01"
        wav_defs[default_note_code] = "silence.wav"
        if args.make_silence_wav:
            write_silence_wav(output_dir / "silence.wav")
        next_code = 2

    for note_number in sorted({note.note for note in notes}):
        wav_name = sample_map.get(note_number)
        if not wav_name:
            note_codes[note_number] = default_note_code
            continue
        code = base36_code(next_code)
        next_code += 1
        wav_defs[code] = wav_name
        note_codes[note_number] = code

    bgm_code: Optional[str] = None
    if args.bgm:
        code = base36_code(next_code)
        next_code += 1
        bgm_code = code
        wav_defs[code] = Path(args.bgm).name

    return wav_defs, note_codes, default_note_code, bgm_code


def write_silence_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00" * 441)


def tick_to_seconds(tick: int, ticks_per_quarter: int, tempos: Sequence[TempoEvent]) -> float:
    ordered = sorted(tempos, key=lambda event: event.tick) or [TempoEvent(0, 120.0)]
    if ordered[0].tick != 0:
        ordered.insert(0, TempoEvent(0, 120.0))

    seconds = 0.0
    previous_tick = ordered[0].tick
    previous_bpm = ordered[0].bpm
    if tick <= previous_tick:
        return 0.0

    for event in ordered[1:]:
        if tick <= event.tick:
            break
        seconds += (event.tick - previous_tick) * 60.0 / (previous_bpm * ticks_per_quarter)
        previous_tick = event.tick
        previous_bpm = event.bpm
    seconds += (tick - previous_tick) * 60.0 / (previous_bpm * ticks_per_quarter)
    return seconds


def seconds_to_tick(seconds: float, ticks_per_quarter: int, tempos: Sequence[TempoEvent]) -> int:
    ordered = sorted(tempos, key=lambda event: event.tick) or [TempoEvent(0, 120.0)]
    if ordered[0].tick != 0:
        ordered.insert(0, TempoEvent(0, 120.0))

    elapsed = 0.0
    previous_tick = ordered[0].tick
    previous_bpm = ordered[0].bpm
    if seconds <= 0:
        return 0

    for event in ordered[1:]:
        segment_seconds = (event.tick - previous_tick) * 60.0 / (previous_bpm * ticks_per_quarter)
        if elapsed + segment_seconds >= seconds:
            return int(round(previous_tick + (seconds - elapsed) * previous_bpm * ticks_per_quarter / 60.0))
        elapsed += segment_seconds
        previous_tick = event.tick
        previous_bpm = event.bpm

    return int(round(previous_tick + (seconds - elapsed) * previous_bpm * ticks_per_quarter / 60.0))


def trim_notes_to_max_seconds(
    notes: Sequence[MidiNote],
    midi: MidiData,
    args: argparse.Namespace,
) -> Tuple[List[MidiNote], int, int]:
    if args.max_seconds <= 0:
        return list(notes), 0, 0

    max_seconds = float(args.max_seconds)
    cutoff_tick = seconds_to_tick(max_seconds, midi.ticks_per_quarter, midi.tempos)
    trimmed: List[MidiNote] = []
    dropped = 0
    clipped = 0
    for note in notes:
        start_seconds = tick_to_seconds(note.tick, midi.ticks_per_quarter, midi.tempos)
        if start_seconds >= max_seconds:
            dropped += 1
            continue
        end_tick = note.tick + max(0, note.duration_ticks)
        if end_tick > cutoff_tick:
            duration_ticks = max(1, cutoff_tick - note.tick)
            trimmed.append(
                MidiNote(
                    tick=note.tick,
                    track=note.track,
                    channel=note.channel,
                    note=note.note,
                    velocity=note.velocity,
                    duration_ticks=duration_ticks,
                    channel_volume=note.channel_volume,
                )
            )
            clipped += 1
        else:
            trimmed.append(note)
    return trimmed, dropped, clipped


def validate_piano_args(args: argparse.Namespace) -> None:
    if args.piano_low_note < 0 or args.piano_low_note + PIANO_NOTE_COUNT - 1 > 127:
        raise SystemExit("--piano-low-note must allow exactly 88 MIDI notes within 0..127")
    if args.piano_medium_ms < 0 or args.piano_long_ms < 0:
        raise SystemExit("--piano-medium-ms and --piano-long-ms must be non-negative")
    if args.piano_long_ms < args.piano_medium_ms:
        raise SystemExit("--piano-long-ms must be greater than or equal to --piano-medium-ms")
    if args.piano_min_velocity < 1 or args.piano_min_velocity > 127:
        raise SystemExit("--piano-min-velocity must be in 1..127")
    if args.piano_min_channel_volume < 0 or args.piano_min_channel_volume > 127:
        raise SystemExit("--piano-min-channel-volume must be in 0..127")


def control_intervals(
    controls: Sequence[ControlEvent],
    controller: int,
    final_tick: int,
) -> Dict[Tuple[int, int], List[Tuple[int, int]]]:
    intervals: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
    active: Dict[Tuple[int, int], int] = {}
    for event in sorted((event for event in controls if event.controller == controller), key=lambda event: event.tick):
        key = (event.track, event.channel)
        is_on = event.value >= MIDI_PEDAL_ON_VALUE
        if is_on:
            active.setdefault(key, event.tick)
        elif key in active:
            start_tick = active.pop(key)
            if event.tick >= start_tick:
                intervals.setdefault(key, []).append((start_tick, event.tick))
    for key, start_tick in active.items():
        if final_tick >= start_tick:
            intervals.setdefault(key, []).append((start_tick, final_tick))
    return intervals


def next_same_pitch_starts(notes: Sequence[MidiNote]) -> List[Optional[int]]:
    starts_by_key: Dict[Tuple[int, int, int], List[int]] = {}
    for note in notes:
        starts_by_key.setdefault((note.track, note.channel, note.note), []).append(note.tick)
    for key, starts in list(starts_by_key.items()):
        starts_by_key[key] = sorted(set(starts))

    result: List[Optional[int]] = []
    for note in notes:
        starts = starts_by_key[(note.track, note.channel, note.note)]
        next_index = bisect.bisect_right(starts, note.tick)
        result.append(starts[next_index] if next_index < len(starts) else None)
    return result


def interval_end_at_tick(intervals: Sequence[Tuple[int, int]], tick: int) -> Optional[int]:
    for start_tick, end_tick in intervals:
        if start_tick <= tick < end_tick:
            return end_tick
    return None


def apply_piano_pedal_durations(notes: Sequence[MidiNote], midi: MidiData) -> List[MidiNote]:
    if not notes or not midi.controls:
        return list(notes)

    final_tick = max(
        [event.tick for event in midi.controls]
        + [note.tick + max(0, note.duration_ticks) for note in notes],
        default=0,
    )
    sustain_intervals = control_intervals(midi.controls, MIDI_SUSTAIN_PEDAL_CC, final_tick)
    sostenuto_intervals = control_intervals(midi.controls, MIDI_SOSTENUTO_PEDAL_CC, final_tick)
    next_starts = next_same_pitch_starts(notes)
    adjusted: List[MidiNote] = []

    for index, note in enumerate(notes):
        base_end_tick = note.tick + max(0, note.duration_ticks)
        effective_end_tick = base_end_tick
        key = (note.track, note.channel)

        sustain_end = interval_end_at_tick(sustain_intervals.get(key, []), base_end_tick)
        if sustain_end is not None:
            effective_end_tick = max(effective_end_tick, sustain_end)

        for start_tick, end_tick in sostenuto_intervals.get(key, []):
            if note.tick <= start_tick < base_end_tick:
                effective_end_tick = max(effective_end_tick, end_tick)

        next_start = next_starts[index]
        if next_start is not None and effective_end_tick > next_start:
            effective_end_tick = next_start

        if effective_end_tick == base_end_tick:
            adjusted.append(note)
            continue
        adjusted.append(
            MidiNote(
                tick=note.tick,
                track=note.track,
                channel=note.channel,
                note=note.note,
                velocity=note.velocity,
                duration_ticks=max(0, effective_end_tick - note.tick),
                channel_volume=note.channel_volume,
            )
        )
    return adjusted


def piano_duration_prefix(note: MidiNote, midi: MidiData, args: argparse.Namespace) -> str:
    start = tick_to_seconds(note.tick, midi.ticks_per_quarter, midi.tempos)
    end = tick_to_seconds(note.tick + max(0, note.duration_ticks), midi.ticks_per_quarter, midi.tempos)
    duration_ms = max(0.0, (end - start) * 1000.0)
    if duration_ms >= args.piano_long_ms:
        return "l"
    if duration_ms >= args.piano_medium_ms:
        return "m"
    return "s"


def piano_sample_filename(prefix: str, pitch_index: int) -> str:
    return f"{prefix}_{pitch_index:03d}.wav"


def piano_sample_code(note: MidiNote, midi: MidiData, args: argparse.Namespace) -> str:
    pitch_index = note.note - args.piano_low_note
    if pitch_index < 0 or pitch_index >= PIANO_NOTE_COUNT:
        return ""
    prefix = piano_duration_prefix(note, midi, args)
    duration_index = PIANO_DURATION_PREFIXES.index(prefix)
    return base36_code(1 + duration_index * PIANO_NOTE_COUNT + pitch_index)


def allocate_piano_sample_wavs(
    args: argparse.Namespace,
    notes: Sequence[MidiNote],
    midi: MidiData,
) -> Tuple[Dict[str, str], List[str], Optional[str]]:
    wav_defs: Dict[str, str] = {}
    for duration_index, prefix in enumerate(PIANO_DURATION_PREFIXES):
        for pitch_index in range(PIANO_NOTE_COUNT):
            code = base36_code(1 + duration_index * PIANO_NOTE_COUNT + pitch_index)
            wav_defs[code] = piano_sample_filename(prefix, pitch_index)

    instance_codes = [piano_sample_code(note, midi, args) for note in notes]
    bgm_code: Optional[str] = None
    if args.bgm:
        bgm_code = next_free_wav_code(wav_defs)
        wav_defs[bgm_code] = Path(args.bgm).name
    return wav_defs, instance_codes, bgm_code


def merge_same_time_keysound_notes(
    notes: Sequence[MidiNote],
    midi: MidiData,
    args: argparse.Namespace,
) -> Tuple[List[MidiNote], int, int]:
    if not args.merge_same_time_keysounds:
        return list(notes), 0, 0

    source_path = Path(args.keysound_source) if args.keysound_source else None
    track_audio_map = discover_track_audio_map(args.auto_track_audio_dir, midi.track_names)
    track_audio_map.update(parse_track_audio_map(args.track_audio_map))

    grouped: Dict[Tuple[int, str], List[MidiNote]] = {}
    for note in notes:
        note_source_path = track_audio_map.get(note.track, source_path)
        source_identity = str(note_source_path) if note_source_path is not None else f"track:{note.track}"
        grouped.setdefault((note.tick, source_identity), []).append(note)

    merged: List[MidiNote] = []
    max_group_size = 0
    for group in grouped.values():
        max_group_size = max(max_group_size, len(group))
        if len(group) == 1:
            merged.append(group[0])
            continue
        representative = max(group, key=lambda note: (note.duration_ticks, note.velocity, -note.note))
        merged.append(
            MidiNote(
                tick=representative.tick,
                track=representative.track,
                channel=representative.channel,
                note=representative.note,
                velocity=max(note.velocity for note in group),
                duration_ticks=max(note.duration_ticks for note in group),
                channel_volume=max(note.channel_volume for note in group),
            )
        )
    merged.sort(key=lambda note: (note.tick, note.track, note.note, note.channel))
    return merged, len(notes) - len(merged), max_group_size


def apply_pcm_fade(
    frames: bytes,
    sample_width: int,
    nchannels: int,
    frame_rate: int,
    fade_in_ms: float,
    fade_out_ms: float,
) -> bytes:
    """Apply a short linear fade-in/out to declick a raw PCM slice.

    Cutting a wet stem at an arbitrary sample produces a click at both edges.
    A few-millisecond ramp on each end removes it without audibly changing the sound.
    """
    if sample_width != 2 or not frames or (fade_in_ms <= 0 and fade_out_ms <= 0):
        return frames
    nchan = max(1, nchannels)
    total_samples = len(frames) // 2
    total_frames = total_samples // nchan
    if total_frames <= 1:
        return frames
    fade_in_frames = min(total_frames, max(0, int(round(fade_in_ms / 1000.0 * frame_rate))))
    fade_out_frames = min(total_frames, max(0, int(round(fade_out_ms / 1000.0 * frame_rate))))
    if fade_in_frames + fade_out_frames > total_frames:
        fade_out_frames = max(0, total_frames - fade_in_frames)
    if fade_in_frames <= 0 and fade_out_frames <= 0:
        return frames
    values = list(struct.unpack("<" + "h" * total_samples, frames[: total_samples * 2]))
    for f in range(fade_in_frames):
        gain = (f + 1) / (fade_in_frames + 1)
        base = f * nchan
        for c in range(nchan):
            values[base + c] = int(values[base + c] * gain)
    for f in range(fade_out_frames):
        gain = (f + 1) / (fade_out_frames + 1)
        base = (total_frames - 1 - f) * nchan
        for c in range(nchan):
            values[base + c] = int(values[base + c] * gain)
    return struct.pack("<" + "h" * total_samples, *values) + frames[total_samples * 2 :]


def write_wav_slice(
    source_path: Path,
    output_path: Path,
    start_seconds: float,
    duration_seconds: float,
    fade_in_ms: float = 0.0,
    fade_out_ms: float = 0.0,
) -> int:
    if start_seconds < 0:
        duration_seconds += start_seconds
        start_seconds = 0.0
    if duration_seconds <= 0:
        duration_seconds = 0.01

    with wave.open(str(source_path), "rb") as src:
        frame_rate = src.getframerate()
        start_frame = max(0, int(round(start_seconds * frame_rate)))
        frame_count = max(1, int(round(duration_seconds * frame_rate)))
        src.setpos(min(start_frame, src.getnframes()))
        frames = src.readframes(frame_count)
        params = src.getparams()
        expected_bytes = frame_count * params.nchannels * params.sampwidth
        if len(frames) < expected_bytes:
            frames += b"\x00" * (expected_bytes - len(frames))
        frames = apply_pcm_fade(frames, params.sampwidth, params.nchannels, frame_rate, fade_in_ms, fade_out_ms)
        peak = pcm_peak(frames, params.sampwidth)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as dst:
        dst.setparams(params)
        dst.writeframes(frames)
    return peak


def pcm_peak(frames: bytes, sample_width: int) -> int:
    if not frames:
        return 0
    if sample_width == 1:
        return max(abs(value - 128) for value in frames)
    if sample_width == 2:
        usable = len(frames) - (len(frames) % 2)
        if usable <= 0:
            return 0
        values = struct.unpack("<" + "h" * (usable // 2), frames[:usable])
        return max((abs(value) for value in values), default=0)
    return 1


def normalize_target_peak(sample_width: int, target_db: float) -> int:
    if sample_width == 1:
        max_peak = 127
    elif sample_width == 2:
        max_peak = 32767
    else:
        return 0
    gain = 10.0 ** (target_db / 20.0)
    return max(1, min(max_peak, int(round(max_peak * gain))))


def normalize_wav_file(path: Path, target_db: float, min_peak: int) -> int:
    with wave.open(str(path), "rb") as src:
        params = src.getparams()
        frames = src.readframes(src.getnframes())

    peak = pcm_peak(frames, params.sampwidth)
    target_peak = normalize_target_peak(params.sampwidth, target_db)
    if peak < max(1, min_peak) or target_peak <= 0:
        return peak

    scale = target_peak / peak
    if params.sampwidth == 1:
        output = bytearray()
        for value in frames:
            signed = value - 128
            normalized = int(round(signed * scale)) + 128
            output.append(max(0, min(255, normalized)))
        frames = bytes(output)
    elif params.sampwidth == 2:
        usable = len(frames) - (len(frames) % 2)
        values = struct.unpack("<" + "h" * (usable // 2), frames[:usable])
        output = bytearray()
        for value in values:
            normalized = int(round(value * scale))
            output += struct.pack("<h", max(-32768, min(32767, normalized)))
        if usable < len(frames):
            output += frames[usable:]
        frames = bytes(output)

    with wave.open(str(path), "wb") as dst:
        dst.setparams(params)
        dst.writeframes(frames)
    return pcm_peak(frames, params.sampwidth)


def apply_gain_to_wav_file(path: Path, gain_db: float) -> int:
    if abs(gain_db) < 1e-9:
        with wave.open(str(path), "rb") as src:
            return pcm_peak(src.readframes(src.getnframes()), src.getsampwidth())

    with wave.open(str(path), "rb") as src:
        params = src.getparams()
        frames = src.readframes(src.getnframes())

    scale = 10.0 ** (gain_db / 20.0)
    if params.sampwidth == 1:
        output = bytearray()
        for value in frames:
            signed = value - 128
            gained = int(round(signed * scale)) + 128
            output.append(max(0, min(255, gained)))
        frames = bytes(output)
    elif params.sampwidth == 2:
        usable = len(frames) - (len(frames) % 2)
        values = struct.unpack("<" + "h" * (usable // 2), frames[:usable])
        output = bytearray()
        for value in values:
            gained = int(round(value * scale))
            output += struct.pack("<h", max(-32768, min(32767, gained)))
        if usable < len(frames):
            output += frames[usable:]
        frames = bytes(output)
    else:
        return pcm_peak(frames, params.sampwidth)

    with wave.open(str(path), "wb") as dst:
        dst.setparams(params)
        dst.writeframes(frames)
    return pcm_peak(frames, params.sampwidth)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wav_file_peak(path: Path) -> int:
    with wave.open(str(path), "rb") as src:
        params = src.getparams()
        frames = src.readframes(src.getnframes())
    return pcm_peak(frames, params.sampwidth)


def note_frequency(note_number: int) -> float:
    return 440.0 * (2.0 ** ((note_number - 69) / 12.0))


def synth_wave_value(phase: float, waveform: str) -> float:
    if waveform == "square":
        return 1.0 if math.sin(phase) >= 0 else -1.0
    if waveform == "triangle":
        return 2.0 / math.pi * math.asin(math.sin(phase))
    if waveform == "saw":
        return 2.0 * ((phase / (2.0 * math.pi)) % 1.0) - 1.0
    return math.sin(phase)


def envelope_value(index: int, total_frames: int, sample_rate: int) -> float:
    attack = max(1, int(0.005 * sample_rate))
    release = max(1, int(0.025 * sample_rate))
    if index < attack:
        return index / attack
    remaining = total_frames - index - 1
    if remaining < release:
        return max(0.0, remaining / release)
    return 1.0


def synth_note_samples(
    note: MidiNote,
    duration_seconds: float,
    sample_rate: int,
    waveform: str,
    gain: float,
) -> array.array:
    frame_count = max(1, int(round(duration_seconds * sample_rate)))
    freq = note_frequency(note.note)
    amplitude = max(0.0, min(1.0, note.velocity / 127.0)) * gain
    samples = array.array("f", [0.0]) * frame_count
    phase_step = 2.0 * math.pi * freq / sample_rate
    phase = 0.0
    for index in range(frame_count):
        samples[index] = synth_wave_value(phase, waveform) * amplitude * envelope_value(index, frame_count, sample_rate)
        phase += phase_step
    return samples


def write_float_mono_wav(path: Path, samples: Sequence[float], sample_rate: int) -> None:
    peak = max((abs(value) for value in samples), default=0.0)
    scale = 0.95 / peak if peak > 1.0 else 1.0
    frames = bytearray()
    for value in samples:
        clamped = max(-1.0, min(1.0, value * scale))
        frames += struct.pack("<h", int(round(clamped * 32767)))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(bytes(frames))


def write_synth_note_wav(path: Path, note: MidiNote, duration_seconds: float, args: argparse.Namespace) -> None:
    samples = synth_note_samples(
        note=note,
        duration_seconds=duration_seconds,
        sample_rate=args.synth_sample_rate,
        waveform=args.synth_waveform,
        gain=args.synth_gain,
    )
    write_float_mono_wav(path, samples, args.synth_sample_rate)


def render_midi_synth_wav(path: Path, notes: Sequence[MidiNote], midi: MidiData, args: argparse.Namespace) -> Dict[str, object]:
    sample_rate = args.synth_sample_rate
    if not notes:
        write_float_mono_wav(path, [], sample_rate)
        return {"path": str(path), "notes": 0, "duration_seconds": 0.0}

    ends = []
    for note in notes:
        start = tick_to_seconds(note.tick, midi.ticks_per_quarter, midi.tempos)
        duration = note_slice_seconds(note, midi, args)
        ends.append(start + duration)
    total_seconds = max(ends, default=0.0) + 0.2
    total_frames = max(1, int(round(total_seconds * sample_rate)))
    mix = array.array("f", [0.0]) * total_frames

    for note in notes:
        start_seconds = tick_to_seconds(note.tick, midi.ticks_per_quarter, midi.tempos) + (args.audio_offset_ms / 1000.0)
        if start_seconds < 0:
            start_seconds = 0.0
        start_frame = int(round(start_seconds * sample_rate))
        duration = note_slice_seconds(note, midi, args)
        samples = synth_note_samples(note, duration, sample_rate, args.synth_waveform, args.synth_gain)
        for offset, value in enumerate(samples):
            mix_index = start_frame + offset
            if mix_index >= total_frames:
                break
            mix[mix_index] += value

    write_float_mono_wav(path, mix, sample_rate)
    return {"path": str(path), "notes": len(notes), "duration_seconds": round(total_seconds, 3)}


def note_slice_seconds(note: MidiNote, midi: MidiData, args: argparse.Namespace) -> float:
    track_slice_ms_map = parse_track_ms_map(getattr(args, "track_slice_ms_map", None), "--track-slice-ms-map")
    track_min_slice_ms_map = parse_track_ms_map(getattr(args, "track_min_slice_ms_map", None), "--track-min-slice-ms-map")
    one_shot_tracks = parse_int_set(getattr(args, "one_shot_tracks", None), zero_based=True) or set()
    if note.track in track_slice_ms_map:
        seconds = track_slice_ms_map[note.track] / 1000.0
    elif note.track in one_shot_tracks:
        seconds = max(1, int(getattr(args, "one_shot_slice_ms", args.slice_ms))) / 1000.0
    elif args.slice_mode == "fixed" or note.duration_ticks <= 0:
        seconds = args.slice_ms / 1000.0
    else:
        duration_ticks = bucket_duration_ticks(note.duration_ticks, args.duration_bucket_ticks)
        start = tick_to_seconds(note.tick, midi.ticks_per_quarter, midi.tempos)
        end = tick_to_seconds(note.tick + duration_ticks, midi.ticks_per_quarter, midi.tempos)
        seconds = max(0.0, end - start) + (args.slice_tail_ms / 1000.0)
    min_slice_ms = max(args.min_slice_ms, track_min_slice_ms_map.get(note.track, 0))
    seconds = max(seconds, min_slice_ms / 1000.0)
    if args.max_slice_ms > 0:
        seconds = min(seconds, args.max_slice_ms / 1000.0)
    return seconds


def bucket_duration_ticks(duration_ticks: int, bucket_ticks: int) -> int:
    bucket = max(1, bucket_ticks)
    return max(1, int(round(duration_ticks / bucket) * bucket))


def allocate_keysound_slices(
    args: argparse.Namespace,
    notes: Sequence[MidiNote],
    midi: MidiData,
    output_dir: Path,
) -> Tuple[Dict[str, str], List[str], Optional[str], int, int, Dict[str, Path], Dict[str, object]]:
    if args.sample_map:
        raise SystemExit("--keysound-source/--track-audio-map cannot be combined with --sample-map")

    source_path: Optional[Path] = None
    track_audio_map = discover_track_audio_map(args.auto_track_audio_dir, midi.track_names)
    track_audio_map.update(parse_track_audio_map(args.track_audio_map))
    track_gain_map = parse_track_gain_map(args.track_gain_map)
    keysound_no_reuse_tracks = parse_int_set(args.keysound_no_reuse_tracks, zero_based=True) or set()
    if not args.synth_keysounds and args.keysound_source:
        source_path = Path(args.keysound_source)
        if not source_path.exists():
            raise SystemExit(f"keysound source WAV not found: {source_path}")
        if source_path.suffix.lower() != ".wav":
            raise SystemExit("--keysound-source currently supports PCM WAV only")
    if (args.auto_track_audio_dir or args.track_audio_map) and not track_audio_map and source_path is None:
        known_tracks = ", ".join(
            f"{track}:{name}" for track, name in sorted(midi.track_names.items())
        ) or "no track names found"
        raise SystemExit(
            "no track audio WAVs matched. "
            "For --auto-track-audio-dir, stem WAV filenames must match MIDI/FLP track names. "
            "Either rename the stem WAV files, use --track-audio-map manually, or add --keysound-source as fallback. "
            f"Known tracks: {known_tracks}"
        )
    if track_audio_map:
        missing_files = [str(path) for path in sorted(set(track_audio_map.values()), key=str) if not path.exists()]
        if missing_files:
            raise SystemExit("track audio source WAV not found: " + ", ".join(missing_files))
        non_wav_files = [str(path) for path in sorted(set(track_audio_map.values()), key=str) if path.suffix.lower() != ".wav"]
        if non_wav_files:
            raise SystemExit("--track-audio-map currently supports PCM WAV only: " + ", ".join(non_wav_files))
        missing_tracks = sorted({note.track for note in notes if note.track not in track_audio_map})
        if missing_tracks and source_path is None:
            missing_detail = ", ".join(
                f"{track}:{midi.track_names.get(track, '?')}" for track in missing_tracks
            )
            raise SystemExit(
                "missing --track-audio-map WAV for tracks "
                + missing_detail
                + "; add those tracks or provide --keysound-source as fallback"
            )

    slice_dir = Path(args.keysound_dir) if args.keysound_dir else output_dir / "keysounds"
    wav_defs: Dict[str, str] = {}
    instance_codes: List[str] = [""] * len(notes)
    group_codes: Dict[Tuple[object, ...], str] = {}
    content_codes: Dict[str, str] = {}
    generated_paths: Dict[str, Path] = {}
    deduped_identical = 0
    dropped_silent = 0
    next_code = 1
    offset_seconds = args.audio_offset_ms / 1000.0
    clip_overlap = bool(getattr(args, "clip_overlap_keysounds", False)) and not args.synth_keysounds
    clip_gap_seconds = max(0, int(getattr(args, "clip_overlap_gap_ms", 0) or 0)) / 1000.0
    clip_exclude_tracks = parse_int_set(getattr(args, "clip_overlap_exclude_tracks", None), zero_based=True) or set()
    note_source_paths: List[Optional[Path]] = []
    track_gain_dbs: List[float] = []
    source_identities: List[str] = []
    start_seconds_by_index: List[float] = []
    group_keys: List[Tuple[object, ...]] = []
    clip_caps: List[Optional[float]] = [None] * len(notes)
    source_positions: Dict[str, List[Tuple[float, int]]] = {}

    for index, note in enumerate(notes):
        note_source_path = track_audio_map.get(note.track, source_path)
        track_gain_db = track_gain_map.get(note.track, 0.0)
        source_identity = "synth" if args.synth_keysounds else str(note_source_path)
        start = tick_to_seconds(note.tick, midi.ticks_per_quarter, midi.tempos) + offset_seconds
        group_key = keysound_group_key(note, index, args, keysound_no_reuse_tracks, midi) + (
            source_identity,
            round(track_gain_db, 6),
        )
        note_source_paths.append(note_source_path)
        track_gain_dbs.append(track_gain_db)
        source_identities.append(source_identity)
        start_seconds_by_index.append(start)
        group_keys.append(group_key)
        if clip_overlap and note_source_path is not None and note.track not in clip_exclude_tracks:
            # excluded tracks (e.g. piano) keep their full decay; BMS does not note-off a
            # retriggered keysound, so overlapping decays ring naturally like the real instrument
            source_positions.setdefault(source_identity, []).append((max(0.0, start), index))

    if clip_overlap:
        for positions in source_positions.values():
            next_distinct_start: Optional[float] = None
            for start, index in reversed(sorted(positions)):
                if next_distinct_start is not None and next_distinct_start > start + 1e-9:
                    clip_caps[index] = max(0.001, next_distinct_start - start - clip_gap_seconds)
                if next_distinct_start is None or start < next_distinct_start - 1e-9:
                    next_distinct_start = start

    group_slice_seconds: Dict[Tuple[object, ...], float] = {}
    overlap_events_clipped = 0
    overlap_max_reduce_ms = 0.0
    for index, note in enumerate(notes):
        requested_seconds = note_slice_seconds(note, midi, args)
        effective_seconds = requested_seconds
        clip_cap = clip_caps[index]
        if clip_cap is not None and clip_cap < effective_seconds:
            effective_seconds = clip_cap
            overlap_events_clipped += 1
            overlap_max_reduce_ms = max(overlap_max_reduce_ms, (requested_seconds - effective_seconds) * 1000.0)
        group_key = group_keys[index]
        previous_seconds = group_slice_seconds.get(group_key)
        if previous_seconds is None or effective_seconds < previous_seconds:
            group_slice_seconds[group_key] = effective_seconds

    overlap_summary: Dict[str, object] = {
        "enabled": clip_overlap,
        "events_clipped": overlap_events_clipped,
        "max_reduce_ms": round(overlap_max_reduce_ms, 3),
        "source_count": len(source_positions) if clip_overlap else 0,
        "gap_ms": int(round(clip_gap_seconds * 1000.0)),
    }

    for index, note in enumerate(notes):
        note_source_path = note_source_paths[index]
        track_gain_db = track_gain_dbs[index]
        group_key = group_keys[index]
        existing_code = group_codes.get(group_key)
        if existing_code:
            instance_codes[index] = existing_code
            continue
        if next_code >= 36 * 36:
            raise SystemExit(
                "too many keysound WAV definitions for one BMS namespace; "
                "try --keysound-reuse track-pitch or reduce/filter notes"
            )
        code = base36_code(next_code)
        group_codes[group_key] = code
        instance_codes[index] = code
        next_code += 1
        filename = f"ks_{len(group_codes):06d}.wav"
        relative_name = str((Path(slice_dir.name) / filename)).replace("\\", "/")
        wav_defs[code] = relative_name
        output_wav_path = slice_dir / filename
        start = start_seconds_by_index[index]
        slice_seconds = group_slice_seconds.get(group_key, note_slice_seconds(note, midi, args))
        if args.synth_keysounds:
            write_synth_note_wav(output_wav_path, note, slice_seconds, args)
            peak = wav_file_peak(output_wav_path)
            if args.normalize_keysounds:
                normalize_wav_file(output_wav_path, args.normalize_target_db, args.normalize_min_peak)
            if track_gain_db:
                apply_gain_to_wav_file(output_wav_path, track_gain_db)
        else:
            if note_source_path is None:
                raise SystemExit(
                    f"no audio source for track {note.track}:{midi.track_names.get(note.track, '?')}; "
                    "use --synth-keysounds, --keysound-source, --track-audio-map, or --auto-track-audio-dir"
                )
            fade_in_ms = getattr(args, "keysound_fade_in_ms", 0.0)
            fade_out_ms = getattr(args, "keysound_fade_out_ms", 0.0)
            peak = write_wav_slice(note_source_path, output_wav_path, start, slice_seconds, fade_in_ms, fade_out_ms)
            if peak == 0 and source_path is not None and note_source_path != source_path:
                peak = write_wav_slice(source_path, output_wav_path, start, slice_seconds, fade_in_ms, fade_out_ms)
            if args.normalize_keysounds:
                normalize_wav_file(output_wav_path, args.normalize_target_db, args.normalize_min_peak)
            if track_gain_db:
                apply_gain_to_wav_file(output_wav_path, track_gain_db)

        if args.drop_silent_keysounds and peak < max(1, args.drop_silent_min_peak):
            instance_codes[index] = ""
            group_codes.pop(group_key, None)
            wav_defs.pop(code, None)
            try:
                output_wav_path.unlink()
            except OSError:
                pass
            dropped_silent += 1
            continue

        if args.dedupe_identical_keysounds:
            content_hash = file_sha256(output_wav_path)
            existing_content_code = content_codes.get(content_hash)
            if existing_content_code:
                instance_codes[index] = existing_content_code
                group_codes[group_key] = existing_content_code
                wav_defs.pop(code, None)
                try:
                    output_wav_path.unlink()
                except OSError:
                    pass
                deduped_identical += 1
                continue
            content_codes[content_hash] = code
        generated_paths[code] = output_wav_path

    bgm_code: Optional[str] = None
    if args.bgm:
        bgm_code = base36_code(next_code)
        wav_defs[bgm_code] = Path(args.bgm).name
    return wav_defs, instance_codes, bgm_code, deduped_identical, dropped_silent, generated_paths, overlap_summary


def keysound_group_key(
    note: MidiNote,
    index: int,
    args: argparse.Namespace,
    no_reuse_tracks: set[int],
    midi: MidiData,
) -> Tuple[int, ...]:
    if note.track in no_reuse_tracks:
        return (index,)
    reuse_mode = args.keysound_reuse
    duration_ticks = bucket_duration_ticks(note.duration_ticks, args.duration_bucket_ticks)
    if reuse_mode == "none":
        return (index,)
    key: Tuple[int, ...]
    if reuse_mode == "pitch":
        key = (note.note,)
    elif reuse_mode == "pitch-duration":
        key = (note.note, duration_ticks)
    elif reuse_mode == "track-pitch":
        key = (note.track, note.note)
    elif reuse_mode == "track-pitch-duration":
        key = (note.track, note.note, duration_ticks)
    elif reuse_mode == "channel-pitch":
        key = (note.channel, note.note)
    else:
        raise SystemExit(f"unknown --keysound-reuse mode: {reuse_mode}")
    return add_keysound_time_bucket(key, note, args, midi)


def add_keysound_time_bucket(
    key: Tuple[int, ...],
    note: MidiNote,
    args: argparse.Namespace,
    midi: MidiData,
) -> Tuple[int, ...]:
    tracks = parse_int_set(getattr(args, "keysound_time_bucket_tracks", None), zero_based=True) or set()
    if note.track not in tracks:
        return key
    measures = int(getattr(args, "keysound_time_bucket_measures", 0) or 0)
    if measures <= 0:
        return key
    numerator = midi.time_signatures[0].numerator if midi.time_signatures else 4
    denominator = midi.time_signatures[0].denominator if midi.time_signatures else 4
    measure, _slot = tick_to_measure_slot(note.tick, midi.ticks_per_quarter, numerator, denominator, 192)
    return key + (measure // measures,)


def keysound_relative_name(path: Path) -> str:
    return str((Path(path.parent.name) / path.name)).replace("\\", "/")


def stack_reduction_db(stack_count: int, args: argparse.Namespace) -> float:
    threshold = max(1, int(args.anti_stack_threshold))
    if stack_count <= threshold:
        return 0.0
    reduce_db = 20.0 * math.log10(stack_count / threshold)
    reduce_db = min(max(0.0, args.anti_stack_max_reduce_db), reduce_db)
    step = max(0.0, float(args.anti_stack_step_db))
    if step > 0:
        reduce_db = math.ceil(reduce_db / step) * step
    return round(reduce_db, 6)


def apply_anti_stack_keysound_gain(
    events: List[PositionedEvent],
    wav_defs: Dict[str, str],
    generated_paths: Dict[str, Path],
    args: argparse.Namespace,
) -> Dict[str, object]:
    summary: Dict[str, object] = {
        "enabled": bool(args.anti_stack_keysounds),
        "max_simultaneous": 0,
        "events_adjusted": 0,
        "variants_written": 0,
        "max_reduce_db": 0.0,
    }
    if not args.anti_stack_keysounds or not generated_paths:
        return summary

    by_position: Dict[Tuple[int, int], List[int]] = {}
    for index, event in enumerate(events):
        if event.channel == "08" or event.code not in generated_paths:
            continue
        by_position.setdefault((event.measure, event.slot), []).append(index)

    if by_position:
        summary["max_simultaneous"] = max(len(indexes) for indexes in by_position.values())

    variant_codes: Dict[Tuple[str, float], str] = {}
    for indexes in by_position.values():
        reduce_db = stack_reduction_db(len(indexes), args)
        if reduce_db <= 0:
            continue
        summary["max_reduce_db"] = max(float(summary["max_reduce_db"]), reduce_db)
        for event_index in indexes:
            event = events[event_index]
            original_code = event.code
            original_path = generated_paths.get(original_code)
            if original_path is None:
                continue
            variant_key = (original_code, reduce_db)
            variant_code = variant_codes.get(variant_key)
            if variant_code is None:
                variant_code = next_free_wav_code(wav_defs)
                suffix = int(round(reduce_db * 10))
                variant_path = original_path.with_name(f"{original_path.stem}_stack{suffix:03d}{original_path.suffix}")
                shutil.copyfile(original_path, variant_path)
                apply_gain_to_wav_file(variant_path, -reduce_db)
                wav_defs[variant_code] = keysound_relative_name(variant_path)
                generated_paths[variant_code] = variant_path
                variant_codes[variant_key] = variant_code
                summary["variants_written"] = int(summary["variants_written"]) + 1
            events[event_index] = PositionedEvent(
                event.measure,
                event.slot,
                event.channel,
                variant_code,
                event.layer,
            )
            summary["events_adjusted"] = int(summary["events_adjusted"]) + 1
    return summary


def measure_boundary_times(
    midi: MidiData, numerator: int, denominator: int, end_seconds: float
) -> List[float]:
    """Absolute start time (seconds) of measure 0,1,2,... covering up to end_seconds."""
    ticks_per_measure = midi.ticks_per_quarter * numerator * (4.0 / denominator)
    times = [tick_to_seconds(0, midi.ticks_per_quarter, midi.tempos)]
    measure = 1
    while times[-1] < end_seconds:
        tick = int(round(measure * ticks_per_measure))
        times.append(tick_to_seconds(tick, midi.ticks_per_quarter, midi.tempos))
        measure += 1
        if measure > 100000:  # safety
            break
    return times


def allocate_bgm_stem_wavs(
    args: argparse.Namespace,
    midi: MidiData,
    output_dir: Path,
    stem_tracks: set,
    used_codes: set,
    numerator: int,
    denominator: int,
    extra_sources: Optional[Sequence[Path]] = None,
) -> Tuple[Dict[str, str], List[Tuple[str, int, int]]]:
    """Place instrument stems as continuous BGM, split into measure-aligned chunks.

    Reverb/sustain tracks lose their tails when sliced per note; a mixer stem already
    contains exactly that instrument across the song. We slice it ONLY at measure
    boundaries into chunks of at most --bgm-stem-max-seconds (BMS IR keysound length
    limit), then place each chunk at its own measure. Measure boundaries land on slot 0
    with no rounding, so the chunks play back gaplessly and the audio stays continuous.

    `extra_sources` are raw stem WAVs not tied to any MIDI track (e.g. an audio-clip
    instrument that exported no notes); they are chunked and placed the same way so
    nothing in the original mix goes missing. Returns wav_defs plus placements
    as (code, measure, slot=0).
    """
    extra_sources = list(extra_sources or [])
    if not stem_tracks and not extra_sources:
        return {}, []
    track_audio_map = discover_track_audio_map(args.auto_track_audio_dir, midi.track_names)
    track_audio_map.update(parse_track_audio_map(args.track_audio_map))
    track_gain_map = parse_track_gain_map(args.track_gain_map)
    slice_dir = Path(args.keysound_dir) if args.keysound_dir else output_dir / "keysounds"
    slice_dir.mkdir(parents=True, exist_ok=True)
    offset_seconds = args.audio_offset_ms / 1000.0
    max_seconds_cut = args.max_seconds if args.max_seconds and args.max_seconds > 0 else 0.0
    max_chunk = max(1.0, float(getattr(args, "bgm_stem_max_seconds", 28.0)))
    # tiny declick fades at each cut; mid-stream chunks also fade in their start
    seam_fade_ms = max(0.0, float(getattr(args, "bgm_stem_seam_fade_ms", 3.0)))

    used_nums = {base36_number(code) for code in used_codes}
    next_num = 1

    def next_free_num() -> int:
        nonlocal next_num
        while next_num in used_nums or next_num <= 0:
            next_num += 1
        if next_num >= 36 * 36:
            raise SystemExit("too many WAV codes for --bgm-stem-tracks; raise --bgm-stem-max-seconds or reduce tracks")
        used_nums.add(next_num)
        return next_num

    # unified work list: (label, source_path, gain_db)
    sources: List[Tuple[str, Path, float]] = []
    for track in sorted(stem_tracks):
        source = track_audio_map.get(track)
        if source is None:
            raise SystemExit(
                f"--bgm-stem-tracks track {track}:{midi.track_names.get(track, '?')} has no stem WAV; "
                "add it via --track-audio-map or --auto-track-audio-dir"
            )
        sources.append((f"t{track:02d}", source, track_gain_map.get(track, 0.0)))
    for i, source in enumerate(extra_sources):
        sources.append((f"x{i:02d}", Path(source), 0.0))

    wav_defs: Dict[str, str] = {}
    placements: List[Tuple[str, int, int]] = []
    seen_sources: Dict[str, str] = {}
    chunk_seq = 0
    for label, source, gain_db in sources:
        if not source.exists():
            raise SystemExit(f"bgm stem WAV not found: {source}")
        # multiple tracks can route to the same mixer stem; place each stem WAV once
        # so a shared insert is not summed twice (which would double its volume)
        source_key = str(source.resolve()).lower()
        if source_key in seen_sources:
            continue
        seen_sources[source_key] = "1"
        with wave.open(str(source), "rb") as src:
            full_seconds = src.getnframes() / float(src.getframerate() or 1)
        end_seconds = full_seconds - offset_seconds
        if max_seconds_cut > 0:
            end_seconds = min(end_seconds, max_seconds_cut)
        if end_seconds <= 0:
            continue
        boundaries = measure_boundary_times(midi, numerator, denominator, end_seconds)
        # greedily group consecutive measures so each chunk <= max_chunk seconds
        seg_start = 0
        span_count = len(boundaries) - 1  # number of full measure spans
        while seg_start < span_count:
            seg_end = seg_start + 1
            while seg_end < span_count and (boundaries[seg_end + 1] - boundaries[seg_start]) <= max_chunk:
                seg_end += 1
            start_t = boundaries[seg_start]
            stop_t = min(boundaries[seg_end], end_seconds)
            length = stop_t - start_t
            if length <= 0:
                break
            code = base36_code(next_free_num())
            chunk_seq += 1
            filename = f"stem_{label}_{chunk_seq:03d}.wav"
            output_wav_path = slice_dir / filename
            fade_in = seam_fade_ms if seg_start > 0 else 0.0
            write_wav_slice(source, output_wav_path, start_t + offset_seconds, length, fade_in, seam_fade_ms)
            if gain_db:
                apply_gain_to_wav_file(output_wav_path, gain_db)
            relative_name = str((Path(slice_dir.name) / filename)).replace("\\", "/")
            wav_defs[code] = relative_name
            placements.append((code, seg_start, 0))
            seg_start = seg_end
    return wav_defs, placements


def build_bms(midi: MidiData, input_path: Path, output_path: Path, args: argparse.Namespace) -> Dict[str, object]:
    if args.background_layers < 1:
        raise SystemExit("--background-layers must be at least 1")
    if args.duration_bucket_ticks < 1:
        raise SystemExit("--duration-bucket-ticks must be at least 1")
    if args.piano_bms:
        validate_piano_args(args)
    lane_channels = parse_lane_channels(args)
    numerator, denominator = initial_time_signature(midi)
    filtered_notes = filter_notes(
        midi.notes,
        args,
        use_track_filter=not args.background_non_player_to_bgm,
        use_channel_filter=not args.background_non_player_to_bgm,
        use_piano_soft_filter=not args.piano_bms,
    )
    if args.piano_bms:
        filtered_notes = apply_piano_pedal_durations(filtered_notes, midi)
        filtered_notes = filter_soft_piano_notes(filtered_notes, args)
    candidate_notes, time_cut_notes, time_clipped_notes = trim_notes_to_max_seconds(filtered_notes, midi, args)
    bgm_stem_tracks = parse_int_set(getattr(args, "bgm_stem_tracks", None), zero_based=True) or set()
    extra_bgm_stems: List[Path] = []
    if getattr(args, "extra_bgm_stems", None):
        for raw in str(args.extra_bgm_stems).replace(";", ",").split(","):
            entry = raw.strip().strip('"')
            if not entry:
                continue
            path = Path(entry)
            if not path.exists():
                raise SystemExit(f"--extra-bgm-stems WAV not found: {path}")
            if path.suffix.lower() != ".wav":
                raise SystemExit(f"--extra-bgm-stems supports PCM WAV only: {path}")
            extra_bgm_stems.append(path)
    bgm_stem_note_count = 0
    if bgm_stem_tracks:
        kept_notes = []
        for note in candidate_notes:
            if note.track in bgm_stem_tracks:
                bgm_stem_note_count += 1
            else:
                kept_notes.append(note)
        candidate_notes = kept_notes
    if args.all_notes_to_bgm:
        player_notes = []
        background_notes = list(candidate_notes)
    elif args.background_non_player_to_bgm:
        player_notes, background_notes = split_player_background_notes(candidate_notes, args)
    else:
        player_notes = candidate_notes
        background_notes = []
    keysound_notes_merged = 0
    keysound_merge_max_group = 0
    if background_notes:
        background_notes, keysound_notes_merged, keysound_merge_max_group = merge_same_time_keysound_notes(
            background_notes,
            midi,
            args,
        )
    lane_map, mapping_mode = build_lane_mapper(player_notes, len(lane_channels), args)
    if not player_notes and not background_notes and not bgm_stem_tracks and not args.allow_empty:
        raise SystemExit(
            "no MIDI note-on events survived. For FL Studio, export a MIDI file with actual piano-roll notes; "
            "the current input would only create a BGM-only BMS."
        )
    output_notes = player_notes + background_notes
    keysounds_deduped = 0
    silent_keysounds_dropped = 0
    unused_keysound_files_removed = 0
    generated_paths: Dict[str, Path] = {}
    overlap_clip_summary: Dict[str, object] = {"enabled": False, "events_clipped": 0, "max_reduce_ms": 0.0, "source_count": 0, "gap_ms": 0}
    uses_instance_codes = bool(
        args.piano_bms
        or args.keysound_source
        or args.track_audio_map
        or args.auto_track_audio_dir
        or args.synth_keysounds
    )
    if args.piano_bms:
        wav_defs, instance_codes, bgm_code = allocate_piano_sample_wavs(args, output_notes, midi)
    elif args.keysound_source or args.track_audio_map or args.auto_track_audio_dir or args.synth_keysounds:
        (
            wav_defs,
            instance_codes,
            bgm_code,
            keysounds_deduped,
            silent_keysounds_dropped,
            generated_paths,
            overlap_clip_summary,
        ) = allocate_keysound_slices(
            args,
            output_notes,
            midi,
            output_path.parent,
        )
        note_codes: Dict[int, str] = {}
    else:
        sample_map = read_sample_map(Path(args.sample_map)) if args.sample_map else {}
        wav_defs, note_codes, _default_note_code, bgm_code = allocate_wavs(
            args,
            output_notes,
            sample_map,
            output_path.parent,
        )
        instance_codes = []

    events: List[PositionedEvent] = []
    collisions = 0
    background_collisions = 0
    occupied: set[Tuple[int, int, str, int]] = set()
    occupied_bgm: set[Tuple[int, int, int]] = set()
    occupied_bgm_codes: set[Tuple[int, int, str]] = set()
    skipped_notes = 0
    silent_player_keysound_events_skipped = 0
    silent_background_keysound_events_skipped = 0
    same_time_bgm_codes_deduped = 0

    for note_index, note in enumerate(player_notes):
        lane_index = lane_map.get(note.note)
        if lane_index is None:
            skipped_notes += 1
            continue
        measure, slot = tick_to_measure_slot(
            note.tick,
            midi.ticks_per_quarter,
            numerator,
            denominator,
            args.resolution,
        )
        channel = lane_channels[lane_index]
        key = (measure, slot, channel, 0)
        if key in occupied:
            collisions += 1
            if args.keep_collisions:
                continue
            continue
        occupied.add(key)
        code = instance_codes[note_index] if uses_instance_codes else note_codes[note.note]
        if not code:
            silent_player_keysound_events_skipped += 1
            continue
        events.append(PositionedEvent(measure, slot, channel, code))

    bpm_events = build_bpm_events(midi, args.resolution, numerator, denominator)
    events.extend(bpm_events)
    if bgm_code:
        events.append(PositionedEvent(0, 0, "01", bgm_code, 0))
        occupied_bgm.add((0, 0, 0))

    for background_index, note in enumerate(background_notes):
        measure, slot = tick_to_measure_slot(
            note.tick,
            midi.ticks_per_quarter,
            numerator,
            denominator,
            args.resolution,
        )
        code_index = len(player_notes) + background_index
        code = instance_codes[code_index] if uses_instance_codes else note_codes[note.note]
        if not code:
            silent_background_keysound_events_skipped += 1
            continue
        same_time_code_key = (measure, slot, code)
        if args.dedupe_same_time_bgm_code and same_time_code_key in occupied_bgm_codes:
            same_time_bgm_codes_deduped += 1
            continue
        occupied_bgm_codes.add(same_time_code_key)
        layer = first_free_bgm_layer(occupied_bgm, measure, slot, args.background_layers)
        if layer is None:
            background_collisions += 1
            continue
        occupied_bgm.add((measure, slot, layer))
        events.append(PositionedEvent(measure, slot, "01", code, layer))

    bgm_stem_wav_defs: Dict[str, str] = {}
    bgm_stem_placements: List[Tuple[str, int, int]] = []
    if bgm_stem_tracks or extra_bgm_stems:
        bgm_stem_wav_defs, bgm_stem_placements = allocate_bgm_stem_wavs(
            args, midi, output_path.parent, bgm_stem_tracks, set(wav_defs), numerator, denominator,
            extra_sources=extra_bgm_stems,
        )
        wav_defs.update(bgm_stem_wav_defs)
        stem_layer_cap = args.background_layers + len(bgm_stem_tracks) + len(extra_bgm_stems) + 4
        for code, measure, slot in bgm_stem_placements:
            layer = first_free_bgm_layer(occupied_bgm, measure, slot, stem_layer_cap)
            if layer is None:
                continue
            occupied_bgm.add((measure, slot, layer))
            events.append(PositionedEvent(measure, slot, "01", code, layer))

    anti_stack_summary = apply_anti_stack_keysound_gain(events, wav_defs, generated_paths, args)

    if generated_paths:
        used_wav_codes = {event.code for event in events if event.channel != "08"}
        unused_codes = set(generated_paths) - used_wav_codes
        for code in unused_codes:
            try:
                generated_paths[code].unlink()
                unused_keysound_files_removed += 1
            except OSError:
                pass
        wav_defs = {
            code: filename
            for code, filename in wav_defs.items()
            if code in used_wav_codes or code not in generated_paths
        }

    grouped: Dict[Tuple[int, str, int], Dict[int, str]] = {}
    for event in events:
        grouped.setdefault((event.measure, event.channel, event.layer), {})[event.slot] = event.code

    lines = render_bms(
        input_path=input_path,
        args=args,
        wav_defs=wav_defs,
        bpm_defs=tempo_defs(midi),
        grouped=grouped,
        resolution=args.resolution,
        numerator=numerator,
        denominator=denominator,
        midi=midi,
        filtered_note_count=len(player_notes),
        collisions=collisions,
        background_note_count=len(background_notes),
        background_collisions=background_collisions,
        skipped_notes=skipped_notes,
        mapping_mode=mapping_mode,
        lane_channels=lane_channels,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {
        "input": str(input_path),
        "output": str(output_path),
        "source_format": midi.source_format,
        "notes_in_midi": len(midi.notes),
        "notes_used": len(player_notes) - skipped_notes - collisions - silent_player_keysound_events_skipped,
        "notes_filtered": len(midi.notes) - len(filtered_notes),
        "notes_cut_by_max_seconds": time_cut_notes,
        "notes_clipped_by_max_seconds": time_clipped_notes,
        "max_seconds": args.max_seconds,
        "collisions": collisions,
        "background_notes": len(background_notes),
        "background_notes_used": len(background_notes) - background_collisions - same_time_bgm_codes_deduped - silent_background_keysound_events_skipped,
        "background_collisions": background_collisions,
        "same_time_bgm_codes_deduped": same_time_bgm_codes_deduped,
        "silent_keysounds_dropped": silent_keysounds_dropped,
        "silent_keysound_events_skipped": silent_player_keysound_events_skipped + silent_background_keysound_events_skipped,
        "background_layers": args.background_layers if (args.background_non_player_to_bgm or args.all_notes_to_bgm) else 0,
        "all_notes_to_bgm": args.all_notes_to_bgm,
        "bgm_stem_tracks": args.bgm_stem_tracks,
        "bgm_stem_notes_diverted": bgm_stem_note_count,
        "extra_bgm_stems": [str(p) for p in extra_bgm_stems] or None,
        "bgm_stem_wavs_placed": len(bgm_stem_placements),
        "bgm_stem_max_seconds": args.bgm_stem_max_seconds if bgm_stem_tracks else None,
        "keysound_fade_in_ms": args.keysound_fade_in_ms,
        "keysound_fade_out_ms": args.keysound_fade_out_ms,
        "resolution": args.resolution,
        "mapping": mapping_mode,
        "lane_channels": lane_channels,
        "time_signature": f"{numerator}/{denominator}",
        "tempo_events": len(midi.tempos),
        "time_signature_events": len(midi.time_signatures),
        "keysound_source": args.keysound_source,
        "track_audio_map": args.track_audio_map,
        "auto_track_audio_dir": args.auto_track_audio_dir,
        "piano_bms": args.piano_bms,
        "piano_low_note": args.piano_low_note if args.piano_bms else None,
        "piano_medium_ms": args.piano_medium_ms if args.piano_bms else None,
        "piano_long_ms": args.piano_long_ms if args.piano_bms else None,
        "piano_min_velocity": args.piano_min_velocity if args.piano_bms else None,
        "piano_min_channel_volume": args.piano_min_channel_volume if args.piano_bms else None,
        "synth_keysounds": args.synth_keysounds,
        "merge_same_time_keysounds": args.merge_same_time_keysounds,
        "keysound_notes_merged": keysound_notes_merged,
        "keysound_merge_max_group": keysound_merge_max_group,
        "keysound_no_reuse_tracks": args.keysound_no_reuse_tracks,
        "normalize_keysounds": args.normalize_keysounds,
        "normalize_target_db": args.normalize_target_db if args.normalize_keysounds else None,
        "track_gain_map": args.track_gain_map,
        "one_shot_tracks": args.one_shot_tracks,
        "one_shot_slice_ms": args.one_shot_slice_ms if args.one_shot_tracks else None,
        "track_slice_ms_map": args.track_slice_ms_map,
        "track_min_slice_ms_map": args.track_min_slice_ms_map,
        "keysound_time_bucket_tracks": args.keysound_time_bucket_tracks,
        "keysound_time_bucket_measures": args.keysound_time_bucket_measures if args.keysound_time_bucket_tracks else None,
        "clip_overlap_keysounds": args.clip_overlap_keysounds,
        "clip_overlap_gap_ms": args.clip_overlap_gap_ms if args.clip_overlap_keysounds else None,
        "clip_overlap_exclude_tracks": args.clip_overlap_exclude_tracks if args.clip_overlap_keysounds else None,
        "overlap_clip_summary": overlap_clip_summary,
        "dedupe_identical_keysounds": args.dedupe_identical_keysounds,
        "keysounds_deduped": keysounds_deduped,
        "drop_silent_keysounds": args.drop_silent_keysounds,
        "drop_silent_min_peak": args.drop_silent_min_peak if args.drop_silent_keysounds else None,
        "anti_stack_keysounds": args.anti_stack_keysounds,
        "anti_stack_threshold": args.anti_stack_threshold if args.anti_stack_keysounds else None,
        "anti_stack_max_reduce_db": args.anti_stack_max_reduce_db if args.anti_stack_keysounds else None,
        "anti_stack_summary": anti_stack_summary,
        "unused_keysound_files_removed": unused_keysound_files_removed,
        "keysounds_written": len(wav_defs) - (1 if bgm_code and bgm_code in wav_defs else 0),
        "track_names": midi.track_names,
        "warnings": build_warnings(midi, collisions, background_collisions, args),
    }


def initial_time_signature(midi: MidiData) -> Tuple[int, int]:
    event = midi.time_signatures[0] if midi.time_signatures else TimeSignatureEvent(0, 4, 4)
    return event.numerator, event.denominator


def tempo_defs(midi: MidiData) -> Dict[str, float]:
    defs: Dict[str, float] = {}
    next_code = 1
    for tempo in midi.tempos:
        if tempo.tick == 0:
            continue
        code = base36_code(next_code)
        next_code += 1
        defs[code] = tempo.bpm
    return defs


def build_bpm_events(
    midi: MidiData,
    resolution: int,
    numerator: int,
    denominator: int,
) -> List[PositionedEvent]:
    events: List[PositionedEvent] = []
    next_code = 1
    for tempo in midi.tempos:
        if tempo.tick == 0:
            continue
        code = base36_code(next_code)
        next_code += 1
        measure, slot = tick_to_measure_slot(
            tempo.tick,
            midi.ticks_per_quarter,
            numerator,
            denominator,
            resolution,
        )
        events.append(PositionedEvent(measure, slot, "08", code))
    return events


def first_free_bgm_layer(
    occupied_bgm: set[Tuple[int, int, int]],
    measure: int,
    slot: int,
    layer_count: int,
) -> Optional[int]:
    for layer in range(max(1, layer_count)):
        if (measure, slot, layer) not in occupied_bgm:
            return layer
    return None


def render_bms(
    input_path: Path,
    args: argparse.Namespace,
    wav_defs: Dict[str, str],
    bpm_defs: Dict[str, float],
    grouped: Dict[Tuple[int, str, int], Dict[int, str]],
    resolution: int,
    numerator: int,
    denominator: int,
    midi: MidiData,
    filtered_note_count: int,
    collisions: int,
    background_note_count: int,
    background_collisions: int,
    skipped_notes: int,
    mapping_mode: str,
    lane_channels: Sequence[str],
) -> List[str]:
    initial_bpm = midi.tempos[0].bpm if midi.tempos else 120.0
    title = args.title or midi.title or input_path.stem
    artist = args.artist or midi.artist or ""
    genre = args.genre or midi.genre or "BMS"
    lines = [
        "#PLAYER 1",
        f"#GENRE {genre}",
        f"#TITLE {title}",
        f"#ARTIST {artist}",
        f"#BPM {format_bpm(initial_bpm)}",
        f"#PLAYLEVEL {args.playlevel}",
        "#RANK 3",
        f"#TOTAL {args.total}",
        "",
        "*---------------------- HEADER FIELD",
        f"* input: {input_path.name}",
        f"* generated_by: daw2bms",
        f"* mapping: {mapping_mode}",
        f"* lanes: {','.join(lane_channels)}",
        f"* source_notes_after_filter: {filtered_note_count}",
        f"* dropped_same_slot_collisions: {collisions}",
        f"* bgm_keysound_notes: {background_note_count}",
        f"* dropped_bgm_layer_overflow: {background_collisions}",
        f"* bgm_layers: {args.background_layers if (args.background_non_player_to_bgm or args.all_notes_to_bgm) else 0}",
        f"* skipped_unmapped_notes: {skipped_notes}",
        "",
    ]
    for code, filename in sorted(wav_defs.items()):
        lines.append(f"#WAV{code} {filename}")
    for code, bpm in sorted(bpm_defs.items()):
        lines.append(f"#BPM{code} {format_bpm(bpm)}")
    lines.append("")
    lines.append("*---------------------- MAIN DATA FIELD")

    if (numerator, denominator) != (4, 4):
        max_measure = max((measure for measure, _channel, _layer in grouped), default=0)
        measure_length = numerator / denominator
        for measure in range(max_measure + 1):
            lines.append(f"#{measure:03d}02:{measure_length:.8g}")

    for (measure, channel, _layer), slots in sorted(grouped.items()):
        data = ["00"] * resolution
        for slot, code in slots.items():
            if 0 <= slot < resolution:
                data[slot] = code
        lines.append(f"#{measure:03d}{channel}:{''.join(data)}")
    return lines


def build_warnings(
    midi: MidiData,
    collisions: int,
    background_collisions: int,
    args: argparse.Namespace,
) -> List[str]:
    warnings = []
    if midi.source_format == "flp":
        warnings.append("FLP parsing extracts note timing only; plugin/instrument rendering still needs an audio source")
    if len(midi.time_signatures) > 1:
        warnings.append("time signature changes were detected; only the first signature is used")
    if collisions:
        warnings.append("some notes landed on the same BMS lane/slot and were dropped")
    if background_collisions:
        warnings.append("some BGM keysound notes exceeded available BGM layers and were dropped")
    if args.all_notes_to_bgm:
        warnings.append("all included notes were emitted on repeated BGM #xxx01 layers")
    elif args.background_non_player_to_bgm:
        warnings.append("non-player notes were emitted on repeated BGM #xxx01 layers")
    if args.max_seconds > 0:
        warnings.append(f"notes after {args.max_seconds:g} seconds were cut from the chart")
    if args.mapping != "lane-notes" and not args.lane_notes:
        warnings.append("pitch auto-mapping is a draft; use --lane-notes for prepared lane MIDI")
    if args.keysound_source:
        warnings.append("keysound slicing cuts the source WAV by note time; full-mix audio will not isolate instruments")
    if args.track_audio_map:
        warnings.append("track audio map slices each MIDI track from its mapped stem WAV")
    if args.auto_track_audio_dir:
        warnings.append("auto track audio map matched MIDI/FLP track names to WAV filenames")
    if args.synth_keysounds:
        warnings.append("keysounds were synthesized from MIDI note data using the built-in draft synth")
    if args.piano_bms:
        warnings.append("piano BMS mode used fixed #WAV01..#WAV7C sample names for 88 keys and s/m/l durations")
    if args.merge_same_time_keysounds:
        warnings.append("same-time notes from the same audio source were merged into single keysound slices")
    if args.keysound_no_reuse_tracks:
        warnings.append("selected tracks bypassed keysound reuse and were sliced per occurrence")
    if args.dedupe_same_time_bgm_code:
        warnings.append("duplicate same-time BGM WAV-code events were deduplicated")
    generated_keysound_mode = bool(args.keysound_source or args.track_audio_map or args.auto_track_audio_dir or args.synth_keysounds)
    if generated_keysound_mode and args.drop_silent_keysounds:
        warnings.append("silent generated keysounds were dropped from WAV definitions and BMS events")
    if args.normalize_keysounds:
        warnings.append(f"keysound WAVs were peak-normalized to {args.normalize_target_db:g} dB")
    if args.track_gain_map:
        warnings.append("track-specific keysound gain was applied after normalization")
    if args.one_shot_tracks:
        warnings.append("selected tracks used fixed one-shot slice lengths")
    if args.track_slice_ms_map or args.track_min_slice_ms_map:
        warnings.append("track-specific keysound slice lengths were applied")
    if args.keysound_time_bucket_tracks:
        warnings.append("selected tracks split keysound reuse into time buckets for time-varying effects")
    if args.clip_overlap_keysounds:
        warnings.append("stem keysound lengths were clipped before the next event from the same audio source")
    if getattr(args, "bgm_stem_tracks", None):
        warnings.append("selected tracks were placed as whole continuous BGM stems instead of per-note keysounds")
    if args.anti_stack_keysounds:
        warnings.append("stack-heavy positions generated quieter keysound variants to reduce summed peaks")
    if generated_keysound_mode and args.dedupe_identical_keysounds:
        warnings.append("identical generated keysound WAVs were deduplicated")
    if not args.bgm:
        warnings.append("no BGM audio was attached; add --bgm song.wav for playable preview")
    return warnings


def format_bpm(value: float) -> str:
    rounded = round(value, 6)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.6f}".rstrip("0").rstrip(".")


def write_sample_midi(path: Path) -> None:
    ticks = 480
    header = b"MThd" + struct.pack(">IHHH", 6, 1, 1, ticks)
    events = bytearray()
    events += b"\x00\xFF\x03\x0Cdaw2bms demo"
    events += b"\x00\xFF\x51\x03\x07\xA1\x20"  # 120 BPM
    events += b"\x00\xFF\x58\x04\x04\x02\x18\x08"
    lane_notes = [60, 62, 64, 65, 67, 69, 71]
    for index, note in enumerate(lane_notes * 2):
        delta = 0 if index == 0 else 240
        events += write_varlen(delta)
        events += bytes([0x90, note, 96])
        events += write_varlen(120)
        events += bytes([0x80, note, 0])
    events += b"\x00\xFF\x2F\x00"
    track = b"MTrk" + struct.pack(">I", len(events)) + bytes(events)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + track)


def write_sample_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 44100
    seconds = 240
    frames = bytearray()
    for frame in range(sample_rate * seconds):
        t = frame / sample_rate
        envelope = 0.25
        sample = int(math.sin(2 * math.pi * 440.0 * t) * 32767 * envelope)
        frames += struct.pack("<h", sample)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(bytes(frames))


def write_varlen(value: int) -> bytes:
    buffer = value & 0x7F
    value >>= 7
    result = [buffer]
    while value:
        buffer = (value & 0x7F) | 0x80
        result.insert(0, buffer)
        value >>= 7
    return bytes(result)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert exported DAW MIDI or best-effort FL Studio FLP into a BMS draft chart.",
    )
    parser.add_argument("input", nargs="?", help="input .mid/.midi/.flp file")
    parser.add_argument("-o", "--output", help="output .bms path")
    parser.add_argument("--title")
    parser.add_argument("--artist")
    parser.add_argument("--genre")
    parser.add_argument("--playlevel", default="1")
    parser.add_argument("--total", default="160")
    parser.add_argument("--keys", type=int, default=7, help="5, 7, 8, or 14")
    parser.add_argument("--lane-channels", help="custom BMS channel list, e.g. 11,12,13,14,15,18,19")
    parser.add_argument("--lane-notes", help="exact MIDI notes for lanes, e.g. 60,62,64,65,67,69,71")
    parser.add_argument("--mapping", choices=["pitch-order", "pitch-mod"], default="pitch-order")
    parser.add_argument("--resolution", type=int, default=192, help="BMS slots per measure")
    parser.add_argument("--midi-channels", help="1-based MIDI channels to include, comma-separated")
    parser.add_argument("--tracks", help="MIDI/FLP track numbers from the track check output to include, comma-separated")
    parser.add_argument("--player-tracks", help="MIDI/FLP track numbers from the track check output to keep on player lanes")
    parser.add_argument("--player-midi-channels", help="1-based MIDI channels to keep on player lanes")
    parser.add_argument("--background-non-player-to-bgm", action="store_true", help="send non-player notes to layered BGM key channels")
    parser.add_argument("--all-notes-to-bgm", action="store_true", help="send every included note to layered BGM key channels; no player pattern")
    parser.add_argument("--background-layers", type=int, default=15, help="BGM key layers available at the same timestamp")
    parser.add_argument("--min-note", type=int)
    parser.add_argument("--max-note", type=int)
    parser.add_argument("--bgm", help="rendered song audio filename to put on BGM channel 01")
    parser.add_argument("--keysound-source", help="PCM WAV to slice into one keysound per MIDI note")
    parser.add_argument(
        "--track-audio-map",
        help='track-specific PCM WAV map for keysound slicing, e.g. "10=Bass.wav,12=Lead.wav"',
    )
    parser.add_argument(
        "--auto-track-audio-dir",
        help="directory of stem WAV files; track names from MIDI/FLP are matched to WAV filenames automatically",
    )
    parser.add_argument(
        "--bgm-stem-tracks",
        help="MIDI/FLP track numbers placed as continuous BGM stems instead of per-note slices; "
        "best for reverb/sustain instruments (pads, strings, piano) that chop badly when sliced",
    )
    parser.add_argument(
        "--extra-bgm-stems",
        help="extra stem WAV paths placed as continuous BGM, NOT tied to any MIDI track "
        "(for audio-clip instruments / mixer inserts that exported no notes), comma-separated. "
        'e.g. "Insert 5.wav,FX.wav"',
    )
    parser.add_argument(
        "--bgm-stem-max-seconds",
        type=float,
        default=28.0,
        help="max length of each --bgm-stem-tracks chunk; stems are split at measure boundaries to stay "
        "under the BMS IR keysound length limit (typically 30s)",
    )
    parser.add_argument(
        "--bgm-stem-seam-fade-ms",
        type=float,
        default=3.0,
        help="declick fade (ms) at each --bgm-stem-tracks chunk boundary",
    )
    parser.add_argument(
        "--keysound-fade-in-ms",
        type=float,
        default=2.0,
        help="declick fade-in (ms) applied to the start of each sliced stem keysound",
    )
    parser.add_argument(
        "--keysound-fade-out-ms",
        type=float,
        default=8.0,
        help="declick fade-out (ms) applied to the end of each sliced stem keysound",
    )
    parser.add_argument("--synth-keysounds", action="store_true", help="generate keysound WAVs from MIDI note/duration data")
    parser.add_argument(
        "--piano-bms",
        "--piano-sample-wavs",
        dest="piano_bms",
        action="store_true",
        help="use fixed 88-key piano sample names s_000.wav..l_087.wav mapped to #WAV01..#WAV7C",
    )
    parser.add_argument("--piano-low-note", type=int, default=PIANO_LOW_NOTE, help="MIDI note mapped to piano sample 000")
    parser.add_argument("--piano-medium-ms", type=float, default=200.0, help="minimum duration for m_ piano samples")
    parser.add_argument("--piano-long-ms", type=float, default=800.0, help="minimum duration for l_ piano samples")
    parser.add_argument("--piano-min-velocity", type=int, default=1, help="drop piano notes below this MIDI velocity")
    parser.add_argument(
        "--piano-min-channel-volume",
        type=int,
        default=1,
        help="drop piano notes below this MIDI channel volume controller value",
    )
    parser.add_argument(
        "--keysound-reuse",
        choices=[
            "none",
            "pitch",
            "pitch-duration",
            "track-pitch",
            "track-pitch-duration",
            "channel-pitch",
            "channel-pitch-duration",
        ],
        default="none",
        help="reuse generated keysounds instead of slicing every note separately",
    )
    parser.add_argument(
        "--keysound-no-reuse-tracks",
        help="MIDI/FLP track numbers that always get per-occurrence keysounds, e.g. 19,20",
    )
    parser.add_argument(
        "--keysound-time-bucket-tracks",
        help="MIDI/FLP track numbers whose reused keysounds are split by measure bucket for effects/automation",
    )
    parser.add_argument(
        "--keysound-time-bucket-measures",
        type=int,
        default=8,
        help="measure bucket size for --keysound-time-bucket-tracks",
    )
    parser.add_argument(
        "--clip-overlap-keysounds",
        action="store_true",
        help="cap stem keysound length at the next event from the same audio source to avoid BGM overlap",
    )
    parser.add_argument(
        "--clip-overlap-gap-ms",
        type=int,
        default=0,
        help="silence gap to leave before the next same-source keysound when --clip-overlap-keysounds is enabled",
    )
    parser.add_argument(
        "--clip-overlap-exclude-tracks",
        help="track numbers exempt from --clip-overlap-keysounds; their keysounds keep full decay and ring "
        "naturally (use for piano/sustain instruments that sound choppy when clipped), e.g. 19",
    )
    parser.add_argument(
        "--merge-same-time-keysounds",
        action="store_true",
        help="merge notes that start at the same tick from the same audio source into one keysound slice",
    )
    parser.add_argument(
        "--dedupe-same-time-bgm-code",
        action="store_true",
        default=True,
        help="emit only one BGM #xxx01 event when the same WAV code appears at the same timestamp",
    )
    parser.add_argument(
        "--no-dedupe-same-time-bgm-code",
        dest="dedupe_same_time_bgm_code",
        action="store_false",
    )
    parser.add_argument(
        "--drop-silent-keysounds",
        action="store_true",
        default=True,
        help="skip generated keysounds whose PCM peak is below --drop-silent-min-peak",
    )
    parser.add_argument("--no-drop-silent-keysounds", dest="drop_silent_keysounds", action="store_false")
    parser.add_argument("--drop-silent-min-peak", type=int, default=1, help="PCM peak threshold for --drop-silent-keysounds")
    parser.add_argument("--keysound-dir", help="directory for generated keysound WAV files")
    parser.add_argument("--normalize-keysounds", action="store_true", help="peak-normalize each generated keysound WAV")
    parser.add_argument("--normalize-target-db", type=float, default=-3.0, help="target peak dB for --normalize-keysounds")
    parser.add_argument("--normalize-min-peak", type=int, default=1, help="skip normalization below this PCM peak")
    parser.add_argument(
        "--track-gain-map",
        help='track-specific post-normalize gain in dB, e.g. "11=3,19=-2"',
    )
    parser.add_argument(
        "--anti-stack-keysounds",
        action="store_true",
        help="create quieter variants for keysounds used in dense simultaneous positions",
    )
    parser.add_argument(
        "--anti-stack-threshold",
        type=int,
        default=4,
        help="simultaneous keysounds allowed before anti-stack attenuation starts",
    )
    parser.add_argument(
        "--anti-stack-max-reduce-db",
        type=float,
        default=12.0,
        help="maximum dB reduction for anti-stack variants",
    )
    parser.add_argument(
        "--anti-stack-step-db",
        type=float,
        default=1.0,
        help="quantize anti-stack reduction to this dB step; 0 disables quantization",
    )
    parser.add_argument(
        "--dedupe-identical-keysounds",
        action="store_true",
        default=True,
        help="reuse exact-identical generated keysound WAV files",
    )
    parser.add_argument("--no-dedupe-identical-keysounds", dest="dedupe_identical_keysounds", action="store_false")
    parser.add_argument("--slice-mode", choices=["duration", "fixed"], default="duration")
    parser.add_argument("--slice-ms", type=int, default=250, help="fixed/fallback keysound slice length in milliseconds")
    parser.add_argument("--slice-tail-ms", type=int, default=30, help="tail added to duration-based keysound slices")
    parser.add_argument(
        "--one-shot-tracks",
        help="MIDI/FLP track numbers to slice as fixed one-shot sounds, useful for drums/percussion",
    )
    parser.add_argument("--one-shot-slice-ms", type=int, default=600, help="slice length for --one-shot-tracks")
    parser.add_argument(
        "--track-slice-ms-map",
        help='track-specific fixed slice length in milliseconds, e.g. "3=1600,4=1600"',
    )
    parser.add_argument(
        "--track-min-slice-ms-map",
        help='track-specific minimum slice length in milliseconds, e.g. "2=450,6=500"',
    )
    parser.add_argument("--duration-bucket-ticks", type=int, default=1, help="round MIDI note durations for keysound reuse")
    parser.add_argument("--min-slice-ms", type=int, default=30, help="minimum keysound slice length")
    parser.add_argument("--max-slice-ms", type=int, default=0, help="0 means no maximum keysound slice length")
    parser.add_argument("--max-seconds", type=float, default=0.0, help="cut notes and render output after this song time; 0 disables")
    parser.add_argument("--audio-offset-ms", type=int, default=0, help="offset source WAV timing against MIDI")
    parser.add_argument("--render-midi-wav", help="render selected MIDI notes to a draft WAV with the built-in synth")
    parser.add_argument("--synth-waveform", choices=["sine", "square", "triangle", "saw"], default="sine")
    parser.add_argument("--synth-sample-rate", type=int, default=DEFAULT_SYNTH_SAMPLE_RATE)
    parser.add_argument("--synth-gain", type=float, default=0.22)
    parser.add_argument("--default-note-wav", help="short note sample filename for all unmapped notes")
    parser.add_argument("--make-silence-wav", action="store_true", default=True)
    parser.add_argument("--no-silence-wav", dest="make_silence_wav", action="store_false")
    parser.add_argument("--sample-map", help="CSV with note,wav columns")
    parser.add_argument("--keep-collisions", action="store_true", help="currently keeps first note per slot")
    parser.add_argument("--allow-empty", action="store_true", help="allow BMS output when no MIDI notes are found")
    parser.add_argument("--summary-json", help="write conversion summary JSON")
    parser.add_argument("--flp-patterns-only", action="store_true", help="ignore FL Studio playlist placements")
    parser.add_argument("--flp-default-bpm", type=float, default=120.0, help="fallback BPM when FLP has no tempo")
    parser.add_argument("--write-sample-midi", help="write a tiny sample MIDI and exit")
    parser.add_argument("--write-sample-wav", help="write a tiny sample WAV and exit")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.write_sample_midi:
        write_sample_midi(Path(args.write_sample_midi))
        print(f"wrote sample MIDI: {args.write_sample_midi}")
        return 0
    if args.write_sample_wav:
        write_sample_wav(Path(args.write_sample_wav))
        print(f"wrote sample WAV: {args.write_sample_wav}")
        return 0
    if args.synth_keysounds and (args.keysound_source or args.track_audio_map or args.auto_track_audio_dir):
        raise SystemExit("--synth-keysounds cannot be combined with --keysound-source, --track-audio-map, or --auto-track-audio-dir")
    if args.piano_bms and (
        args.synth_keysounds
        or args.keysound_source
        or args.track_audio_map
        or args.auto_track_audio_dir
        or args.sample_map
        or args.default_note_wav
    ):
        raise SystemExit(
            "--piano-bms cannot be combined with generated/sliced/sample-map keysound modes; "
            "it only references the fixed s_/m_/l_ piano WAV filenames"
        )
    if not args.input:
        raise SystemExit("input MIDI path is required unless --write-sample-midi is used")

    input_path = Path(args.input)
    if input_path.suffix.lower() not in {".mid", ".midi", ".flp"}:
        raise SystemExit("input must be .mid, .midi, or .flp")
    output_path = Path(args.output) if args.output else input_path.with_suffix(".bms")
    midi = parse_flp(input_path, args) if input_path.suffix.lower() == ".flp" else parse_midi(input_path)
    render_summary = None
    if args.render_midi_wav:
        render_notes = filter_notes(
            midi.notes,
            args,
            use_track_filter=not args.background_non_player_to_bgm,
            use_channel_filter=not args.background_non_player_to_bgm,
            use_piano_soft_filter=not args.piano_bms,
        )
        if args.piano_bms:
            validate_piano_args(args)
            render_notes = apply_piano_pedal_durations(render_notes, midi)
            render_notes = filter_soft_piano_notes(render_notes, args)
        render_notes, _render_dropped, _render_clipped = trim_notes_to_max_seconds(render_notes, midi, args)
        render_summary = render_midi_synth_wav(Path(args.render_midi_wav), render_notes, midi, args)
    summary = build_bms(midi, input_path, output_path, args)
    if render_summary:
        summary["rendered_midi_wav"] = render_summary
    if args.summary_json:
        Path(args.summary_json).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
