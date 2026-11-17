@echo off
rem Launcher do neo-voice TTS engine (usado pela start destacada). Loga em tts.log.
cd /d "%~dp0"
if not defined NEO_TTS_PORT set NEO_TTS_PORT=5006
".\.venv\Scripts\python.exe" neo_tts_engine.py >> tts.log 2>&1
