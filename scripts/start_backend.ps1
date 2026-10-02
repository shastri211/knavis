$root = Split-Path -Parent $PSScriptRoot
Set-Location (Join-Path $root 'backend')
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
