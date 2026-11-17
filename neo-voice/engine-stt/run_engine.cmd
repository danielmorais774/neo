@echo off
rem Launcher do neo-stt engine (usado pela Scheduled Task). Redireciona tudo para engine.log.
cd /d "%~dp0"
if not defined NEO_STT_PORT set NEO_STT_PORT=5005
".\.venv\Scripts\python.exe" neo_stt_engine.py >> engine.log 2>&1
