# daw2bms 사용 설명서

`daw2bms`는 MIDI 또는 FL Studio 프로젝트에서 BMS 초안을 만드는 긴급 변환 도구입니다.

주요 기능:

- MIDI `.mid/.midi` 파싱
- FL Studio `.flp` 노트 타임라인 파싱
- 플레이어 노트와 BGM 키음 노트 분리
- 패턴을 짜지 않고 모든 노트를 BGM 키음 채널로 보내 원곡 재현 우선 변환
- BGM 키음을 BMS `#xxx01` 채널에 여러 레이어로 배치
- MIDI note-on / note-off 길이를 반영한 키음 WAV 생성
- 같은 `트랙 + 음높이 + 길이` 키음 재사용
- 트랙별 stem WAV를 지정해서 FL Studio 원래 음색에 가깝게 키음 추출
- 앞 2분만 자르는 테스트 컷
- MIDI 정보를 이용한 간이 synth WAV 렌더

주의: 이 도구는 FL Studio의 플러그인 소리를 정확히 렌더링하는 프로그램이 아닙니다. `--synth-keysounds`는 테스트용 내장 신스로 키음을 만드는 기능입니다. 실제 투고 품질을 원하면 FL Studio에서 악기별 stem 또는 keysound용 WAV를 따로 렌더한 뒤 사용하는 쪽이 더 안전합니다.

## 신규 옵션 — 원곡 충실 변환 (권장 워크플로우)

원곡처럼 들리게 하려면 "타격감 악기는 키음, 리버브/서스테인 악기는 연속 BGM 스템" 하이브리드가 가장 효과적입니다.

```bat
python daw2bms.py "song.mid" -o "song.bms" ^
  --all-notes-to-bgm --max-seconds 129 --background-layers 32 ^
  --track-audio-map "2=Kick.wav,5=Hat.wav,6=Snare.wav,10=Bass.wav,11=Brass.wav,12=Lead.wav,13=Orch.wav,19=Piano.wav,20=Organ.wav,21=Bell.wav" ^
  --bgm-stem-tracks 11,12,13,19,20,21 ^
  --extra-bgm-stems "Insert 5.wav" ^
  --bgm-stem-max-seconds 28 ^
  --keysound-reuse track-pitch-duration ^
  --clip-overlap-keysounds --clip-overlap-exclude-tracks 19 ^
  --keysound-dir "keysounds" --summary-json "out.summary.json"
```

새로 추가된 옵션:

- `--bgm-stem-tracks 11,12,13,...`: 해당 트랙을 노트별로 자르지 않고 **스템을 통째로 연속 BGM**으로 배치합니다. 리버브/서스테인 악기(패드·스트링·피아노·오르간·벨)는 노트별로 자르면 꼬리가 끊기므로 이쪽이 깔끔합니다. 스템은 **마디 경계 기준 청크**로 분할돼 끊김 없이 이어집니다.
- `--bgm-stem-max-seconds 28`: 각 스템 청크의 최대 길이(초). BMS IR(인터넷 랭킹)은 키음 길이 **30초 제한**이 있어서 그 아래로 쪼갭니다. 마디 경계로 자르므로 재생은 gapless.
- `--bgm-stem-seam-fade-ms 3`: 청크 경계의 declick 페이드(ms). beatoraja처럼 샘플 단위로 스케줄하는 플레이어면 `0`도 가능.
- `--extra-bgm-stems "Insert 5.wav,FX.wav"`: **MIDI 트랙과 무관한** raw 스템 WAV를 연속 BGM으로 배치합니다. 오디오 클립 악기, 혹은 매핑에서 빠진 믹서 인서트처럼 **노트가 없는 소리**를 넣을 때 씁니다.
- `--keysound-fade-in-ms 2 / --keysound-fade-out-ms 8`: 잘린 키음 끝의 "툭" 클릭을 없애는 declick 페이드.
- `--clip-overlap-exclude-tracks 19`: `--clip-overlap-keysounds`에서 특정 트랙을 제외합니다. BMS는 키음을 다시 쳐도 이전 음이 꺼지지 않으므로(자연 중첩), **피아노 등 여음이 긴 악기는 clip하면 끊겨 들립니다.** 그런 트랙을 여기 넣어 풀로 울리게 둡니다.

