@echo off
cd /d "%~dp0"
echo Checking for required Python package 'twikit'...
python -c "import twikit" 2>nul
if %errorlevel% neq 0 (
    echo Installing twikit...
    pip install twikit
)
python fetch_tweets.py
if %errorlevel% neq 0 (
    echo.
    echo Something went wrong while running fetch_tweets.py.
    echo Verify your X credentials in config.json and try again.
    pause
)
