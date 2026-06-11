"""Offline autoplay render of a BMS vs a reference master WAV.

Sums every scheduled #xxx01 WAV at its BMS-grid time (what a BMS player does on
autoplay) and measures the difference against the reference master render.
Useful to verify a --master-residual-bed conversion really sums back to the master.

Usage:
    python verify_autoplay_render.py song.bms master_render.wav [compare_seconds]

Assumes a single-BPM 4/4 chart (no #xxx03/#xxx08 BPM changes) and 16-bit WAVs.
Requires numpy.
"""
import sys
import wave
from pathlib import Path

import numpy as np


def main() -> None:
    bms_path = Path(sys.argv[1])
    master_path = Path(sys.argv[2])
    compare_seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0

    wav_defs = {}
    bpm = None
    events = []  # (measure, fraction_of_measure, code)
    for line in bms_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#BPM "):
            bpm = float(line.split(None, 1)[1])
        elif line.startswith("#WAV"):
            wav_defs[line[4:6]] = line[7:].strip()
        if len(line) > 7 and line[0] == "#" and line[6] == ":" and line[1:4].isdigit():
            measure, channel = int(line[1:4]), line[4:6]
            if channel == "01":
                data = line[7:].strip()
                pairs = [data[i : i + 2] for i in range(0, len(data) - 1, 2)]
                for i, p in enumerate(pairs):
                    if p != "00":
                        events.append((measure, i / len(pairs), p))

    if not bpm:
        raise SystemExit("no #BPM header")
    measure_len = 4 * 60.0 / bpm  # single-tempo 4/4
    print(f"BPM {bpm}, measure {measure_len:.4f}s, scheduled events: {len(events)}")

    def read_wav(p: Path):
        with wave.open(str(p), "rb") as w:
            sr, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
            a = np.frombuffer(w.readframes(n), dtype="<i2").astype(np.float64) / 32768.0
        return a.reshape(-1, ch), sr

    master, sr = read_wav(master_path)
    if compare_seconds <= 0:
        compare_seconds = len(master) / sr
    buf = np.zeros((int(sr * (compare_seconds + 35)), master.shape[1]))

    cache = {}
    missing = 0
    for measure, frac, code in events:
        rel = wav_defs.get(code)
        if not rel:
            missing += 1
            continue
        if code not in cache:
            x, wsr = read_wav(bms_path.parent / rel)
            if wsr != sr:
                raise SystemExit(f"sample rate mismatch: {rel}")
            if x.shape[1] != master.shape[1]:
                x = np.repeat(x.mean(axis=1, keepdims=True), master.shape[1], axis=1)
            cache[code] = x
        x = cache[code]
        s = int(round((measure + frac) * measure_len * sr))
        e = min(s + len(x), len(buf))
        buf[s:e] += x[: e - s]

    n = min(int(sr * compare_seconds), len(master), len(buf))
    m, r = master[:n], buf[:n]
    diff = r - m

    def rms_db(x) -> float:
        return float(20 * np.log10(np.sqrt(np.mean(x.mean(axis=1) ** 2)) + 1e-12))

    print(f"missing wav codes: {missing}")
    print(f"master rms: {rms_db(m):.2f} dBFS")
    print(f"render rms: {rms_db(r):.2f} dBFS")
    print(f"diff   rms: {rms_db(diff):.2f} dBFS  (diff-to-master {rms_db(diff) - rms_db(m):+.2f} dB)")
    print(f"correlation: {np.corrcoef(r.mean(axis=1), m.mean(axis=1))[0, 1]:.6f}")

    dm = diff.mean(axis=1)
    worst_db, worst_t = -999.0, 0.0
    for s in range(0, n - sr, sr // 2):
        d = 20 * np.log10(np.sqrt(np.mean(dm[s : s + sr] ** 2)) + 1e-12)
        if d > worst_db:
            worst_db, worst_t = d, s / sr
    print(f"worst 1s window: {worst_db:.2f} dBFS at {worst_t:.1f}s")


if __name__ == "__main__":
    main()
