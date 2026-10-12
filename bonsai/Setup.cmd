@echo off
REM Sets up Bonsai for the Zapit example: downloads Bonsai if needed and
REM restores the packages listed in Bonsai.config from nuget.org.
REM Setup.ps1 lives beside Bonsai.exe because it must run from that folder.
pushd "%~dp0.bonsai"
powershell -ExecutionPolicy Bypass -File ./Setup.ps1
popd
