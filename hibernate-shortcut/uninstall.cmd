@echo off
rem Udalenie yarlyka "Gibernaciya" s rabochego stola.
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Uninstall %*
