# Deployment

## Local development

1. Create a virtual environment and install `backend/requirements.txt` (Python 3.11 or newer; CI uses 3.13).
2. Copy `.env.example` to `.env` and add the provider keys you want.
3. `scripts/start_backend.ps1`, then `scripts/start_frontend.ps1`, and open http://127.0.0.1:5173.
4. The first account you register takes ownership of any chats created before accounts existed.
   For a purely personal install you can set `AUTH_ENABLED=false` (no sign-in; never do this on a shared host).

## Docker

```bash
cp .env.example .env        # add keys; leave QDRANT_URL empty to use the bundled on-disk vector store
docker compose up --build   # http://127.0.0.1:8080
```

- Two containers: **backend** (FastAPI, one worker, runs as an unprivileged user) and **frontend** (nginx serving the
  built app and forwarding `/api` to the backend, so the browser sees one origin and CORS is not involved).
- The backend is not published to the host. nginx passes the client address on, and the backend trusts it
  (`TRUST_PROXY_HEADERS=true`) for its rate limits.
- Everything that must survive a restart (database, uploads, spreadsheet tables, vectors) is in the `knavis-data` volume.
  `docker compose down` keeps it; `docker compose down -v` deletes it.
- Port and bind address: `KNAVIS_PORT` (default 8080) and `KNAVIS_BIND` (default `127.0.0.1`).
- Legacy Office formats (.doc, .xls, .ppt) need LibreOffice, which the image does not include; modern formats do not.
- CI builds both images, starts the stack and smoke-tests it through the proxy on every change.

## Operating it

- **Administration without e-mail.** There is no mail service, so an operator handles accounts from the command line, where
  the data lives (in Docker: `docker compose exec backend python -m app.admin ...`):

  ```bash
  python -m app.admin users                      # accounts with chats, documents, storage, active sign-ins
  python -m app.admin reset-password EMAIL       # prompts for the new password; ends every sign-in of that account
  python -m app.admin create-user EMAIL          # for servers with ALLOW_REGISTRATION=false
  python -m app.admin revoke-tokens [EMAIL]      # sign one account (or everyone) out
  python -m app.admin delete-user EMAIL --yes    # the account and everything it owns
  python -m app.admin usage                      # totals and the biggest accounts
  ```

  People can change their own password in the app (it ends their other sign-ins).
- **Restarts are safe.** Every ingestion job records how it should run and how often it was started. At start-up, jobs
  that were queued or running are resumed (a job that keeps dying is failed with a message after three starts); OCR,
  figure and embedding results are cached, so nothing already paid for is paid twice. At most `INGESTION_CONCURRENCY`
  documents are processed at once.
- **Schema changes are migrations.** The schema belongs to Alembic (`backend/migrations`) and is brought up to date at
  start-up. After changing a model: `cd backend && alembic revision --autogenerate -m "what changed"`, read the generated
  file, commit it. A test fails if a model and the migrations disagree. Installs that predate Alembic are adopted
  automatically (missing nullable columns are added, then the database is stamped at the baseline) with their data intact.

## Moving to PostgreSQL

SQLite is the default and is fine for a few users on one host. For more writers, use PostgreSQL:

```bash
POSTGRES_PASSWORD=choose-one docker compose -f docker-compose.yml -f docker-compose.postgres.yml up --build
```

or set `DATABASE_URL=postgresql://user:password@host:5432/knavis` for a database you run yourself. The keyword index
uses a `tsvector` table with a GIN index there instead of SQLite FTS5 (same terms, same Hindi handling). To move an
existing SQLite install: create an empty database, then, with the old settings still in place,

```bash
python -m app.admin copy-database postgresql://user:password@host:5432/knavis
```

copies every row (it refuses a database that already has data), and after you set `DATABASE_URL` and restart, the keyword
index rebuilds itself. Uploaded files, spreadsheet tables and vectors stay where they are (data directory, Qdrant; to move the
files to a bucket see "Object storage"). CI runs the whole test suite on PostgreSQL as well as SQLite.

## Large documents and the embedding quota

