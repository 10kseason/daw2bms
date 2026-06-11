"""daw2bms GUI -- DAW(MIDI/FLP) stems -> keysounded BMS, click-through workflow.

A thin tkinter front-end over daw2bms.py aimed at the "reproduce the original
mix" workflow: pick the MIDI/FLP, point at the stem folder, optionally give the
master render, set per-track modes, convert.

Per-track modes:
  파티션   -- waveform-preserving keysounds (--partition-keysound-tracks):
              gapless onset tiling, slices sum back to the stem exactly.
              Best default, but every slice is unique -- on dense songs it can
              exhaust the 1296 #WAV namespace, so switch the densest tracks to
              노트별 (pitch+duration reuse keeps their code count tiny and the
              master residual bed absorbs the substitution error in the mix)
  스템 BGM -- whole stem placed as measure-aligned chunks (--bgm-stem-tracks)
  노트별   -- per-note slices reused by track+pitch+duration
  제외     -- track notes dropped (its stem can still play via 추가 BGM 스템)

Stems not assigned to any track are offered as 추가 BGM 스템 (--extra-bgm-stems)
so nothing in the original mix goes missing. With a master render selected, the
master residual bed (--master-residual-bed) makes autoplay sum back to the
master sample-exactly.

Run:  python daw2bms_gui.py [input.mid]
Build a single-file exe:
  pip install pyinstaller pyflp numpy
  pyinstaller --onefile --windowed --name daw2bms-gui --collect-all pyflp daw2bms_gui.py
"""
from __future__ import annotations

import contextlib
import json
import os
import queue
import sys
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import daw2bms

MODE_PARTITION = "파티션"
MODE_STEM = "스템 BGM"
MODE_SLICE = "노트별"
MODE_EXCLUDE = "제외"
MODE_CYCLE = [MODE_PARTITION, MODE_STEM, MODE_SLICE, MODE_EXCLUDE]

AUDIO_EXTS = {".wav"}


def normalize_name(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum())


