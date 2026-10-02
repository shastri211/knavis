@echo off
cd /d %~dp0..\backend
python -m app.evaluation.run
pause