Dense search embeds 32 chunks per request against the free embedding quota. A 20 MB text file makes about 17,000 chunks, which
would be over 500 requests, so `MAX_EMBED_CHUNKS_PER_DOC` (default 1000, 0 turns it off) caps what one document may spend
without asking. The check counts chunks that still need embedding: text whose vector is cached (the same file uploaded again,
or repeated passages) is free.

A document over the limit stops as `awaiting_confirmation` after its text is saved (it is searchable by keyword at once) and
shows how many passages and requests it would take. `POST /api/documents/{id}/process` with `confirm` embeds everything; `skip`
indexes it without vectors (its details show `embedding.skipped`), and `reindex` asks again later. The decision belongs to the
job: it survives a restart, and a quota wait followed by `retry` does not ask a second time. Hosted OCR or transcription has its
own question (`CONFIRM_ABOVE_CALLS`); a document can be asked both, one after the other. `GET /api/quota` shows used and remaining
requests per provider and an `embedding` summary (remaining requests and chunks, the per-document limit). The built-in
per-minute limits are placeholders; set your own account's real ones with `QUOTA_OVERRIDES`.

## Object storage (optional)

By default uploads (`<data dir>/uploads`) and spreadsheet table files (`<data dir>/tables`) are on the host's disk, which only one
host can use. With an S3-compatible object store they can be shared, so web processes and workers may run on different machines:

```bash
STORAGE_BACKEND=s3
S3_BUCKET=knavis
S3_ENDPOINT_URL=https://<account>.r2.cloudflarestorage.com   # empty for AWS S3
S3_ACCESS_KEY_ID=...                                         # both empty: the AWS default credential chain (IAM role)
S3_SECRET_ACCESS_KEY=...
S3_PATH_STYLE=false                                          # true for most self-hosted servers
S3_PREFIX=knavis                                             # optional: several installs in one bucket
```

Any provider that speaks the S3 API works (AWS S3, Cloudflare R2, Backblaze B2, or a server you run). `S3_CREATE_BUCKET=true`
creates a missing bucket at start-up, and `python scripts/check_config.py --online` reports whether the bucket is reachable. Keep the bucket private: KNAVIS reads and writes it with its own credentials and never
hands out object URLs.

- **How files are referenced.** `documents.path` holds `s3:<key>` for an object and a disk path for anything else, so documents
  uploaded before the switch keep working from the host that has them. `python -m app.admin migrate-storage` (add
  `--delete-local` to remove the copies) uploads those files and rewrites their references; it can be repeated.
- **Extraction** downloads the object to a temporary directory for the length of the job and removes it afterwards.
  Converted copies of legacy Office files are scratch space too.
- **Spreadsheet tables** are one SQLite file per document, stored as an object whose key changes on every write
  (`tables/<chat>/<document>-<token>.sqlite`). The host that answers a question downloads the files it needs once and caches
  them in `<data dir>/tables`; because a key is never overwritten, a cached copy cannot be stale. Several spreadsheets in a chat
  are merged into one local view file for the query. Cached files that nothing refers to are removed at start-up after an hour.
  An install from before this layout (one file per chat) is split into per-document files at its first start.
- **Deleting** a document, a chat or an account deletes its objects (uploads and table files, plus strays under the chat's
  prefix). A bucket that is unreachable never blocks the delete: the rows go and the failure is logged. While it is unreachable,
  uploads answer 503, and start-up logs `Object storage is not usable` instead of refusing to start.
- **Costs.** Every upload is one `PUT`, every ingestion one `GET`, and a host's first spreadsheet question fetches the table
  file. Check your provider's free allowance (R2 and B2 have one) against your traffic.

To try it on one machine, `docker-compose.s3.yml` adds RustFS, a small self-hosted S3-compatible server, and points the backend at it:

```bash
S3_SECRET_ACCESS_KEY=choose-one docker compose -f docker-compose.yml -f docker-compose.s3.yml up --build
# with the worker fleet, add: -f docker-compose.scale.yml -f docker-compose.s3.scale.yml (and POSTGRES_PASSWORD)
```