class QueueWriter:
    """File-like stdout/stderr target that feeds the GUI log queue."""

    def __init__(self, log_queue: "queue.Queue[tuple[str, object]]") -> None:
        self.log_queue = log_queue

    def write(self, text: str) -> None:
        if text:
            self.log_queue.put(("log", text))

    def flush(self) -> None:
        pass


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("daw2bms GUI — DAW 스템 → 키음 BMS")
        root.geometry("1020x780")
        root.minsize(860, 640)

        self.midi = None
        self.track_rows: dict[str, dict] = {}  # tree item id -> row state
        self.extra_rows: dict[str, dict] = {}
        self.log_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.running = False
        self.last_output: Path | None = None

        pad = {"padx": 6, "pady": 3}
        top = ttk.Frame(root)
        top.pack(fill="x", **pad)
        top.columnconfigure(1, weight=1)

        self.input_var = tk.StringVar()
        self.stems_var = tk.StringVar()
        self.master_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.title_var = tk.StringVar()
        self.artist_var = tk.StringVar()
        self.max_seconds_var = tk.StringVar(value="0")
        self.residual_var = tk.BooleanVar(value=True)
        self.all_bgm_var = tk.BooleanVar(value=True)
        self.merge_var = tk.BooleanVar(value=True)

        def file_row(row: int, label: str, var: tk.StringVar, command) -> None:
            ttk.Label(top, text=label).grid(row=row, column=0, sticky="w")
            ttk.Entry(top, textvariable=var).grid(row=row, column=1, sticky="ew", padx=4)
            ttk.Button(top, text="찾아보기…", command=command).grid(row=row, column=2)

        file_row(0, "입력 (.mid/.flp)", self.input_var, self.pick_input)
        file_row(1, "스템 폴더", self.stems_var, self.pick_stems)
        file_row(2, "마스터 렌더 WAV (선택)", self.master_var, self.pick_master)
        file_row(3, "출력 BMS", self.output_var, self.pick_output)
        ttk.Button(top, text="스템 자동 매칭", command=self.auto_match).grid(row=1, column=3, padx=4)
        ttk.Checkbutton(
            top, text="마스터 잔차 베드 (오토플레이 = 마스터와 동일)", variable=self.residual_var
        ).grid(row=2, column=3, padx=4, sticky="w")

        meta = ttk.Frame(root)
        meta.pack(fill="x", **pad)
        ttk.Label(meta, text="제목").pack(side="left")
        ttk.Entry(meta, textvariable=self.title_var, width=24).pack(side="left", padx=4)
        ttk.Label(meta, text="아티스트").pack(side="left")
        ttk.Entry(meta, textvariable=self.artist_var, width=20).pack(side="left", padx=4)
        ttk.Label(meta, text="곡 길이 제한(초, 0=전체)").pack(side="left", padx=(12, 0))
        ttk.Entry(meta, textvariable=self.max_seconds_var, width=7).pack(side="left", padx=4)
        ttk.Checkbutton(meta, text="모든 노트를 BGM으로 (원곡 재현)", variable=self.all_bgm_var).pack(
            side="left", padx=12
        )
        ttk.Checkbutton(meta, text="동시 노트는 화음 키음으로 병합", variable=self.merge_var).pack(
            side="left", padx=4
        )

        tracks_frame = ttk.LabelFrame(root, text="트랙 (더블클릭: 모드 순환 / 스템 열 더블클릭: WAV 지정)")
        tracks_frame.pack(fill="both", expand=True, **pad)
        columns = ("track", "name", "notes", "mode", "stem")
        self.tree = ttk.Treeview(tracks_frame, columns=columns, show="headings", height=10, selectmode="extended")
        headings = {"track": "트랙", "name": "이름", "notes": "노트", "mode": "모드", "stem": "스템 WAV"}
        widths = {"track": 50, "name": 200, "notes": 60, "mode": 110, "stem": 420}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="w", stretch=(col in ("name", "stem")))
        tree_scroll = ttk.Scrollbar(tracks_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.on_tree_double_click)

        track_buttons = ttk.Frame(root)
        track_buttons.pack(fill="x", **pad)
        ttk.Label(track_buttons, text="선택 트랙 →").pack(side="left")
        for mode in MODE_CYCLE:
            ttk.Button(
                track_buttons, text=mode, command=lambda m=mode: self.set_mode_for_selection(m)
            ).pack(side="left", padx=2)
        ttk.Button(track_buttons, text="스템 WAV 지정…", command=self.assign_stem_for_selection).pack(
            side="left", padx=10
        )

        extras_frame = ttk.LabelFrame(
            root, text="추가 BGM 스템 — 트랙에 매핑 안 된 WAV는 통째 BGM으로 배치 (클릭으로 켜고 끄기)"
        )
        extras_frame.pack(fill="x", **pad)
        self.extras_tree = ttk.Treeview(extras_frame, columns=("use", "file"), show="headings", height=4)
        self.extras_tree.heading("use", text="포함")
        self.extras_tree.heading("file", text="파일")
        self.extras_tree.column("use", width=50, anchor="center", stretch=False)
        self.extras_tree.column("file", width=700, anchor="w")
        extras_scroll = ttk.Scrollbar(extras_frame, orient="vertical", command=self.extras_tree.yview)
        self.extras_tree.configure(yscrollcommand=extras_scroll.set)
        self.extras_tree.pack(side="left", fill="x", expand=True)
        extras_scroll.pack(side="right", fill="y")
        self.extras_tree.bind("<Button-1>", self.on_extra_click)

        run_frame = ttk.Frame(root)
        run_frame.pack(fill="x", **pad)
        self.run_button = ttk.Button(run_frame, text="변환 실행", command=self.run_conversion)
        self.run_button.pack(side="left")
        self.progress = ttk.Progressbar(run_frame, mode="indeterminate", length=220)
        self.progress.pack(side="left", padx=10)
        self.open_button = ttk.Button(run_frame, text="출력 폴더 열기", command=self.open_output, state="disabled")
        self.open_button.pack(side="left")

        log_frame = ttk.LabelFrame(root, text="로그")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(log_frame, height=9, state="disabled", wrap="word")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        self.root.after(100, self.drain_log_queue)

    # ---------- logging ----------

    def log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text if text.endswith("\n") else text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def drain_log_queue(self) -> None:
        try:
            while True:
                kind, payload = self.log_queue.get_nowait()
                if kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", str(payload))
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "done":
                    self.on_conversion_done(payload)
        except queue.Empty:
            pass
        self.root.after(100, self.drain_log_queue)

    # ---------- pickers ----------

    def pick_input(self) -> None:
        path = filedialog.askopenfilename(
            title="입력 MIDI/FLP", filetypes=[("MIDI/FLP", "*.mid *.midi *.flp"), ("모든 파일", "*.*")]
        )
        if path:
            self.load_input(Path(path))

    def pick_stems(self) -> None:
        path = filedialog.askdirectory(title="스템 WAV 폴더")
        if path:
            self.stems_var.set(path)
            self.auto_match()

    def pick_master(self) -> None:
        path = filedialog.askopenfilename(title="마스터 렌더 WAV", filetypes=[("WAV", "*.wav")])
        if path:
            self.master_var.set(path)
            self.refresh_extras()

    def pick_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="출력 BMS", defaultextension=".bms", filetypes=[("BMS", "*.bms")]
        )
        if path:
            self.output_var.set(path)

    # ---------- input loading ----------

    def load_input(self, path: Path) -> None:
        try:
            if path.suffix.lower() == ".flp":
                args = daw2bms.parse_args([str(path)])
                midi = daw2bms.parse_flp(path, args)
            else:
                midi = daw2bms.parse_midi(path)
        except BaseException as exc:  # SystemExit included
            messagebox.showerror("입력 파일 오류", str(exc))
            return
        self.midi = midi
        self.input_var.set(str(path))
        if not self.output_var.get():
            self.output_var.set(str(path.with_suffix(".bms")))
        if not self.stems_var.get():
            self.stems_var.set(str(path.parent))

        counts: dict[int, int] = {}
        for note in midi.notes:
            counts[note.track] = counts.get(note.track, 0) + 1
        tracks = sorted(set(midi.track_names) | set(counts))

        self.tree.delete(*self.tree.get_children())
        self.track_rows.clear()
        for track in tracks:
            note_count = counts.get(track, 0)
            mode = MODE_PARTITION if note_count else MODE_EXCLUDE
            item = self.tree.insert(
                "", "end",
                values=(track, midi.track_names.get(track, ""), note_count, mode, ""),
            )
            self.track_rows[item] = {"track": track, "notes": note_count, "mode": mode, "stem": ""}
        self.log(f"입력 로드: {path.name} — 노트 {len(midi.notes)}개, 트랙 {len(tracks)}개")
        self.auto_match()

    # ---------- track table ----------

    def set_row(self, item: str, *, mode: str | None = None, stem: str | None = None) -> None:
        row = self.track_rows[item]
        if mode is not None:
            row["mode"] = mode
        if stem is not None:
            row["stem"] = stem
        values = list(self.tree.item(item, "values"))
        values[3] = row["mode"]
        values[4] = row["stem"]
        self.tree.item(item, values=values)

    def on_tree_double_click(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if not item:
            return
        column = self.tree.identify_column(event.x)
        if column == "#5":  # stem column
            self.assign_stem_dialog([item])
        else:
            current = self.track_rows[item]["mode"]
            self.set_row(item, mode=MODE_CYCLE[(MODE_CYCLE.index(current) + 1) % len(MODE_CYCLE)])
        self.refresh_extras()

    def set_mode_for_selection(self, mode: str) -> None:
        for item in self.tree.selection():
            self.set_row(item, mode=mode)
        self.refresh_extras()

    def assign_stem_for_selection(self) -> None:
        items = list(self.tree.selection())
        if items:
            self.assign_stem_dialog(items)
            self.refresh_extras()

    def assign_stem_dialog(self, items: list[str]) -> None:
        initial = self.stems_var.get() or None
        path = filedialog.askopenfilename(title="스템 WAV 선택", initialdir=initial, filetypes=[("WAV", "*.wav")])
        if path:
            for item in items:
                self.set_row(item, stem=path)

    def auto_match(self) -> None:
        stems_dir = Path(self.stems_var.get()) if self.stems_var.get() else None
        if not stems_dir or not stems_dir.is_dir() or not self.track_rows:
            self.refresh_extras()
            return
        wavs = sorted(p for p in stems_dir.iterdir() if p.suffix.lower() in AUDIO_EXTS)
        matched = 0
        for item, row in self.track_rows.items():
            if row["stem"]:
                continue
            track_name = normalize_name(str(self.tree.item(item, "values")[1]))
            if not track_name:
                continue
            for wav in wavs:
                wav_name = normalize_name(wav.stem)
                if track_name and (track_name in wav_name or wav_name in track_name):
                    self.set_row(item, stem=str(wav))
                    matched += 1
                    break
        if matched:
            self.log(f"스템 자동 매칭: {matched}개 트랙 연결")
        self.refresh_extras()

    # ---------- extras ----------

    def refresh_extras(self) -> None:
        stems_dir = Path(self.stems_var.get()) if self.stems_var.get() else None
        previous = {Path(row["path"]).name: row["use"] for row in self.extra_rows.values()}
        self.extras_tree.delete(*self.extras_tree.get_children())
        self.extra_rows.clear()
        if not stems_dir or not stems_dir.is_dir():
            return
        used = {Path(row["stem"]).resolve() for row in self.track_rows.values() if row["stem"]}
        master = Path(self.master_var.get()).resolve() if self.master_var.get() else None
        for wav in sorted(p for p in stems_dir.iterdir() if p.suffix.lower() in AUDIO_EXTS):
            resolved = wav.resolve()
            if resolved in used or (master and resolved == master):
                continue
            # full-mix style renders must not be layered on top of the stems
            lowered = normalize_name(wav.stem)
            default_use = not any(tag in lowered for tag in ("master", "current", "mixdown", "fullmix"))
            use = previous.get(wav.name, default_use)
            item = self.extras_tree.insert("", "end", values=("✓" if use else "—", wav.name))
            self.extra_rows[item] = {"path": str(wav), "use": use}

    def on_extra_click(self, event) -> None:
        item = self.extras_tree.identify_row(event.y)
        if not item:
            return
        row = self.extra_rows[item]
        row["use"] = not row["use"]
        self.extras_tree.item(item, values=("✓" if row["use"] else "—", Path(row["path"]).name))

    # ---------- conversion ----------

    def build_argv(self) -> list[str]:
        if not self.input_var.get():
            raise ValueError("입력 MIDI/FLP 파일을 선택하세요.")
        input_path = Path(self.input_var.get())
        if not input_path.exists():
            raise ValueError(f"입력 파일이 없습니다: {input_path}")
        output_path = Path(self.output_var.get()) if self.output_var.get() else input_path.with_suffix(".bms")

        try:
            max_seconds = float(self.max_seconds_var.get() or "0")
        except ValueError:
            raise ValueError("곡 길이 제한은 숫자로 입력하세요.")

        partition_tracks: list[int] = []
        stem_tracks: list[int] = []
        included_tracks: list[int] = []
        audio_map: dict[int, str] = {}
        skipped_no_stem: list[str] = []
        for item, row in self.track_rows.items():
            track, mode, stem, notes = row["track"], row["mode"], row["stem"], row["notes"]
            if mode == MODE_EXCLUDE or notes == 0:
                continue
            if not stem:
                name = self.tree.item(item, "values")[1]
                skipped_no_stem.append(f"{track}:{name}")
                continue
            included_tracks.append(track)
            audio_map[track] = stem
            if mode == MODE_PARTITION:
                partition_tracks.append(track)
            elif mode == MODE_STEM:
                stem_tracks.append(track)
        if not included_tracks:
            raise ValueError(
                "변환할 트랙이 없습니다. 노트가 있는 트랙에 스템 WAV를 지정하고 모드를 선택하세요."
            )
        if skipped_no_stem:
            self.log("스템 미지정으로 제외된 트랙: " + ", ".join(skipped_no_stem))

        extras = [row["path"] for row in self.extra_rows.values() if row["use"]]

        argv: list[str] = [str(input_path), "-o", str(output_path)]
        if self.all_bgm_var.get():
            argv += ["--all-notes-to-bgm", "--background-layers", "48"]
        # 노트별 tracks reuse by pitch+duration so dense songs stay inside the
        # 1296 #WAV namespace; partition tracks bypass reuse on their own
        argv += ["--keysound-reuse", "track-pitch-duration"]
        if self.merge_var.get():
            argv += ["--merge-same-time-keysounds"]
        if max_seconds > 0:
            argv += ["--max-seconds", f"{max_seconds:g}"]
        argv += ["--tracks", ",".join(str(t) for t in sorted(included_tracks))]
        argv += ["--track-audio-map", ",".join(f"{t}={p}" for t, p in sorted(audio_map.items()))]
        if partition_tracks:
            argv += ["--partition-keysound-tracks", ",".join(str(t) for t in sorted(partition_tracks))]
        if stem_tracks:
            argv += ["--bgm-stem-tracks", ",".join(str(t) for t in sorted(stem_tracks))]
        if extras:
            argv += ["--extra-bgm-stems", ",".join(extras)]
        if self.master_var.get() and self.residual_var.get():
            argv += ["--master-residual-bed", self.master_var.get()]
        if self.title_var.get():
            argv += ["--title", self.title_var.get()]
        if self.artist_var.get():
            argv += ["--artist", self.artist_var.get()]
        keysound_dir = output_path.parent / f"keysounds_{output_path.stem}"
        argv += ["--keysound-dir", str(keysound_dir)]
        argv += ["--summary-json", str(output_path.with_suffix(".summary.json"))]
        self.last_output = output_path
        return argv

    def run_conversion(self) -> None:
        if self.running:
            return
        try:
            argv = self.build_argv()
        except ValueError as exc:
            messagebox.showerror("설정 오류", str(exc))
            return
        self.running = True
        self.run_button.configure(state="disabled")
        self.progress.start(12)
        self.log("\n=== 변환 시작 ===")
        self.log("daw2bms " + " ".join(argv))

        def worker() -> None:
            writer = QueueWriter(self.log_queue)
            code: object = 1
            try:
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                    code = daw2bms.main(argv)
            except SystemExit as exc:
                writer.write(f"\n오류: {exc}\n")
                code = exc.code if isinstance(exc.code, int) else 1
            except Exception:
                writer.write("\n예상치 못한 오류:\n" + traceback.format_exc())
            self.log_queue.put(("done", code))

        threading.Thread(target=worker, daemon=True).start()

    def on_conversion_done(self, code: object) -> None:
        self.running = False
        self.run_button.configure(state="normal")
        self.progress.stop()
        if code == 0 and self.last_output and self.last_output.exists():
            self.open_button.configure(state="normal")
            summary_path = self.last_output.with_suffix(".summary.json")
            highlights = []
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                highlights.append(f"키음 WAV {summary.get('keysounds_written', '?')}개")
                residual = summary.get("master_residual_bed")
                if residual:
                    highlights.append(
                        f"마스터 잔차 베드 {residual.get('chunks_placed', '?')}청크 "
                        f"(잔차 {residual.get('residual_rms_db', '?')} dBFS)"
                    )
                for warning in summary.get("warnings", []):
                    self.log("주의: " + warning)
            except OSError:
                pass
            self.log("=== 변환 완료: " + " / ".join(highlights) + " ===")
            messagebox.showinfo("완료", f"변환 완료!\n{self.last_output}")
        else:
            self.log("=== 변환 실패 — 위 로그를 확인하세요 ===")
            messagebox.showerror("실패", "변환에 실패했습니다. 로그를 확인하세요.")

    def open_output(self) -> None:
        if self.last_output and self.last_output.exists():
            os.startfile(self.last_output.parent)  # noqa: S606 (local folder open)


def run() -> None:
    trace = Path(os.environ.get("TEMP", ".")) / "daw2bms_gui_trace.log"
    try:
        trace.write_text("start\n", encoding="utf-8")
        root = tk.Tk()
        trace.write_text("tk ok\n", encoding="utf-8")
        try:
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass
        app = App(root)
        trace.write_text("app ok\n", encoding="utf-8")
        if len(sys.argv) > 1:
            candidate = Path(sys.argv[1])
            if candidate.suffix.lower() in {".mid", ".midi", ".flp"} and candidate.exists():
                app.load_input(candidate)
        trace.write_text("mainloop\n", encoding="utf-8")
        root.mainloop()
        trace.write_text("mainloop exited normally\n", encoding="utf-8")
    except BaseException:
        trace.write_text("CRASH:\n" + traceback.format_exc(), encoding="utf-8")
        raise


if __name__ == "__main__":
    run()
