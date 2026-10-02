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
index rebuilds itself. Uploaded files, spreadsheet tables and vectors stay where they are (data directory, Qdrant). CI runs
the whole test suite on PostgreSQL as well as SQLite.

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
  spreading across hosts needs a shared filesystem or object storage, which this project does not provide.

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
5. Back up the `knavis-data` volume (it is the whole application state).

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

- **One host.** The default setup is one process (on-disk Qdrant and an in-process rate limiter). The scale overlay shares
  work between processes on one host; uploads and spreadsheet tables live in a data volume that all of them mount, so
  spreading across hosts needs a shared filesystem or object storage that this project does not provide. Back up the data
  directory as well as the database.
- **Scanning is opt-in.** ClamAV catches known malware by signature; it does not make a hostile document safe to open elsewhere.
  There is no e-mail verification of new accounts (only password reset by e-mail, when SMTP is set).
- **Lexical evidence gate.** Plausible-sounding questions the documents cannot answer can still reach the model, which
  is then told to abstain. `python -m app.evaluation.run` measures this (see `eval/README.md`).
- **Free-tier terms.** Providers change limits, models and data terms without notice; check them before sending
  sensitive documents, and run `python scripts/check_config.py --probe` after changing a model.
