@echo off
cd /d "%~dp0"
echo Checking for required Python package 'twikit'...
python -c "import twikit" 2>nul
if %errorlevel% neq 0 (
    echo Installing twikit...
    pip install twifork
)
python fetch_tweets.py --priority-only
if %errorlevel% neq 0 (
    echo.
    echo Something went wrong while running fetch_tweets.py in priority mode.
    echo Verify your X credentials in config.json and session.json, and try again.
    pause
)