⚠️ **원곡과 다르게 들리거나 "뭔가 빠진" 느낌이면**, 먼저 **모든 비-무음 stem/믹서 인서트가 키음이나 스템으로 들어갔는지 대조**하세요. 매핑에서 빠진 인서트가 가장 흔한 원인이며, MIDI 노트가 없는 인서트는 `--extra-bgm-stems`로 넣습니다.

## 준비

Python 3이 필요합니다.
FLP 직접 읽기는 pyflp 호환성 때문에 Python 3.12를 권장합니다. `EventEnum has no members` 에러가 나면 최신 zip의 `daw2bms.py`로 교체하세요.

```bat
cd /d D:\TMD
python --version
```

FLP 직접 읽기가 필요하면 한 번만 설치합니다.

```bat
install_flp_support.bat
```

또는 직접 설치:

```bat
python -m pip install -r requirements-flp.txt
```

Python 3.12로 명시해서 설치/실행:

```bat
py -3.12 -m pip install pyflp
py -3.12 daw2bms.py "project.flp" -o "project_synth.bms" --synth-keysounds
```

## 가장 기본 변환

MIDI를 BMS 초안으로 변환합니다. 소리는 `silence.wav` 기반이라 패턴 확인용입니다.

```bat
python daw2bms.py "song.mid" -o "song.bms" --title "곡명" --artist "작곡가"
```

전체 렌더 WAV를 BGM으로 한 번 재생하려면:

```bat
python daw2bms.py "song.mid" -o "song.bms" --title "곡명" --artist "작곡가" --bgm "song.wav"
```

## 트랙 확인

어떤 트랙을 플레이어 노트로 쓸지 먼저 확인합니다.

```bat
python -c "from pathlib import Path; from daw2bms import parse_midi; from collections import Counter; m=parse_midi(Path('song.mid')); print(m.track_names); print(Counter(n.track for n in m.notes).most_common())"
```

출력 예시:

```text
{8: 'MELODY', 10: 'DRUMS', ...}
[(4, 1043), (3, 690), (8, 217), ...]
```

이 경우 `--player-tracks 8`처럼 지정하면 8번 트랙만 플레이어 노트가 됩니다.

## 플레이어 노트 + BGM 키음 채널

특정 트랙은 플레이어가 치는 노트로 두고, 나머지 트랙은 BGM 키음 채널로 보냅니다.

```bat
python daw2bms.py "song.mid" -o "song_bgmkeys.bms" ^
  --player-tracks 8 ^
  --background-non-player-to-bgm ^
  --background-layers 15
```

의미:

- `--player-tracks 8`: 8번 트랙을 플레이어 채널 `11,12,13,14,15,18,19`에 배치
- `--background-non-player-to-bgm`: player가 아닌 노트는 BGM 키음으로 처리
- `--background-layers 15`: 같은 시각에 겹치는 BGM 키음을 최대 15 레이어까지 `#xxx01`에 반복 출력

## 원곡 재현 우선, 패턴 없음

패턴을 아직 짜지 않고 원곡처럼 들리게 변환하는 것이 우선이면 모든 노트를 BGM 키음 채널로 보냅니다.

```bat
python daw2bms.py "song.mid" -o "song_bgm_only.bms" ^
  --max-seconds 120 ^
  --all-notes-to-bgm ^
  --background-layers 32 ^
  --auto-track-audio-dir "stems" ^
  --keysound-reuse track-pitch-duration ^
  --normalize-keysounds ^
  --keysound-dir "keysounds_bgm_only" ^
  --summary-json "song_bgm_only.summary.json"
```

이 방식은 플레이어 채널에 노트를 만들지 않습니다. 모든 노트가 `#xxx01` BGM 키음 레이어로 들어가므로 원곡 재현 확인용에 더 맞습니다.

