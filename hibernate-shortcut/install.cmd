@echo off
rem Zapusk ustanovshchika yarlyka "Gibernaciya". Dvoynoy shchelchok po etomu faylu.
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
