@echo off
:: ── Watt Energy — Backend Launcher (Windows) ──
echo Starting Watt backend...
echo.

pip install fastapi uvicorn requests beautifulsoup4 --quiet

echo.
echo  Backend running at http://localhost:8000
echo  Open smart_energy_optimizer_live.html in your browser
echo  Press Ctrl+C to stop
echo.

uvicorn main:app --reload --port 8000
pause
