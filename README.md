# daw2bms

> Turn an exported DAW project (MIDI / FL Studio FLP) into a **keysounded BMS draft** by slicing per-note keysounds from your instrument stems.

**English** · [日本語](#日本語) · [한국어](#한국어)

`daw2bms` is a single-file Python tool for BMS authors. It does the tedious first pass — keysound extraction, BGM layering, IR-safe lengths — so you can jump straight to charting. It is **not** an audio renderer: it does not reproduce FL Studio plugin sound by itself. You supply rendered stems/WAVs, and the tool slices and arranges them.

---

## English

### Features
- MIDI (`.mid`/`.midi`) and best-effort FL Studio (`.flp` via [pyflp](https://pypi.org/project/PyFLP/)) parsing
- Per-note keysound slicing from track stems — keeps the real instrument timbre
- **Waveform-preserving partition slicing** (`--partition-keysound-tracks`): stems are cut as a gapless onset-to-onset tiling at the quantized chart grid — no overlap doubling, no gaps, the slices reassemble the original waveform sample-exactly, and every hit keeps its natural ring until the next one
- **Continuous BGM-stem mode** (`--bgm-stem-tracks`): reverb/sustain instruments are placed whole as measure-aligned chunks instead of being chopped per note (no tail clipping). `--extra-bgm-stems` does the same for mixer inserts / audio-clip instruments that exported **no MIDI notes**, so nothing in the original mix goes missing
- **Declick fades** on every slice, so cut edges don't pop
- **IR-safe**: stem chunks stay under the ~30s keysound limit for ranking registration
- **Per-track clip-overlap exclusion** (`--clip-overlap-exclude-tracks`) so piano/sustain rings naturally — BMS never note-offs a retriggered keysound
- **Master-chain emulation** (`--master-emulate ref.wav`): shapes each keysound toward a reference master with a matching EQ + soft limiter. The EQ is *linear*, so it transfers exactly; the *non-linear* bus glue (cross-instrument compression/limiting) cannot be reproduced from isolated keysounds and is intentionally not faked
- **Master residual bed** (`--master-residual-bed master.wav`): simulates the exact mix the BMS will play (every keysound at its scheduled time) and lays `master − simulated mix` as a measure-chunked BGM bed — autoplay then sums back to the master render **sample-exactly**, non-linear bus glue and slice artifacts included; a missed note degrades cleanly to `master − that slice`. Needs stems/master rendered sample-aligned from the same project
- **88-key piano mode** (`--piano-bms`) with s/m/l duration tiers; same-time same-pitch notes keep only the longest tier so layered/doubled notes don't stack duplicate keysounds (`--no-piano-dedupe-duration-tiers` to keep all)
- **Fixed-BPM conversion** (`--fixed-bpm N`): discard the MIDI tempo map and emit one fixed 4/4 BPM, quantizing every note by its real time (ms) to the nearest grid slot at that BPM and `--resolution` — playback timing stays in sync with `--bgm`
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
- **波形保存パーティションスライス**（`--partition-keysound-tracks`）：ステムをオンセット間の隙間なしタイルとして量子化グリッドで切る — 重なりの二重化もギャップもなく、スライスを順に鳴らすと**元の波形がサンプル単位で再構成**され、各ヒットは次のヒットまで自然な余韻を保つ
- **連続 BGM ステムモード**（`--bgm-stem-tracks`）：リバーブ/サステイン系はノート単位で刻まず、小節境界のチャンクとして丸ごと配置（テールが切れない）
- 全スライスに**デクリック・フェード**（切れ目のプチノイズ防止）
- **マスターチェーン・エミュレーション**（`--master-emulate ref.wav`）：各キー音をリファレンスのマスターに合わせる。マッチングEQは*線形*なので正確に転写されるが、*非線形*のバスグルー（楽器間のコンプ/リミッター相互作用）は分離キー音では再現不可で、無理に偽装しない
- **マスター残差ベッド**（`--master-residual-bed master.wav`）：BMS が実際に鳴らすミックス（全キー音をスケジュール時刻で合算）をシミュレートし、`マスター − シミュレートミックス` を小節チャンクの BGM ベッドとして敷く — オートプレイの合計が**サンプル単位でマスターレンダーに一致**（非線形バスグルーもスライス副作用も込み）。ミスしたノートは `マスター − そのスライス` にきれいに劣化。ステムとマスターは同一プロジェクトからサンプル整列でレンダーすること
- **IR 対応**：ステムチャンクをキー音の約30秒制限内に分割
- **トラック別 clip-overlap 除外**（`--clip-overlap-exclude-tracks`）：BMS は再発音してもノートオフしないので、ピアノ等は自然に響かせる
- **88鍵ピアノモード**（`--piano-bms`）：s/m/l の長さ区分。同時刻・同音は最も長い区分だけ残し、レイヤー/重ねで入った音がキー音を同じマスに重複して積むのを防ぐ（`--no-piano-dedupe-duration-tiers` で全保持）
- **BPM 固定変換**（`--fixed-bpm N`）：MIDI のテンポマップを捨てて 4/4・単一 BPM で出力。全ノートを実時間（ms）に換算し、指定 BPM と `--resolution` の格子で最も近いマスに配置 — `--bgm` と再生タイミングが揃う
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
- **원곡 파형 보존 파티션 슬라이스**(`--partition-keysound-tracks`): 스템을 온셋→온셋 빈틈없는 타일로 양자화 격자에서 자름 — 겹침 더블링·빈틈 0, 순서대로 재생하면 **원본 파형이 샘플 단위로 재조립**되고 각 타격은 다음 타격까지 자연스러운 여운 유지
- **연속 BGM 스템 모드**(`--bgm-stem-tracks`): 리버브/서스테인 악기는 노트별로 안 자르고 마디 경계 청크로 통째 배치 → 꼬리 안 잘림
- 모든 슬라이스에 **declick 페이드** (잘린 끝 "툭" 제거)
- **마스터 체인 에뮬레이션**(`--master-emulate ref.wav`): 각 키음을 레퍼런스 마스터에 맞춰 매칭 EQ + 소프트 리미터 적용. EQ는 *선형*이라 정확히 전사되지만, *비선형* 버스 글루(악기 간 comp/limiter 상호작용)는 분리 키음으로 재현 불가 — 억지로 흉내내지 않음
- **마스터 잔차 베드**(`--master-residual-bed master.wav`): BMS가 실제로 낼 믹스(전 키음을 배치 시각에 합산)를 시뮬레이션하고 `마스터 − 시뮬레이션 믹스`를 마디 청크 BGM 베드로 깔아줌 — 오토플레이 합이 **샘플 단위로 마스터 렌더와 일치**(비선형 버스 글루·슬라이스 부작용 포함). 노트를 놓치면 `마스터 − 그 슬라이스`로 우아하게 열화. 스템·마스터는 같은 프로젝트에서 샘플 정렬로 렌더돼 있어야 함
- **IR 대응**: 스템 청크를 키음 30초 제한 아래로 분할
- **트랙별 clip-overlap 제외**(`--clip-overlap-exclude-tracks`): BMS는 재트리거해도 이전 키음이 안 꺼지므로 피아노/서스테인은 자연스럽게 울림
- **88키 피아노 모드**(`--piano-bms`): s/m/l 길이 등급. 같은 시점·같은 음은 가장 긴 등급만 남겨(레이어/더블링으로 들어온) 같은 칸 키음 중복 쌓임을 방지(`--no-piano-dedupe-duration-tiers`로 끔)
- **BPM 고정 변환**(`--fixed-bpm N`): MIDI 템포 맵을 버리고 4/4·단일 BPM으로 출력. 모든 노트를 실제 시간(ms)으로 환산해 지정 BPM·`--resolution` 격자에서 가장 가까운 칸에 배치 — `--bgm`과 재생 타이밍 유지
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
