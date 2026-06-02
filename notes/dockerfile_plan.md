# Dockerfile — Build & Runtime Plan

> **Status:** Design plan, not yet implemented.
> **Scope:** Containerize the Flask app (`server.py` + `src/`) for development/
> single-host deployment. Multi-stage build, persistent volumes for uploads and
> Redis, and a lean final image.

This document plans the `Dockerfile`, `.dockerignore`, and `docker-compose.yml`
**before** any of them are written. It exists so the build is deliberate: the
dependency set here is heavy (`torch`, `faiss-cpu`, `transformers`, `easyocr`,
`opencv`), so a naive single-stage build would produce a multi-GB image and
rebuild everything on every source edit.

---

## 1. Goals

1. **Small, reproducible final image** — heavy build tooling and pip caches must
   not leak into the runtime layer.
2. **Fast incremental rebuilds** — editing `server.py` should *not* re-download
   `torch`. Dependency install and source copy live in separate cache layers.
3. **No secrets or bloat in the image** — `.env`, `.git`, docs, caches, and
   uploaded images are excluded via `.dockerignore`.
4. **Non-ephemeral data** — uploaded images and Redis data survive container
   restarts/rebuilds by living on **named volumes / host disk**, not the
   container's writable layer.

---

## 2. `.dockerignore`

Keep the build context tiny and secret-free. Everything listed here is excluded
from the image (and from the build context sent to the daemon), which both
shrinks the image and speeds up `docker build`.

```dockerignore
# ── Secrets — never bake credentials into an image layer ──────────────
.env
.env.*
*.pem
*.key

# ── Version control ──────────────────────────────────────────────────
.git
.gitignore

# ── Docs & notes — not needed at runtime ─────────────────────────────
*.md
notes/

# ── Python caches / build artifacts ──────────────────────────────────
__pycache__/
*.pyc
*.pyo
*.pyd
.pytest_cache/
.mypy_cache/
*.egg-info/

# ── Local virtualenv (rebuilt inside the image) ──────────────────────
venv/
.venv/

# ── Runtime data — lives on volumes, must not be copied into image ───
data/uploads/
data/cache/
data/embeddings/
*.rdb
*.aof

# ── Editor / OS noise ────────────────────────────────────────────────
.claude/
.vscode/
.idea/
.DS_Store
```

**Why these specifically (per the request):**

- **`.env`** — holds `SERPAPI_KEY`, AWS creds, Postgres/Weaviate/Gemini keys,
  and (newly) the Redis URL. Secrets are injected at **runtime** via
  `env_file`/`-e`, never copied into a layer where `docker history` would expose
  them.
- **`*.md` + `notes/`** — pure documentation; zero runtime value, pure bloat.
- **`data/cache/`, `*.rdb`, `*.aof`** — cache/Redis snapshots are regenerated or
  mounted from a volume; copying a stale snapshot into the image is wrong.
- **`data/uploads/`** — user images belong on a persistent volume, not in the
  image.
- **`.git`** — the entire history would otherwise be sent to the daemon and can
  be large; nothing at runtime needs it.

---

## 3. Multi-stage Build

Two stages. The **builder** stage carries compilers, headers, and the pip
download/cache; the **runtime** stage receives only the installed packages and
the application code. This is what keeps the heavy libraries out of the final
layers' build tooling and gives cache-friendly rebuilds.

```dockerfile
# syntax=docker/dockerfile:1

# ───────────────────────── Stage 1: builder ──────────────────────────
FROM python:3.12-slim AS builder

# Build-time system deps for compiling wheels (opencv/easyocr/torch extras).
# These stay in the builder and never reach the final image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
    && rm -rf /var/lib/apt/lists/*

# Install into an isolated venv we can copy wholesale into the runtime stage.
ENV VIRTUAL_ENV=/opt/venv
RUN python -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Copy ONLY requirements first → this layer is cached until requirements change,
# so editing source code never re-triggers the (slow) dependency install.
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && \
    pip install -r requirements.txt

# ───────────────────────── Stage 2: runtime ──────────────────────────
FROM python:3.12-slim AS runtime

# Runtime-only shared libs (e.g. libGL/libglib for opencv-python-headless).
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Bring over the fully-built venv from the builder — no compilers, no pip cache.
ENV VIRTUAL_ENV=/opt/venv
COPY --from=builder /opt/venv /opt/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# Run as a non-root user (security; also makes volume ownership explicit).
RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

# Copy application code last — the most frequently-changed input, so it sits in
# the outermost (cheapest-to-rebust) layer.
COPY --chown=appuser:appuser server.py ./
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser index.html ./

# Mount points for persistent data (declared so the dirs exist + are owned right).
RUN mkdir -p /app/data/uploads /app/data/cache /app/data/embeddings \
    && chown -R appuser:appuser /app/data

USER appuser

EXPOSE 5000

# Dev server today (server.py uses app.run). For anything beyond local dev,
# swap to gunicorn:  CMD ["gunicorn","-w","4","-b","0.0.0.0:5000","server:app"]
# (multi-worker is the reason the trending in-memory fallback note matters —
#  see trending_feature_arch.md §7.)
CMD ["python", "server.py"]
```

**Layer-ordering rationale (cache strategy):**

| Layer                       | Changes…        | Rebuild cost when source edited |
| --------------------------- | --------------- | ------------------------------- |
| `apt-get` system deps       | rarely          | cached                          |
| `pip install requirements`  | on dep bumps    | cached (the expensive one)      |
| `COPY server.py / src/`     | every code edit | cheap — only this rebuilds      |

