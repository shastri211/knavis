@echo off
cd /d "%~dp0\.."
call conda create -n mmrag python=3.11 -y
call conda activate mmrag
pip install -r backend\requirements.txt
if not exist .env copy .env.example .env
echo Setup complete.
