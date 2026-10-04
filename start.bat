@echo off
REM Deep Researcher - avvio server (indipendente dalla sessione AI)
cd /d "%~dp0"
echo ============================================
echo   Deep Researcher - http://127.0.0.1:8766
echo   Premi Ctrl+C per fermare il server
echo ============================================
.venv\Scripts\python.exe -m uvicorn app.server.app:create_app --factory --host 127.0.0.1 --port 8766
pause
