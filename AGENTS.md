# Noadcast

A self-hosted podcast system that skips ads, intros, and outros. A Python
server (`src/noadcast/`) polls RSS, downloads and stores audio, transcribes
it with faster-whisper `tiny.en` on the GPU (CUDA float16; word timestamps
joined into sentences), and classifies skippable segments from the
transcript text through OpenRouter. The iOS 26+ app (`ios_app/`) is a thin client: it mirrors the
server's podcasts, episodes, and markers into SwiftData, streams or
downloads the server's audio, and skips segments during playback.
`docs/API.md` is the contract between the two; change both sides together.

## Layout

```
src/noadcast/        Server package (Python 3.13)
  config.py            Settings from secrets.env + env vars
  db/                  SQLite engine, migrations/NNN_*.sql, repo.py (all SQL)
  feeds/               RSS parsing, conditional fetch, refresh + admission
  media/               Audio storage, resumable download, probe, Range serving
  transcribe/          Spawned whisper pool, sentence joiner, word codec
  classify/            Prompts, rendering, providers, sanitising, costs
  pipeline/            Job table, stages, scheduler, retention
  api/                 FastAPI app, auth, sync, routers, web/ dashboard assets
  server.py, cli.py    `noadcast serve` and the other subcommands
tests/               unittest suite; tests/e2e drives the real HTTP API
deploy/              systemd user unit and operating notes
docs/API.md          HTTP contract
scripts/, benchmarks/  Transcription benchmarks and evaluation harness
ios_app/             Xcode project
```

## Server conventions

- Deploy code through Git commits, pushes, and a fast-forward pull on `laurel`.
  Do not copy or archive working-tree files onto the remote checkout.
- Use `.venv/bin/python` (uv-managed, no pip; install with
  `uv pip install --python .venv/bin/python -e .`). Keep caches in the
  project: `TMPDIR`, `HF_HOME`, `XDG_CACHE_HOME`, `UV_CACHE_DIR` under `.cache/`.
- Tests are stdlib `unittest`:
  `.venv/bin/python -m unittest discover -s tests -t .` (~35 s, no network).
- Timestamps go through `noadcast.timeutil.iso()`: fixed-width UTC with
  milliseconds and `Z`, so SQL string comparison is time comparison.

### Invariants that are load-bearing

- **The database is single-threaded.** A `Database` is used only on the
  thread that opened it — the event-loop thread. Every route or dependency
  that touches it is `async def`; FastAPI runs plain `def` handlers in a
  thread pool, where sqlite3 raises.
- **Never `await` inside `with db.write()`.** Do network, disk, and
  subprocess work first, then open the transaction, write, and leave.
- **One sync seq per changed client-visible row**, allocated with
  `tx.next_seq()` inside the same transaction. Podcasts, episodes (any
  marker change bumps its episode), tombstones, and settings carry seqs;
  fine-grained progress and audio access times do not. This is what makes
  `/sync?since=` paging lossless. IDs come from high-water marks and are
  never reused.
- **One server process per data directory, `workers=1`.** The scheduler
  and the seq ordering assume a single writer; `create_app` refuses more.
- **The transcription pool is spawned before uvicorn starts** and only
  attached by the app. Keep `cli.py`'s module-level imports light (the
  standard library and dependency-free modules like `config`, `db.engine`,
  `timeutil`): spawned workers re-import it before anything else runs, and
  numpy must not load before a worker sets its thread counts.
- **Audio responses are never compressed** (it breaks Range); the gzip
  middleware bypasses audio routes. Audio routes must accept `HEAD`.
- **The web shell is public, the data is private.** Only exact `GET|HEAD /`,
  `/web/app.css`, and `/web/app.js` are public for the browser login screen.
  All `/api/v1` routes, including dashboard lists, stats, and settings,
  require the bearer token. The browser keeps it in session storage and sends
  it in the header; never put it in a URL or shipped asset. Keep asset links
  and API requests relative so a reverse-proxy path prefix works. Static
  assets are shipped inside the Python package and use a strict CSP.
- **Migrations are append-only.** Add `db/migrations/NNN_name.sql`; never
  edit one that has been applied.

### Classification

- OpenRouter is the only classification provider. Models are
  `deepseek/deepseek-v4.1-flash` (default), `qwen/qwen3.8-flash`, and
  `openai/gpt-6-luna` (always high reasoning). Select the model in the web
  dashboard or iOS Settings,
  with `NOADCAST_OPENROUTER_MODEL`, or via `POST /episodes/{id}/reanalyze`.
  Offline replay doubles live only under `tests/support/`; historical
  classification rows retain their original provider and model.
- Prompt versions live in `classify/prompts.py` (`segments-v3` default,
  for the `sentences` render format: `[22.24-23.88] A complete sentence.`).
  Every classification row is kept with its
  prompt version, render format, token counts, and cost at the price-table
  version in effect, so comparisons are SQL joins, not re-runs.
- Markers are produced by `classify.sanitize.finalize`: intros start at 0,
  outros extend to the measured audio end only across silence, and
  boundaries snap toward silence midpoints.

