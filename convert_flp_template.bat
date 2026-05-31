@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
  echo Usage: convert_flp_template.bat project.flp keysound_stem.wav [backing.wav]
  exit /b 2
)
if "%~2"=="" (
  echo Usage: convert_flp_template.bat project.flp keysound_stem.wav [backing.wav]
  exit /b 2
)

if "%~3"=="" (
  python daw2bms.py "%~1" -o "%~n1_split.bms" --keysound-source "%~2" --summary-json "%~n1_split.summary.json"
) else (
  python daw2bms.py "%~1" -o "%~n1_split.bms" --keysound-source "%~2" --bgm "%~3" --summary-json "%~n1_split.summary.json"
)
exit /b %errorlevel%