The bundled server is for trying the arrangement and for CI; for production use a managed bucket (skip the overlay and set the
variables above). MinIO is not used because its project stopped publishing images and was archived in 2026.

The storage tests run the upload, ingest, delete and analytics flows on the disk and on a bucket; the bucket half needs a server and
is skipped without one. To run it locally:

```bash
docker run -d --name knavis-s3-test -p 19000:9000 -e RUSTFS_ACCESS_KEY=knavisdev -e RUSTFS_SECRET_KEY=knavisdev-secret rustfs/rustfs:1.0.1
cd backend && TEST_S3_ENDPOINT=http://127.0.0.1:19000 python -m pytest -q tests/test_object_storage.py
```

(`TEST_S3_ACCESS_KEY` and `TEST_S3_SECRET_KEY` override the keys.) CI starts the same image as a service container for both the
SQLite and the PostgreSQL test jobs, and a separate job builds the object-storage overlay, uploads through the proxy and checks
the bucket (`scripts/smoke_object_storage.sh`).

## Virus scanning (optional)

```bash
docker compose -f docker-compose.yml -f docker-compose.scan.yml up --build
```

adds ClamAV and sets `CLAMAV_HOST=clamav` and `CLAMAV_REQUIRED=true`: every upload is streamed to the scanner (nothing is written
first), an infected file is refused with the signature name, and while the scanner is down or still loading its signatures
(the first start takes a minute or two) uploads are refused with a 503 rather than accepted unscanned. Without
`CLAMAV_REQUIRED` an unreachable scanner is logged and the upload continues. clamd rejects streams over its `StreamMaxLength`,
so keep it above `MAX_UPLOAD_MB` (the overlay sets 100M). Any clamd you already run works: set `CLAMAV_HOST` and `CLAMAV_PORT`.

## Password reset by e-mail (optional)

Set `SMTP_HOST`, `SMTP_FROM` (and `SMTP_USER`/`SMTP_PASSWORD`, `SMTP_PORT`, `SMTP_STARTTLS` as your provider needs) and
`PUBLIC_URL` (where people open the app). The sign-in page then offers "Forgot your password?". Links are single-use, expire
after `RESET_TOKEN_MINUTES`, only the newest works, and the answer is identical for unknown addresses. Resetting ends every
sign-in of the account. Without SMTP the link is not offered and the operator CLI is the way.

## Confirming new accounts' e-mail addresses (optional)

With SMTP configured (see above), set `REQUIRE_EMAIL_VERIFICATION=true`. A new account is created but cannot sign in until
its owner opens the link we mail (it works once, expires after `VERIFICATION_TOKEN_HOURS`, and opening it signs them in).
Signing in earlier gets a clear message and a "send the link again" button; asking again is rate-limited and answers the same for
unknown and already-confirmed addresses. A password-reset link also confirms the address. Accounts that already exist when you
turn this on are treated as confirmed (they were migrated with a confirmation date), and accounts created by an operator with
`python -m app.admin create-user` are confirmed from the start. Chats from before accounts existed go to the first *confirmed*
account, so someone who registers first with an address they do not own cannot take them. Without SMTP the setting has no effect.

## Built-in checks for active content (always on)

Independent of any scanner, every upload is refused if it contains **macros** (Office `vbaProject.bin`, OpenDocument `Basic/`,
legacy Office macro projects), an **embedded program** (`.exe`, `.dll`, `.ps1`, `.jar`, `.lnk` ... inside an Office or
OpenDocument file), or a **PDF with JavaScript or launch actions** (looked for inside compressed PDF objects too). The message
tells the person how to save a clean copy. KNAVIS only reads text and never runs anything in a document, so this is about not
storing and passing on risky files; set `ALLOW_ACTIVE_CONTENT=true` to turn it off. These checks are structural and cannot
recognise known malware, which is what ClamAV (below) adds.

## Scaling beyond one process (one host)

```bash
POSTGRES_PASSWORD=choose-one docker compose -f docker-compose.yml -f docker-compose.scale.yml up --build --scale backend=2 --scale worker=2
```