## 88키 피아노 샘플 BMS 만들기

이미 준비된 피아노 키음 파일이 `s_000.wav`부터 `l_087.wav`까지 있을 때 씁니다. MIDI 노트는 88키 피아노 범위 기준으로 A0=`000`, C8=`087`에 대응하고, BMS에는 `#WAV01`부터 `#WAV7C`까지 고정 정의가 들어갑니다.

```bat
python daw2bms.py "piano.mid" -o "piano_template.bms" ^
  --piano-bms ^
  --all-notes-to-bgm ^
  --background-layers 32 ^
  --summary-json "piano_template.summary.json"
```

길이 분류:

- `s_000.wav` 계열: 200ms 미만
- `m_000.wav` 계열: 200ms 이상
- `l_000.wav` 계열: 800ms 이상

옵션:

- `--piano-medium-ms 200`: `m_`으로 넘어가는 기준
- `--piano-long-ms 800`: `l_`로 넘어가는 기준
- `--piano-min-velocity 8`: 너무 약한 note velocity 제거 기준
- `--piano-min-channel-volume 8`: 너무 낮은 MIDI channel volume(CC 7) 제거 기준

피아노 모드는 MIDI 10번 채널을 자동 제외합니다. MIDI sustain pedal(CC 64)은 note-off 뒤 페달이 풀리는 시점 또는 같은 음이 다시 나오는 시점까지 길이를 늘려서 분류합니다. Sostenuto pedal(CC 66)은 페달이 눌린 순간 이미 울리던 음만 같은 방식으로 늘려서 분류합니다.

## MIDI로 키음 WAV 생성

MIDI 정보만으로 테스트용 키음을 생성합니다.

```bat
python daw2bms.py "song.mid" -o "song_synth.bms" ^
  --player-tracks 8 ^
  --background-non-player-to-bgm ^
  --background-layers 15 ^
  --synth-keysounds ^
  --keysound-reuse track-pitch-duration ^
  --keysound-dir "keysounds_synth" ^
  --render-midi-wav "song_synth.wav" ^
  --summary-json "song_synth.summary.json"
```

의미:

- `--synth-keysounds`: MIDI note 번호, velocity, duration으로 키음 WAV 생성
- `--keysound-reuse track-pitch-duration`: 같은 트랙, 같은 음, 같은 길이면 키음 재사용
- `--keysound-dir`: 생성된 키음 WAV 폴더
- `--render-midi-wav`: MIDI 전체를 간이 synth WAV로 렌더
- `--summary-json`: 변환 결과 요약 저장

## 앞 2분만 테스트

곡 전체가 너무 길거나 `#WAVxx` 한계를 넘으면 앞 2분만 잘라서 테스트합니다.

```bat
python daw2bms.py "song.mid" -o "song_2min_synth.bms" ^
  --max-seconds 120 ^
  --player-tracks 8 ^
  --background-non-player-to-bgm ^
  --background-layers 15 ^
  --synth-keysounds ^
  --keysound-reuse track-pitch-duration ^
  --keysound-dir "keysounds_2min_exact" ^
  --render-midi-wav "song_2min_synth.wav" ^
  --summary-json "song_2min_synth.summary.json"
```

`--max-seconds 120` 동작:

- 120초 이후 시작하는 노트는 버림
- 120초 전에 시작해서 120초를 넘는 긴 음은 120초까지만 자름
- BMS, 키음 WAV, synth WAV 렌더에 모두 같은 컷 적용

## 키음 수가 너무 많을 때

BMS `#WAVxx`는 두 글자 코드라 실질적으로 쓸 수 있는 WAV 정의 수가 제한됩니다. exact duration 기준으로 너무 많은 키음이 생기면 duration을 몇 tick 단위로 묶습니다.

```bat
python daw2bms.py "song.mid" -o "song_synth_bucket.bms" ^
  --player-tracks 8 ^
  --background-non-player-to-bgm ^
  --background-layers 15 ^
  --synth-keysounds ^
  --keysound-reuse track-pitch-duration ^
  --duration-bucket-ticks 5 ^
  --keysound-dir "keysounds_bucket5" ^
  --summary-json "song_synth_bucket.summary.json"
```

