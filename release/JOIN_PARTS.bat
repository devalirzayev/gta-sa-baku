@echo off
rem Joins the parts into Baku_SA.zip (or open Baku_SA.zip.001 with 7-Zip directly)
cd /d "%~dp0"
copy /b Baku_SA.zip.001+Baku_SA.zip.002 Baku_SA.zip
echo Baku_SA.zip hazirdir / is ready.
pause
