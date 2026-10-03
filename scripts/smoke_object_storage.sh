#!/usr/bin/env bash
# Smoke test of the object-storage overlay through the proxy (used by CI, runnable locally):
#   S3_SECRET_ACCESS_KEY=ci-secret KNAVIS_PORT=18081 docker compose -f docker-compose.yml -f docker-compose.s3.yml up -d --build --wait
#   S3_SECRET_ACCESS_KEY=ci-secret KNAVIS_PORT=18081 scripts/smoke_object_storage.sh
# Uploads a text file and a spreadsheet, checks they are stored as objects (and not in the data volume), asks a question,
# then deletes the chat and checks the objects are gone.
set -euo pipefail
base="http://127.0.0.1:${KNAVIS_PORT:-18081}"
PYTHON="${PYTHON:-python3}"
compose=(docker compose -f docker-compose.yml -f docker-compose.s3.yml)

json() { "$PYTHON" -c "import sys,json; print(json.load(sys.stdin)$1)"; }
keys() {
  "${compose[@]}" exec -T backend python - <<'PY'
import os, boto3
c = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT_URL"], aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
                 aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"], region_name="us-east-1")
for o in c.list_objects_v2(Bucket=os.environ["S3_BUCKET"]).get("Contents", []):
    print(o["Key"])
PY
}

token=$(curl -fsS -X POST "$base/api/auth/register" -H 'Content-Type: application/json' \
  -d '{"email":"s3smoke-'$$'@example.com","password":"a long smoke test passphrase"}' | json '["token"]')
auth=(-H "Authorization: Bearer $token")
session=$(curl -fsS -X POST "$base/api/sessions" "${auth[@]}" -H 'Content-Type: application/json' -d '{"title":"s3 smoke"}' | json '["id"]')

printf 'Company data must be retained for 90 days after the contract ends.\n' > /tmp/knavis_policy.txt
printf 'channel,converted\nEmail,1\nEmail,0\nSearch,1\n' > /tmp/knavis_sheet.csv
for f in knavis_policy.txt knavis_sheet.csv; do
  curl -fsS -X POST "$base/api/uploads" "${auth[@]}" -F "session_id=$session" -F "file=@/tmp/$f" > /dev/null
done

indexed() { curl -fsS "$base/api/sessions/$session/documents" "${auth[@]}" | "$PYTHON" -c "import sys,json; print(sum(d['status']=='indexed' for d in json.load(sys.stdin)))"; }
for _ in $(seq 1 60); do
  [ "$(indexed)" = "2" ] && break
  sleep 1
done
curl -fsS "$base/api/sessions/$session/documents" "${auth[@]}" | "$PYTHON" -c "
import sys, json
docs = json.load(sys.stdin)
assert len(docs) == 2 and all(d['status'] == 'indexed' for d in docs), docs
print('both documents indexed')"

found=$(keys)
echo "$found"
echo "$found" | grep -q '^uploads/.*_knavis_policy.txt$' || { echo "FAIL: the upload is not in the bucket"; exit 1; }
echo "$found" | grep -q "^tables/$session/.*\.sqlite$" || { echo "FAIL: the spreadsheet table file is not in the bucket"; exit 1; }
local_uploads=$("${compose[@]}" exec -T backend sh -c 'ls /data/uploads | wc -l')
[ "$local_uploads" = "0" ] || { echo "FAIL: $local_uploads upload(s) were also kept in the data volume"; exit 1; }

curl -fsS -X DELETE "$base/api/sessions/$session" "${auth[@]}" -o /dev/null
remaining=$(keys | grep -c "$session\|_knavis_" || true)
[ "$remaining" = "0" ] || { echo "FAIL: $remaining object(s) left after deleting the chat"; keys; exit 1; }
echo "object storage smoke test passed"