권장 순서:

1. 먼저 `--max-seconds 120`으로 2분 테스트
2. 그래도 WAV 수가 너무 많으면 `--duration-bucket-ticks 5`
3. 더 줄여야 하면 `--keysound-reuse track-pitch`

## FL Studio에서 MIDI로 내보내기

가장 안전한 절차:

1. `.flp` 원본을 백업합니다.
2. 백업본에서 `Tools -> Macros -> Prepare for MIDI export`를 실행합니다.
3. `File -> Export -> MIDI file`로 MIDI를 뽑습니다.
4. 필요하면 전체 곡 또는 stem을 WAV로 렌더합니다.
5. 원본 `.flp`는 MIDI export 준비 상태로 저장하지 않습니다.

내보낸 MIDI에 실제 piano-roll 노트가 있어야 합니다. 변환기가 `no MIDI note-on events survived`라고 하면 MIDI가 비어 있는 것입니다.

## FLP 직접 변환

FLP 직접 읽기는 `pyflp` 기반 best-effort 기능입니다. FLP 포맷이 공식 안정 교환 포맷이 아니라 실패할 수 있습니다.

```bat
install_flp_support.bat
python daw2bms.py "project.flp" -o "project_synth.bms" ^
  --player-tracks 8 ^
  --background-non-player-to-bgm ^
  --background-layers 15 ^
  --synth-keysounds ^
  --keysound-reuse track-pitch-duration ^
  --max-seconds 120 ^
  --summary-json "project_synth.summary.json"
```

배치 템플릿도 있습니다.

```bat
convert_flp_template.bat project.flp keysound_stem.wav backing.wav
```

단, FLP 안의 플러그인 소리를 그대로 렌더하는 기능은 아닙니다.

## 실제 stem WAV를 잘라 키음으로 쓰기

이미 렌더된 keysound용 stem WAV가 있으면 MIDI 타이밍으로 잘라 BMS 키음에 붙일 수 있습니다.

```bat
python daw2bms.py "song.mid" -o "song_split.bms" ^
  --player-tracks 8 ^
  --background-non-player-to-bgm ^
  --background-layers 15 ^
  --keysound-source "keysound_stem.wav" ^
  --keysound-reuse track-pitch-duration ^
  --bgm "backing.wav" ^
  --summary-json "song_split.summary.json"
```

주의:

- 전체 믹스 `song.wav`를 `--keysound-source`로 쓰면 진짜 악기 분리가 아니라 전체 믹스 조각이 잘립니다.
- 같은 full mix를 `--keysound-source`와 `--bgm`에 동시에 쓰면 소리가 중복됩니다.
- 좋은 결과를 원하면 FL Studio에서 `player용 stem`, `background/backing stem`을 따로 렌더하는 편이 낫습니다.

## 트랙별 stem WAV로 FL 음색 유지하기

FL Studio의 실제 음색을 최대한 유지하려면 MIDI와 함께 트랙별 stem WAV를 뽑은 뒤 `--track-audio-map`으로 연결합니다.

FL Studio에서:

1. 각 악기 채널을 Mixer track에 라우팅합니다.
2. `File -> Export -> WAV file`에서 `Split mixer tracks`를 켜고 렌더합니다.
3. MIDI 체크 명령어로 나온 track 번호와 렌더된 stem WAV를 맞춥니다.

예를 들어 MIDI 체크 결과가 `10: BassGuitar2`, `12: Hypersaw`, `13: Orchestral`이면:

stem WAV 파일명이 트랙 이름과 같으면 자동 매칭을 쓸 수 있습니다.

```bat
python daw2bms.py "song.mid" -o "song_auto_stems.bms" ^
  --max-seconds 120 ^
  --player-tracks 12,19,20,21 ^
  --background-non-player-to-bgm ^
  --background-layers 15 ^
  --auto-track-audio-dir "stems" ^
  --keysound-reuse track-pitch-duration ^
  --keysound-dir "keysounds_stems" ^
  --summary-json "song_auto_stems.summary.json"
```

