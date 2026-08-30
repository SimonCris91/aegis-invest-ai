@echo off
setlocal

cd /d "%~dp0"

if not exist "outputs" mkdir "outputs"

set "AEGIS_CONFIDENCE_PROFILE=V2_B_GUARDED"

echo.
echo AEGIS INVEST AI - V2_B FINAL WINDOWS VALIDATION
echo ==================================================
echo Confidence profile: V2_B_GUARDED
echo Broker execution: DISABLED
echo.
echo Starting validation...
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "& { & '.\.venv\Scripts\python.exe' -m app.main validate-strategies --real-data --timeframe 1D --max-instruments 9 2>&1 | Tee-Object -FilePath '.\outputs\AEGIS-V2B-FINAL-WINDOWS-VALIDATION.txt' }"

set "AEGIS_EXIT_CODE=%ERRORLEVEL%"

echo.
echo ==================================================
echo Validation complete.
echo Exit code: %AEGIS_EXIT_CODE%
echo Output saved to:
echo outputs\AEGIS-V2B-FINAL-WINDOWS-VALIDATION.txt
echo.
pause

endlocal
exit /b %AEGIS_EXIT_CODE%
