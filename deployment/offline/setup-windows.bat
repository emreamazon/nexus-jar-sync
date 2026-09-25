@echo off
setlocal DisableDelayedExpansion
set "BUNDLE_ROOT=%~dp0"
set "SETUP_HELPER=%BUNDLE_ROOT%tools\setup-windows.ps1"

if not exist "%SETUP_HELPER%" (
  echo Setup helper not found: "%SETUP_HELPER%"
  exit /b 2
)

powershell.exe -NoLogo -NoProfile -File "%SETUP_HELPER%" -BundleRoot "%BUNDLE_ROOT%" %*
set "SETUP_EXIT=%ERRORLEVEL%"
if not "%SETUP_EXIT%"=="0" (
  echo.
  echo Guided setup stopped with exit code %SETUP_EXIT%.
  echo If PowerShell was blocked, contact your administrator about the applicable execution policy.
)
exit /b %SETUP_EXIT%