FLP를 직접 읽을 때도 입력 파일만 `.flp`로 바꾸면 됩니다.

```bat
python daw2bms.py "project.flp" -o "song_flp_auto_stems.bms" ^
  --max-seconds 120 ^
  --player-tracks 12,19,20,21 ^
  --background-non-player-to-bgm ^
  --background-layers 15 ^
  --auto-track-audio-dir "stems" ^
  --keysound-reuse track-pitch-duration ^
  --keysound-dir "keysounds_flp_stems" ^
  --summary-json "song_flp_auto_stems.summary.json"
```

자동 매칭이 실패하면 직접 지정합니다.

```bat
python daw2bms.py "song.mid" -o "song_stem_keys.bms" ^
  --max-seconds 120 ^
  --player-tracks 12,19,20,21 ^
  --background-non-player-to-bgm ^
  --background-layers 15 ^
  --track-audio-map "10=BassGuitar2.wav,12=Hypersaw.wav,13=Orchestral.wav,14=Orchestral_2.wav,19=ePiano.wav,20=Organ1.wav,21=Dream_bell.wav" ^
  --keysound-reuse track-pitch-duration ^
  --normalize-keysounds ^
  --track-gain-map "12=3,19=2" ^
  --keysound-dir "keysounds_stems" ^
  --summary-json "song_stem_keys.summary.json"
```

드럼도 키음으로 쓸 거면 드럼 트랙 stem도 같이 넣습니다.

```bat
--track-audio-map "2=Kick.wav,3=Crash.wav,5=Hat.wav,6=Snare.wav,10=Bass.wav,12=Lead.wav"
```

주의:

- `--track-audio-map`에 없는 트랙이 변환 대상에 있으면 에러가 납니다.
- `--auto-track-audio-dir`는 트랙 이름과 WAV 파일명이 비슷할 때만 매칭됩니다.
- 빠진 트랙을 전체 WAV에서라도 자르고 싶으면 `--keysound-source "fullmix.wav"`를 fallback으로 같이 줄 수 있습니다.
- `--normalize-keysounds`를 넣으면 생성된 키음 WAV를 파일별 peak 기준으로 보통화합니다. 기본 목표는 -3 dB입니다.
- `--track-gain-map "12=3,19=-2"`처럼 쓰면 특정 트랙 키음만 dB 단위로 더 키우거나 줄입니다. 이 보정은 보통화 뒤에 적용됩니다.
- 완전히 같은 키음 WAV는 기본으로 하나만 남기고 재사용합니다. 끄려면 `--no-dedupe-identical-keysounds`를 씁니다.
- 경로에 공백이 있으면 전체 map 문자열을 따옴표로 감싸세요.
- 이 방식도 stem WAV 안에 리버브/딜레이/사이드체인 등 시간에 따라 달라지는 효과가 있으면 완벽히 분리되지는 않습니다.

## 자주 쓰는 옵션

```text
--player-tracks 8
```

플레이어가 칠 트랙을 지정합니다. 트랙 확인 명령어에 나온 번호를 그대로 씁니다.

```text
--background-non-player-to-bgm
```

플레이어 트랙이 아닌 노트를 BGM 키음 채널로 보냅니다.

```text
--all-notes-to-bgm
```

패턴을 만들지 않고 모든 노트를 BGM 키음 채널로 보냅니다. 원곡 재현 확인이 우선일 때 씁니다.

```text
--background-layers 15
```

같은 시각에 겹치는 BGM 키음 레이어 수입니다.

```text
--synth-keysounds
```

MIDI 정보로 테스트용 키음 WAV를 생성합니다.

```text
--piano-bms
```

88키 피아노 샘플명 `s_000.wav`부터 `l_087.wav`까지를 `#WAV01`부터 `#WAV7C`까지 고정 정의하고, MIDI 노트의 pitch/duration으로 해당 WAV 코드를 배치합니다. MIDI 10번 채널은 자동 제외합니다.

