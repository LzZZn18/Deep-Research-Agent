@echo off
setlocal
cd /d "%~dp0"

echo.
echo  ==========================================================
echo    DeepResearch  -  one-click local dev startup
echo  ==========================================================
echo.

rem ---- preflight checks (non-blocking warnings) ----
if not exist "%~dp0backend\.env" (
    echo  [WARN] backend\.env not found.
    echo         Copy backend\.env.example to backend\.env and fill in API keys.
    echo.
)
if not exist "%~dp0backend\.venv\Scripts\python.exe" (
    echo  [WARN] backend\.venv not found. Run:
    echo         cd backend ^&^& python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    echo.
)
if not exist "%~dp0frontend\node_modules" (
    echo  [WARN] frontend\node_modules not found. Run: cd frontend ^&^& npm install
    echo.
)

rem ---- 1. DB + Redis (Docker) ----
echo  [1/5] Starting DB + Redis (Docker)...
docker compose up -d db redis
if errorlevel 1 (
    echo  [ERROR] Failed to start Docker. Is Docker Desktop running?
    pause
    exit /b 1
)

echo  [2/5] Waiting for DB + Redis to become healthy...
docker compose up -d --wait db redis >nul 2>&1
timeout /t 3 /nobreak >nul

rem ---- 3. Backend (uvicorn) ----
echo  [3/5] Starting backend (uvicorn) on :8000 ...
start "DeepResearch Backend" /d "%~dp0backend" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000"

rem ---- 4. Worker (consumes the research queue) ----
echo  [4/5] Starting worker ...
start "DeepResearch Worker" /d "%~dp0backend" cmd /k ".venv\Scripts\python.exe -m app.worker"

rem ---- 5. Frontend (Vite) ----
echo  [5/5] Starting frontend (Vite) on :5173 ...
start "DeepResearch Frontend" /d "%~dp0frontend" cmd /k "npm run dev"

echo.
echo  Done. Services:
echo    DB         localhost:5432
echo    Redis      localhost:6379
echo    Backend    http://localhost:8000/docs
echo    Frontend   http://localhost:5173   (Vite may pick 5174 if 5173 is busy)
echo.
echo  To stop: close each window, then run "docker compose down"
echo.
pause
