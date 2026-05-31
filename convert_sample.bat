@echo off
setlocal
cd /d "%~dp0"

python daw2bms.py --write-sample-midi examples\demo.mid
if errorlevel 1 exit /b 1

python daw2bms.py --write-sample-wav examples\demo_source.wav
if errorlevel 1 exit /b 1

python daw2bms.py examples\demo.mid -o examples\demo.bms --lane-notes 60,62,64,65,67,69,71 --summary-json examples\demo.summary.json
if errorlevel 1 exit /b 1

python daw2bms.py examples\demo.mid -o examples\demo_split.bms --lane-notes 60,62,64,65,67,69,71 --keysound-source examples\demo_source.wav --summary-json examples\demo_split.summary.json
exit /b %errorlevel%