Because `requirements.txt` is copied and installed **before** the source, a code
change invalidates only the final `COPY` layers — `torch`/`faiss`/`transformers`
are *not* re-downloaded.

> **Note on `server.py`:** it currently binds `app.run(debug=True, port=5000)`.
> Inside a container, Flask must listen on `0.0.0.0`, not the implicit
> `127.0.0.1`. When implementing, either set `app.run(host="0.0.0.0", port=5000)`
> or (preferred) front it with gunicorn as shown above. `debug=True` should be
> off in any shared environment.

---

## 4. Persistent Data — Volumes

Two categories of state must **not** be ephemeral. Today `server.py` writes
uploads to `tempfile.NamedTemporaryFile(..., delete=False)` and `os.unlink`s
them after each request — fine for the current flow, but the request is to make
uploaded images durable on host disk. Both concerns are solved with volumes.

### 4.1 Uploaded images → host-mounted volume

- **In-container path:** `/app/data/uploads/`
- **Backing:** a Docker **named volume** (or a host bind mount for direct disk
  access during development).
- **Effect:** images written there survive `docker restart` / `docker compose
  down && up` and image rebuilds, because the volume lives outside the
  container's writable layer.

> **Code follow-up (out of scope for this plan, noted for the build step):**
> point the upload save path at `/app/data/uploads/` instead of the OS temp dir
> so persisted images actually land on the mounted volume. The reverse-search
> flow still uploads to the public host (litterbox) for SerpAPI; the local copy
> is what we're persisting.

### 4.2 Redis data → snapshot persistence, read on startup

Redis is introduced as the trending cache backend (see
`trending_feature_arch.md` §7). Its data must also persist:

- **In-container path:** `/data` (Redis's default data dir).
- **Backing:** a named volume `redis-data`.
- **Persistence mode:** enable **RDB snapshots** (and/or AOF). On container
  start, Redis **loads the snapshot from the mounted volume**, so a restart
  doesn't cold-start the trending cache — warm data is read back on startup
  exactly as requested.
  - RDB: `save 900 1` style snapshotting → periodic `dump.rdb` on the volume.
  - AOF (optional, more durable): `appendonly yes` → `appendonlydir/` on the
    volume, replayed on startup.

### 4.3 `docker-compose.yml` sketch

Compose is the cleanest way to declare the app + Redis + the two volumes and to
inject secrets from `.env` at runtime (never baked into the image).

```yaml
services:
  web:
    build: .
    ports:
      - "5000:5000"
    env_file:
      - .env                       # secrets injected at runtime, not in image
    volumes:
      - uploads:/app/data/uploads  # persisted user images
      - cache:/app/data/cache      # any on-disk cache artifacts
    depends_on:
      - redis

  redis:
    image: redis:7-alpine
    command: ["redis-server", "--save", "900", "1", "--appendonly", "yes"]
    volumes:
      - redis-data:/data           # snapshot read on startup → warm cache
    # No host port exposed unless needed; web reaches it via the compose network
    # at redis://redis:6379  (this is the REDIS_URL value for in-container runs).

volumes:
  uploads:
  cache:
  redis-data:
```

**Snapshot-read-on-startup flow:**

```
docker compose up
   │
   ├─ redis container starts
   │     └─ mounts redis-data volume → loads dump.rdb / AOF → cache warm
   │
   └─ web container starts
         └─ mounts uploads + cache volumes → prior images still present
         └─ connects to redis://redis:6379 → trending cache already populated
```

---

## 5. `.env` / Secrets Handling

- `.env` is **gitignored already** and is added to **`.dockerignore`** (§2), so
  it is excluded from the build context — secrets never enter an image layer.
- Secrets reach the running container at **runtime** via compose `env_file`
  (or `docker run --env-file .env`).
- The new **`REDIS_URL`** belongs in `.env` (see `trending_feature_arch.md` §3
  for the variable). In-container, it resolves to the compose service name:
  `REDIS_URL=redis://redis:6379/0`. For host-side / local dev runs (no compose),
  it's `redis://localhost:6379/0`.

---

## 6. Build & Run Cheatsheet (for the implementation step)

```bash
# Build the image
docker build -t product-vision-matcher .

# Run the full stack (web + redis + volumes)
docker compose up --build

# Inspect what landed in the image (verify no .env / .git / notes leaked)
docker history product-vision-matcher
docker run --rm product-vision-matcher ls -la /app      # no .env, no notes/

# Confirm volumes persist across a restart
docker compose down          # containers gone, volumes kept
docker compose up            # uploads + redis snapshot still present
```

---

## 7. Implementation Order (when this is built)

Mirrors the project's bottom-up, commit-per-step convention:

1. Add `.dockerignore` (§2). _Commit:_ `add dockerignore to exclude secrets and bloat from build context`
2. Add multi-stage `Dockerfile` (§3); confirm `docker build` succeeds and the
   final image excludes build tooling. _Commit:_ `add multi-stage dockerfile for flask app`
3. Point the upload save path at `/app/data/uploads/` in `server.py`.
   _Commit:_ `persist uploaded images to mounted volume instead of temp dir`
4. Add `docker-compose.yml` with web + redis + named volumes (§4).
   _Commit:_ `add docker compose with redis and persistent volumes`
5. Verify snapshot persistence: upload an image, `compose down`/`up`, confirm
   the image and the warm Redis trending cache both survive.

> Depends on the Redis work in `trending_feature_arch.md` §7 for the `redis`
> service and `REDIS_URL`. The two plans are intended to land together.
