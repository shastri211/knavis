import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"

def run(cmd):
    print("\n>", " ".join(cmd))
    return subprocess.run(cmd, cwd=BACKEND, text=True)

def main():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(BACKEND)

    checks = [
        [sys.executable, "-m", "compileall", "-q", "app"],
        # The whole suite, including the end-to-end golden tests per file type.
        [sys.executable, "-m", "pytest", "-q", "tests"],
    ]

    failures = []
    for cmd in checks:
        p = subprocess.run(cmd, cwd=BACKEND, env=env, text=True)
        if p.returncode != 0:
            failures.append(" ".join(cmd))

    report = {
        "compileall": "ok" if not failures or not any("compileall" in x for x in failures) else "failed",
        "failed_checks": failures,
        "status": "pass" if not failures else "needs_environment_or_code_fix",
    }
    print(json.dumps(report, indent=2))
    return 0 if not failures else 1

if __name__ == "__main__":
    raise SystemExit(main())