```text
--piano-medium-ms 200 --piano-long-ms 800
```

피아노 샘플 길이 분류 기준입니다. 200ms 미만은 `s_`, 200ms 이상은 `m_`, 800ms 이상은 `l_`를 씁니다.

```text
--piano-min-velocity 8 --piano-min-channel-volume 8
```

너무 작게 들리는 피아노 노트를 제외합니다. channel volume은 MIDI CC 7 값을 봅니다.

```text
--track-audio-map "10=Bass.wav,12=Lead.wav"
```

트랙별 stem WAV에서 해당 트랙 노트를 잘라 키음 WAV를 생성합니다.

```text
--auto-track-audio-dir "stems"
```

MIDI/FLP 트랙 이름과 `stems` 폴더 안의 WAV 파일명을 자동 매칭해 키음 WAV를 생성합니다.

```text
--normalize-keysounds --normalize-target-db -3
```

각 키음 WAV를 파일별 peak 기준으로 -3 dB에 맞춥니다. 너무 조용한 키음이 많은 경우 체감 볼륨을 맞추는 용도입니다.

```text
--track-gain-map "12=3,19=-2"
```

12번 트랙 키음을 +3 dB, 19번 트랙 키음을 -2 dB로 보정합니다. `--normalize-keysounds`와 같이 쓰면 보통화 뒤에 적용됩니다. 크게 올릴 때는 clipping을 피하려고 `--normalize-target-db -6`처럼 여유를 두는 것이 좋습니다.

--no-dedupe-identical-keysounds
```

완전히 같은 내용의 키음 WAV 자동 재사용을 끕니다. 기본값은 중복 제거 켜짐입니다.

```text
--keysound-reuse track-pitch-duration
```

같은 트랙, 같은 음, 같은 길이 키음을 재사용합니다.

```text
--duration-bucket-ticks 5
```

MIDI 길이를 5 tick 단위로 묶어서 WAV 수를 줄입니다.

```text
--max-seconds 120
```

앞 2분만 변환합니다.

```text
--render-midi-wav output.wav
```

MIDI 전체를 간이 synth WAV로 렌더합니다.

```text
--summary-json result.json
```

변환 통계를 JSON으로 저장합니다.

## 생성 결과 확인

summary JSON에서 봐야 할 값:

- `notes_used`: 플레이어 노트 수
- `background_notes_used`: BGM 키음으로 들어간 노트 수
- `background_collisions`: BGM 레이어가 부족해서 버린 노트 수
- `keysounds_written`: 생성된 키음 WAV 수
- `notes_cut_by_max_seconds`: 시간 컷으로 버린 노트 수
- `notes_clipped_by_max_seconds`: 시간 컷 경계에서 길이를 줄인 노트 수

키음 WAV가 무음인지 간단히 확인하려면:

```bat
python -c "exec(\"import wave,struct,pathlib\\nfiles=sorted(pathlib.Path('keysounds_2min_exact').glob('ks_*.wav'))\\nsilent=0\\nfor p in files:\\n    w=wave.open(str(p),'rb')\\n    data=w.readframes(w.getnframes())\\n    sw=w.getsampwidth()\\n    peak=0\\n    if sw==2 and data:\\n        vals=struct.unpack('<'+'h'*(len(data)//2),data)\\n        peak=max(abs(v) for v in vals)\\n    elif data:\\n        peak=1\\n    silent += 1 if peak==0 else 0\\n    w.close()\\nprint('count',len(files),'silent',silent)\")"
```

## 한계

- 내장 synth는 테스트용입니다. 실제 FL Studio 악기 소리와 다릅니다.
- FLP 직접 파싱은 프로젝트 구조에 따라 실패할 수 있습니다.
- BMS `#WAVxx` 코드 수에는 한계가 있습니다.
- dense MIDI를 그대로 BMS화하면 사람이 치기 어려운 패턴이 됩니다.
- 투고용 완성도는 수동 패턴 정리와 음원 정리가 필요합니다.
