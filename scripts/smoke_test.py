import os, sys, urllib.request, json

base = os.getenv("MRAG_URL", "http://127.0.0.1:8000")
checks = ["/", "/api/health", "/api/health/live", "/api/health/ready", "/api/models"]

failed = False
for path in checks:
    try:
        with urllib.request.urlopen(base + path, timeout=5) as r:
            body = r.read().decode("utf-8")
            print("OK", path, r.status, body[:160])
    except Exception as exc:
        print("FAIL", path, exc)
        failed = True

sys.exit(1 if failed else 0)