## iOS app

SwiftUI, SwiftData, AVFoundation, background `URLSession`. There is no Swift
toolchain on `laurel`; build and test in Xcode on a Mac.

```
ios_app/Noadcast/
  Models/       SwiftData @Model types: server mirrors plus device-local fields
  Networking/   NoadcastAPIClient (actor), DTOs, APIError, server config
                (base URL in UserDefaults, token in the Keychain)
  Sync/         SyncService (orchestration, job polling, background refresh)
                and SyncEngine (@ModelActor that applies /sync pages)
  Downloads/    DownloadManager (background URLSession, resume data) and AudioStorage
  Playback/     PlaybackSourceResolver (local file, stream, or unavailable)
                and AdSkipPlanner (chain skip)
  Migration/    One-time import of the pre-server store and device-state restore
  Services/     PlayerService, SubscriptionService, artwork, search, OPML, reachability
  Views/        SwiftUI views, one folder per tab
  Util/         Small helpers (time formatting, logging, etc.)
  NoadcastApp.swift   App entry: store lifecycle, ModelContainer, service graph
  ContentView.swift   Root TabView
```

The Xcode project uses `PBXFileSystemSynchronizedRootGroup` for both
`Noadcast/` and `NoadcastTests/`: any file added under them is compiled
automatically, with no `project.pbxproj` edit.

### Data flow

1. **Configure**: Settings → Server stores the base URL and token; *Test
   connection* calls `GET /health` then `GET /api/v1/session`.
2. **Sync**: `SyncService` pulls `/api/v1/sync` deltas on launch, on
   foreground, after mutations, on pull-to-refresh, and from a
   `BGAppRefreshTask`. `SyncEngine` upserts mirrors keyed by `serverID` and
   replaces each episode's markers wholesale; the cursor advances in the
   same save as the page.
3. **Progress**: `GET /api/v1/jobs/active` is polled with `If-None-Match`
   (fast on the Status tab, slow when idle, never in the background) and
   held in memory, never written to SwiftData.
4. **Play**: `PlaybackSourceResolver` prefers a local file, else streams the
   server's audio through a signed URL. `PlayerService` snapshots markers
   into `AdRegion`s; `AdSkipPlanner` chain-skips with seek guards that stop a
   short-landing seek on a stream from looping.
5. **Download**: `DownloadManager` fetches server audio on the background
   session with the bearer header, keeps resume data, and reconciles with
   `session.allTasks` at launch.
6. **Played**: the app sends `DELETE /api/v1/episodes/{id}/audio?reason=played`
   so the server cancels unfinished media jobs and frees its copy; failed
   sends are retried from a persisted list. Late audio reads cannot restart
   a played release; a new explicit processing or playback request can.
   Marking played (including finishing playback) always removes local audio
   and queue entries. Each successful sync repairs retained played files and
   reconstructs missed releases from the local played flag and server state.
   An absent local file alone is not evidence that an episode was played.

### Ownership rules

- The server owns podcasts, episodes, job state, and markers; the app never
  edits those mirrors except through the API.
- Playback position, played flags, queue order, download state, and
  per-podcast auto-download and speed are **device-local** and never synced.
  `SyncEngine` must never write them.
- Every sync write is write-if-changed: SwiftData dirties an object on any
  assignment, and blind writes would invalidate `@Query` lists on every poll.
- Identity is `serverID`. `guid` and `feedURL` are not unique (two feeds may
  share a GUID). Never persist a `PersistentIdentifier`.

### Threading rules

- The project sets `SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor`. Top-level
  types are MainActor by default.
- `ModelContext` is not `Sendable`. `SyncEngine` opens a fresh context per
  operation (a long-lived background context goes stale against fields the
  main context writes), and download progress is written on a background
  context.
- Long-running transfers use the background `URLSession`; the app delegate
  forwards system completion callbacks to `DownloadManager`.

### Adding a new tab or view

Drop a SwiftUI file into `Views/<Tab>/`, add it to `ContentView`'s `TabView`,
and keep row bodies from reading high-frequency fields unless the view really
needs live progress (see `EpisodeRow`'s `showProgress`).

### Adding episode metadata

1. **Server-owned**: add it on the server, then to `docs/API.md`, then to
   `EpisodeDTO` (optional, so older servers still decode) and the mirror
   field, applied write-if-changed in `SyncEngine`.
2. **Device-local**: add it to `Episode` with a default, and keep it out of
   `SyncEngine`.
3. Additive schema changes (optional or defaulted properties) migrate
   automatically. For anything else write a `SchemaMigrationPlan`: if the
   container still fails to open, `NoadcastApp` moves the store aside and
   starts empty, which re-syncs the mirror but loses device-local state.

### Product choices

- Episodes stream from the server or play from a downloaded copy; a local
  file always wins.
- Ad analysis is a server setting, globally and per podcast.
- Markers remain visible even when skipping is disabled, and are not applied
  to a local file whose size or hash differs from the server's audio.
- The queue auto-advances to the first playable episode, in queue order.
