# daw2bms 빠른 사용법

## 1. 준비

MIDI만 변환하면 추가 설치는 필요 없습니다.

FLP 파일을 직접 읽으려면 한 번만 설치합니다.

```bat
python -m pip install pyflp
```

`EventEnum has no members` 에러가 나면 예전 zip을 쓰는 중일 수 있습니다. 최신 zip의 `daw2bms.py`에는 pyflp 호환 패치가 들어 있습니다. 그래도 FLP 직접 읽기는 Python 3.12를 권장합니다.

```bat
py -3.12 -m pip install pyflp
py -3.12 daw2bms.py "Project_5.flp" -o "song_flp_synth.bms" --max-seconds 120 --player-tracks 12,19,20,21 --background-non-player-to-bgm --background-layers 15 --synth-keysounds --keysound-reuse track-pitch-duration --keysound-dir "keysounds_flp_synth" --summary-json "song_flp_synth.summary.json"
```

## 2. FL Studio에서 파일 뽑기

원본 FLP는 백업한 뒤 진행하세요.

1. `Tools -> Macros -> Prepare for MIDI export`
2. `File -> Export -> MIDI file`로 `.mid` 저장
3. 원래 음색을 쓰려면 `Split mixer tracks`로 악기별 WAV stem도 저장
4. MIDI export 준비 상태로 원본 FLP를 저장하지 마세요

## 3. MIDI 트랙 확인

```bat
python -c "from pathlib import Path; from daw2bms import parse_midi; from collections import Counter; m=parse_midi(Path('Project_5.mid')); print('notes', len(m.notes)); print('track_names', m.track_names); print('tracks', Counter(n.track for n in m.notes).most_common())"
```

여기 나온 트랙 번호를 `--player-tracks`와 `--track-audio-map`에 씁니다.

## 4. 내장 synth로 테스트 변환

```bat
python daw2bms.py "Project_5.mid" -o "song_test.bms" --max-seconds 120 --player-tracks 12,19,20,21 --background-non-player-to-bgm --background-layers 15 --synth-keysounds --keysound-reuse track-pitch-duration --keysound-dir "keysounds_test" --summary-json "song_test.summary.json"
```

이 방식은 음색이 FL과 다릅니다. 패턴 확인용입니다.

## 5. 원곡 재현 우선, 패턴 없음

패턴을 안 짜도 되고 원곡처럼 들리는 것이 우선이면 모든 노트를 BGM 키음 채널로 보냅니다.

```bat
python daw2bms.py "Project_5.mid" -o "song_bgm_only.bms" --max-seconds 120 --all-notes-to-bgm --background-layers 32 --auto-track-audio-dir "stems" --keysound-reuse track-pitch-duration --normalize-keysounds --keysound-dir "keysounds_bgm_only" --summary-json "song_bgm_only.summary.json"
```

## 6. 88키 피아노 샘플 BMS

`s_000.wav`부터 `l_087.wav`까지 피아노 키음 파일이 이미 준비돼 있으면 이 모드를 씁니다.

```bat
python daw2bms.py "piano.mid" -o "piano_template.bms" --piano-bms --all-notes-to-bgm --background-layers 32 --summary-json "piano_template.summary.json"
```

기본 분류는 200ms 미만 `s_`, 200ms 이상 `m_`, 800ms 이상 `l_`입니다. 바꾸려면 `--piano-medium-ms 180 --piano-long-ms 700`처럼 넣습니다.

약한 노트를 빼려면 `--piano-min-velocity 8 --piano-min-channel-volume 8`처럼 씁니다. MIDI 10번 채널은 자동 제외되고, sustain(CC 64)과 sostenuto(CC 66)는 길이 분류에 반영됩니다.

## 7. FL 음색 stem으로 변환

stem WAV 파일명이 MIDI/FLP 트랙 이름과 같으면 자동 매칭을 쓸 수 있습니다.

```bat
python daw2bms.py "Project_5.mid" -o "song_auto_stems.bms" --max-seconds 120 --player-tracks 12,19,20,21 --background-non-player-to-bgm --background-layers 15 --auto-track-audio-dir "stems" --keysound-reuse track-pitch-duration --keysound-dir "keysounds_stems" --summary-json "song_auto_stems.summary.json"
```

FLP를 직접 읽을 때도 같은 방식입니다.

```bat
python daw2bms.py "Project_5.flp" -o "song_flp_auto_stems.bms" --max-seconds 120 --player-tracks 12,19,20,21 --background-non-player-to-bgm --background-layers 15 --auto-track-audio-dir "stems" --keysound-reuse track-pitch-duration --keysound-dir "keysounds_flp_stems" --summary-json "song_flp_auto_stems.summary.json"
```

자동 매칭이 실패하면 직접 매핑합니다.

```bat
python daw2bms.py "Project_5.mid" -o "song_stem_keys.bms" --max-seconds 120 --player-tracks 12,19,20,21 --background-non-player-to-bgm --background-layers 15 --track-audio-map "10=BassGuitar2.wav,12=Hypersaw.wav,13=Orchestral.wav,14=Orchestral_2.wav,19=ePiano.wav,20=Organ1.wav,21=Dream_bell.wav" --keysound-reuse track-pitch-duration --normalize-keysounds --track-gain-map "12=3,19=2" --keysound-dir "keysounds_stems" --summary-json "song_stem_keys.summary.json"
```

WAV 파일명은 실제로 뽑힌 stem 파일명에 맞게 바꾸세요.

`--auto-track-audio-dir`나 `--track-audio-map`에 없는 트랙이 변환 대상이면 에러가 납니다. 모든 배경음까지 키음으로 쓰려면 드럼/베이스 등 배경 트랙 stem도 같이 매핑해야 합니다.

`--normalize-keysounds`는 키음 WAV별 peak를 -3 dB 기준으로 맞춥니다. 키음 소리가 들쭉날쭉하면 켜세요.

`--track-gain-map "12=3,19=-2"`는 12번 트랙을 +3 dB, 19번 트랙을 -2 dB로 보정합니다. 보통화 뒤에 적용됩니다.

완전히 같은 키음 WAV는 기본으로 하나만 남기고 재사용합니다.