runs two web processes and two dedicated ingestion workers (`python -m app.worker`) over PostgreSQL, Redis and a Qdrant server:

- **Jobs** are claimed with an atomic update and a short lease that the owner keeps renewing. Each job runs once however many
  workers race for it; if a worker dies, its lease runs out and another takes the job (a job started three times is failed
  with a message). `INGESTION_INLINE=false` makes the web processes only enqueue.
- **Rate limits** are counted in Redis (`REDIS_URL`), so the limit is shared by every web process. If Redis is unreachable
  requests are allowed and the problem is logged.
- **Start-up** is serialised (a lock file for SQLite, a PostgreSQL advisory lock otherwise), so processes starting together
  do not collide on migrations.
- **Vectors** use a Qdrant server (`QDRANT_URL`); the default on-disk Qdrant belongs to a single process.
- **Uploads and spreadsheet tables** live in the data volume, which every backend and worker mounts. That works on one host;
  for several hosts use object storage (see "Object storage": `docker-compose.s3.yml`, `docker-compose.s3.scale.yml`).

Verified with this stack: eight uploads through nginx were all indexed with each job run exactly once across two workers, and
the sign-in limit was shared by the two web processes (429 after the fifth failed attempt in total, not per process).

## Putting it on the internet

The compose file binds to `127.0.0.1` on purpose. To serve other people:

1. Put HTTPS in front of port 8080 (Caddy, a cloud load balancer, or a tunnel). Never expose plain HTTP:
   sign-in tokens and documents would travel in the clear.
2. Set `KNAVIS_BIND=0.0.0.0` only if that proxy runs on another machine; otherwise keep the default and let the proxy
   connect to 127.0.0.1.
3. Decide who may register: leave `ALLOW_REGISTRATION=true` while people sign up, then set it to `false`.
4. Review the limits in `.env.example` (`RATE_LIMIT_*`, `MAX_USER_STORAGE_MB`, `MAX_UPLOAD_MB`) against your disk and the
   free-tier quotas of your providers. Every signed-in user spends the same provider keys.
5. Back up the `knavis-data` volume (it is the whole application state, apart from a bucket or PostgreSQL if you use them).

## What is in place

- Per-user accounts (scrypt password hashes; revocable tokens stored hashed); every chat, document and job is private to
  its owner and other users get 404.
- Rate limits per user (chat, upload) and per address and email (sign-in).
- Upload checks: real file type by content, streamed size limit, decompression-bomb checks for Office files, page and
  pixel limits for PDFs and images, clean file names, caps on documents per chat and storage per user.
- Spreadsheet SQL runs read-only inside an authorizer allowlist with a time limit.
- Complete deletion of a document, a chat or an account (vectors, files, index rows, tables).
- Health endpoints: `/api/health`, `/api/health/live`, `/api/health/ready`.

## Known limits before a larger deployment

These are real and deliberate for a free-tier, single-host design:

- **One host by default.** The default setup is one process (on-disk Qdrant and an in-process rate limiter). The scale overlay
  shares work between processes on one host through the data volume. Spreading across hosts needs PostgreSQL, a Qdrant server,
  Redis and `STORAGE_BACKEND=s3`. That arrangement is tested piece by piece (storage on a real S3 server, jobs and rate limits on
  one host), not as a multi-machine deployment. Back up the bucket (or the data directory) as well as the database.
- **Malware scanning is opt-in.** The built-in active-content checks are always on, but recognising known malware needs
  ClamAV (a signature scanner, about 1 GB of memory and a signature download), so it stays optional. Signatures do not catch
  novel malware, and a scanner does not make a hostile document safe to open elsewhere.
- **E-mail confirmation is optional** and needs SMTP; without it, anyone can register with any address.
- **Lexical evidence gate.** Plausible-sounding questions the documents cannot answer can still reach the model, which
  is then told to abstain. `python -m app.evaluation.run` measures this (see `eval/README.md`).
- **Free-tier terms.** Providers change limits, models and data terms without notice; check them before sending
  sensitive documents, and run `python scripts/check_config.py --probe` after changing a model.
