# daw2bms

> Turn an exported DAW project (MIDI / FL Studio FLP) into a **keysounded BMS draft** by slicing per-note keysounds from your instrument stems.

**English** · [日本語](#日本語) · [한국어](#한국어)

`daw2bms` is a single-file Python tool for BMS authors. It does the tedious first pass — keysound extraction, BGM layering, IR-safe lengths — so you can jump straight to charting. It is **not** an audio renderer: it does not reproduce FL Studio plugin sound by itself. You supply rendered stems/WAVs, and the tool slices and arranges them.

---

## English

### Features
- MIDI (`.mid`/`.midi`) and best-effort FL Studio (`.flp` via [pyflp](https://pypi.org/project/PyFLP/)) parsing
- Per-note keysound slicing from track stems — keeps the real instrument timbre
- **Continuous BGM-stem mode** (`--bgm-stem-tracks`): reverb/sustain instruments are placed whole as measure-aligned chunks instead of being chopped per note (no tail clipping). `--extra-bgm-stems` does the same for mixer inserts / audio-clip instruments that exported **no MIDI notes**, so nothing in the original mix goes missing
- **Declick fades** on every slice, so cut edges don't pop
- **IR-safe**: stem chunks stay under the ~30s keysound limit for ranking registration
- **Per-track clip-overlap exclusion** (`--clip-overlap-exclude-tracks`) so piano/sustain rings naturally — BMS never note-offs a retriggered keysound
- Keysound reuse, identical-WAV dedupe, silent-drop, peak normalize, anti-stack, 2-minute test cut
- A `--summary-json` report for every conversion

### Requirements
- Python 3 (3.12 recommended if you read `.flp` directly)
- MIDI only: no extra dependencies. FLP: `pip install pyflp`

### Quick start
```bash
# MIDI -> BMS draft (pattern-confirmation, silent.wav based)
python daw2bms.py song.mid -o song.bms --title "Title" --artist "Artist"
```

Hybrid conversion (recommended for "sounds like the original"): punchy tracks become keysounds, reverb/sustain tracks become continuous BGM stems.
```bash
python daw2bms.py song.mid -o song.bms \
  --all-notes-to-bgm --max-seconds 129 --background-layers 32 \
  --track-audio-map "2=Kick.wav,10=Bass.wav,12=Lead.wav,19=Piano.wav,20=Organ.wav,21=Bell.wav" \
  --bgm-stem-tracks 20,21 \
  --keysound-reuse track-pitch-duration \
  --clip-overlap-keysounds --clip-overlap-exclude-tracks 19 \
  --keysound-dir keysounds --summary-json out.summary.json
```

Inspect MIDI tracks first to pick numbers for `--track-audio-map` / `--bgm-stem-tracks`:
```bash
python -c "from pathlib import Path; from daw2bms import parse_midi; from collections import Counter; m=parse_midi(Path('song.mid')); print(m.track_names); print(Counter(n.track for n in m.notes).most_common())"
```

📖 Full option reference: **[docs/USAGE.ko.md](docs/USAGE.ko.md)** · quick guide: **[docs/QUICKSTART.ko.md](docs/QUICKSTART.ko.md)** (Korean)

### Getting stems out of FL Studio
1. Back up the `.flp` first.
2. `Tools → Macros → Prepare for MIDI export`, then `File → Export → MIDI file`.
3. `File → Export → WAV file` with **Split mixer tracks** enabled to render per-instrument stems.
4. Match the track numbers from the inspect command above and map them with `--track-audio-map`.

### Notes & limits
- A BMS keysound plays to its end (no note-off). Sustain instruments should overlap-ring, so put them in `--clip-overlap-exclude-tracks`, not under clip-overlap.
- `#WAVxx` is base36 → **max 1296 keysounds**. Use `--keysound-reuse`, `--duration-bucket-ticks`, or `--max-seconds` if you exceed it.
- `--synth-keysounds` uses a built-in draft synth; it does not sound like the real instrument.
- Dense MIDI converted 1:1 makes unplayable charts — this produces a draft, not a finished chart.
- **After converting, verify every non-silent stem/mixer insert is represented** (keysounded or stemmed). A forgotten insert is the #1 cause of "sounds different from the original" — feed inserts with no MIDI track through `--extra-bgm-stems`.
- **You are responsible for the copyright of any MIDI/audio you feed in.** Do not commit other people's songs/stems to your repo.

### License
MIT — see [LICENSE](LICENSE).

---

## 日本語

DAW（MIDI / FL Studio FLP）から書き出したプロジェクトを、ステムから切り出した**キー音付き BMS のたたき台**に変換する単一ファイルの Python ツールです。キー音抽出・BGM レイヤー配置・IR 対応の長さ調整といった面倒な下処理を自動化し、譜面作成にすぐ取りかかれるようにします。**音源レンダラーではありません**（FL のプラグイン音は再現しません）。レンダリング済みのステム/WAV を渡してください。

### 主な機能
- MIDI（`.mid`/`.midi`）と FL Studio（`.flp`、pyflp による best-effort）の解析
- トラックのステムからノートごとにキー音をスライス（実際の音色を保持）
- **連続 BGM ステムモード**（`--bgm-stem-tracks`）：リバーブ/サステイン系はノート単位で刻まず、小節境界のチャンクとして丸ごと配置（テールが切れない）
- 全スライスに**デクリック・フェード**（切れ目のプチノイズ防止）
- **IR 対応**：ステムチャンクをキー音の約30秒制限内に分割
- **トラック別 clip-overlap 除外**（`--clip-overlap-exclude-tracks`）：BMS は再発音してもノートオフしないので、ピアノ等は自然に響かせる
- キー音の再利用・同一WAVの重複排除・無音除去・ピーク正規化・アンチスタック・2分テストカット
- 変換ごとに `--summary-json` レポート

### 必要環境
- Python 3（`.flp` を直接読むなら 3.12 推奨）
- MIDI のみなら追加依存なし。FLP は `pip install pyflp`

### クイックスタート
```bash
python daw2bms.py song.mid -o song.bms --title "曲名" --artist "作者"
```
ハイブリッド変換（原曲に近づけたい場合）— 打撃系はキー音、リバーブ/サステイン系は連続 BGM ステムに：
```bash
python daw2bms.py song.mid -o song.bms \
  --all-notes-to-bgm --max-seconds 129 --background-layers 32 \
  --track-audio-map "2=Kick.wav,10=Bass.wav,12=Lead.wav,19=Piano.wav" \
  --bgm-stem-tracks 20,21 \
  --keysound-reuse track-pitch-duration \
  --clip-overlap-keysounds --clip-overlap-exclude-tracks 19 \
  --keysound-dir keysounds --summary-json out.summary.json
```
📖 全オプション：**[docs/USAGE.ko.md](docs/USAGE.ko.md)**（韓国語）

### 注意・制限
- キー音は終端まで再生されます（ノートオフなし）。サステイン系は clip-overlap ではなく `--clip-overlap-exclude-tracks` に入れて重ねて響かせます。
- `#WAVxx` は base36 → **キー音は最大 1296 個**。超える場合は再利用・時間カットを。
- `--synth-keysounds` は内蔵の簡易シンセで、実際の音色とは異なります。
- 変換後は**全ての非無音ステム/ミキサーインサートが反映されているか確認**を。抜けたインサートは「原曲と違う」の最大要因。MIDI ノートを持たないインサートは `--extra-bgm-stems` で追加します。
- **入力する MIDI/音源の著作権は利用者の責任です。** 他人の楽曲/ステムをリポジトリに含めないでください。

### ライセンス
MIT（[LICENSE](LICENSE) 参照）。

---

## 한국어

DAW(MIDI / FL Studio FLP)에서 내보낸 프로젝트를, **스템에서 잘라낸 키음이 박힌 BMS 초안**으로 변환하는 단일 파일 Python 도구입니다. 키음 추출·BGM 레이어 배치·IR 길이 맞추기 같은 귀찮은 1차 작업을 자동화해서 바로 채보에 들어갈 수 있게 해줍니다. **음원 렌더러가 아닙니다**(FL 플러그인 소리를 직접 재현하지 않음). 렌더된 스템/WAV를 넣어주면 그걸 잘라 배치합니다.

### 주요 기능
- MIDI(`.mid`/`.midi`) 및 FL Studio(`.flp`, pyflp best-effort) 파싱
- 트랙 스템에서 노트별 키음 슬라이스 — 실제 음색 유지
- **연속 BGM 스템 모드**(`--bgm-stem-tracks`): 리버브/서스테인 악기는 노트별로 안 자르고 마디 경계 청크로 통째 배치 → 꼬리 안 잘림
- 모든 슬라이스에 **declick 페이드** (잘린 끝 "툭" 제거)
- **IR 대응**: 스템 청크를 키음 30초 제한 아래로 분할
- **트랙별 clip-overlap 제외**(`--clip-overlap-exclude-tracks`): BMS는 재트리거해도 이전 키음이 안 꺼지므로 피아노/서스테인은 자연스럽게 울림
- 키음 재사용·동일 WAV 중복제거·무음 제거·피크 정규화·anti-stack·2분 테스트컷
- 변환마다 `--summary-json` 리포트

### 준비물
- Python 3 (`.flp` 직접 읽기는 3.12 권장)
- MIDI만이면 추가 설치 없음. FLP는 `pip install pyflp`

### 빠른 시작
```bash
python daw2bms.py song.mid -o song.bms --title "곡명" --artist "작곡가"
```
하이브리드 변환(원곡처럼 들리게 하고 싶을 때) — 타격감 악기는 키음, 리버브/서스테인은 연속 BGM 스템:
```bash
python daw2bms.py song.mid -o song.bms \
  --all-notes-to-bgm --max-seconds 129 --background-layers 32 \
  --track-audio-map "2=Kick.wav,10=Bass.wav,12=Lead.wav,19=Piano.wav" \
  --bgm-stem-tracks 20,21 \
  --keysound-reuse track-pitch-duration \
  --clip-overlap-keysounds --clip-overlap-exclude-tracks 19 \
  --keysound-dir keysounds --summary-json out.summary.json
```
📖 전체 옵션: **[docs/USAGE.ko.md](docs/USAGE.ko.md)** · 빠른 가이드: **[docs/QUICKSTART.ko.md](docs/QUICKSTART.ko.md)**

### 주의·한계
- BMS 키음은 끝까지 재생됨(note-off 없음). 서스테인 악기는 clip-overlap 말고 `--clip-overlap-exclude-tracks`에 넣어 겹쳐 울리게.
- `#WAVxx`는 base36 → **키음 최대 1296개**. 넘으면 재사용·시간컷 사용.
- `--synth-keysounds`는 내장 간이 신스라 실제 음색과 다름.
- 변환 후 **모든 비-무음 스템/믹서 인서트가 반영됐는지 확인**. 빠진 인서트 = "원곡과 다름"의 1순위 원인. MIDI 노트 없는 인서트는 `--extra-bgm-stems`로 추가.
- **넣는 MIDI/음원의 저작권은 사용자 책임.** 남의 곡/스템을 레포에 올리지 마세요.

### 라이선스
MIT — [LICENSE](LICENSE) 참고.
