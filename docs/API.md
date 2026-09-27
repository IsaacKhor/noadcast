# Noadcast server HTTP API (v1)

This is the contract between the Python server (`src/noadcast/api/`) and the
iOS client (`ios_app/`) and web dashboard. Change it deliberately, in one commit with the affected clients.

## Conventions

- **Base path** `/api/v1` for data and settings; `/health` and the web shell/assets are outside it.
- **Auth**: every `/api/v1` request carries `Authorization: Bearer <token>`,
  compared in constant time. Failure: `401` with `WWW-Authenticate: Bearer`.
  The only other accepted credential is the signed audio URL (below). The raw
  token is never accepted in a query string.
- **JSON** is camelCase. Unknown fields must be ignored by the client, and
  unknown enum strings must decode to a safe default rather than fail.
- **IDs** are integers issued by the server and are meaningful only within
  one `instanceId` (see sync). IDs are never reused within an instance, so a
  deleted podcast's or episode's ID can never come back naming something else.
- **Timestamps** are RFC 3339 UTC with millisecond precision and `Z`, e.g.
  `2026-09-22T18:03:11.123Z`. Clients should still tolerate fractional-second
  variants and a missing fraction.
- **Errors**: `{"error": {"code": "camelCaseCode", "message": "human text"}}`,
  possibly with extra top-level fields documented per endpoint.
  Common codes: `unauthorized` (401), `notFound` (404), `invalidRequest` (400/422),
  `cursorExpired` (410), `rateLimited` (429, with `Retry-After`),
  `audioNotReady` / `audioEvicted` (409, with `Retry-After`), `upstreamFailed` (502),
  `methodNotAllowed` (405), `payloadTooLarge` (413), `internal` (500).
- **Compression**: JSON responses may be gzip-encoded. Audio responses are
  never content-encoded (it would break Range semantics).
- **Playback state is device-local.** The server stores no playback position,
  played flag, or queue order. The only playback-related signal a client sends
  is the retention release `DELETE /episodes/{id}/audio`.

## Enumerations

| Field | Values |
| --- | --- |
| `Episode.state` | `discovered`, `download_pending`, `downloading`, `downloaded`, `transcribe_pending`, `transcribing`, `transcribed`, `classify_pending`, `classifying`, `ready`, `failed` |
| `Episode.audioState` | `absent`, `partial`, `present`, `evicted` |
| `Episode.transcriptState` | `none`, `ready`, `stale`, `failed` |
| `Episode.classifyState` | `none`, `ready`, `stale`, `failed`, `skipped` |
| `AdMarker.kind` | `ad`, `intro`, `outro` |
| `AdMarker.source` | `auto`, `manual` |

`state` is the server pipeline. An episode is *playable from the server* when
`audioState == "present"`, regardless of `state` — markers may still be on
their way (`classifying`), and may arrive while the user is listening.

## Objects

### Podcast

```json
{
  "id": 3,
  "feedUrl": "https://example.com/feed.xml",
  "title": "This American Life",
  "author": "This American Life",
  "summary": "…",
  "artworkUrl": "https://…/art.jpg",
  "language": "en",
  "link": "https://…",
  "autoProcessEnabled": true,
  "adAnalysisEnabled": true,
  "episodeCount": 812,
  "latestEpisodeAt": "2026-09-13T20:00:00.000Z",
  "lastFetchAt": "2026-09-22T18:00:00.000Z",
  "lastFetchError": null,
  "createdAt": "…",
  "updatedAt": "…",
  "seq": 1204
}
```

`autoProcessEnabled`: newly published episodes are downloaded and processed
on the server automatically. `adAnalysisEnabled`: processed episodes are
transcribed and classified. Client-side preferences (auto-download to the
phone, playback speed) are not server fields.

### Episode

```json
{
  "id": 42,
  "podcastId": 3,
  "guid": "tal-646",
  "title": "646: The Secret of My Death",
  "description": "<p>…show notes HTML…</p>",
  "publishedAt": "2026-09-13T20:00:00.000Z",
  "durationSeconds": 3918.94,
  "durationIsMeasured": true,
  "enclosureUrl": "https://…/default.mp3",
  "enclosureType": "audio/mpeg",
  "artworkUrl": null,
  "audioState": "present",
  "audioBytes": 63346363,
  "audioSha256": "b9489445…",
  "audioContentType": "audio/mpeg",
  "state": "ready",
  "error": null,
  "transcriptState": "ready",
  "classifyState": "ready",
  "markerRevision": 2,
  "adMarkers": [
    {"id": 9, "startSeconds": 0.0, "endSeconds": 78.4, "kind": "intro",
     "summary": "Theme music followed by an Acme mattress offer", "source": "auto"}
  ],
  "updatedAt": "…",
  "seq": 9917
}
```

- `durationSeconds` is the decoded duration once the server has the audio
  (`durationIsMeasured: true`), otherwise the feed's declared duration or null.
- `adMarkers` is always the **complete** current marker set for the episode,
  sorted by `startSeconds`, excluding deleted markers. Clients replace their
  local set wholesale; markers have no independent sync identity.
  `markerRevision` increments on every marker change.
  Each auto marker carries the classifier's content `summary` alongside its
  `startSeconds`, `endSeconds`, and `kind`; clients can display this in the
  episode and now-playing views.
- `audioSha256` identifies the exact bytes the markers were computed
  against. A client holding a local file with a different size or hash must
  not apply these markers to it. `audioBytes` and `audioSha256` are kept
  when the server's copy is released or evicted, so they always describe
  the bytes the current markers belong to.

### Settings

```json
{
  "adAnalysisEnabled": true,
  "autoProcessEnabled": true,
  "classifier": "openrouter",
  "classifierModel": "deepseek/deepseek-v4.1-flash",
  "availableClassifiers": {"openrouter": false}
}
```

Global switches; a per-podcast `false` also disables. `availableClassifiers`
reports which providers have server-side keys configured — keys themselves are
never exposed.

## Endpoints

### Web dashboard

`GET|HEAD /` serves the self-hosted dashboard shell. `GET|HEAD /web/app.css`
and `/web/app.js` serve its assets. These three fixed paths are public so the
login screen can load; they contain no server data or credentials. The browser
sends the API token as a bearer header to the same protected API endpoints as
other clients. No token is accepted in a URL. Other methods and paths remain
protected by the normal default-deny authentication middleware.

### `GET /health` (no auth)

```json
{"status": "ok", "version": "0.2.0", "apiVersion": 1, "instanceId": "0bc1…",
 "authRequired": true,
 "capabilities": ["sync", "jobsActive", "audioUrl", "releaseAudio", "usage", "opml"]}
```

Clients feature-detect by `capabilities`.

### `GET /api/v1/session`

`200 {"authenticated": true, "serverTime": "…"}`. Cheap token check for a
"Test connection" button.

### `GET /api/v1/sync?since=<seq>&limit=<n>`

Delta sync. `since` defaults to 0 (full sync); `limit` defaults to 200, max 1000.

```json
{
  "instanceId": "0bc1…",
  "podcasts": [Podcast, …],
  "episodes": [Episode, …],
  "deletions": [{"entity": "podcast", "id": 7}, {"entity": "episode", "id": 99}],
  "settings": Settings | null,
  "nextSince": 9917,
  "hasMore": false,
  "serverTime": "…"
}
```

Server semantics (these guarantee no row is ever skipped):

1. Every podcast change, episode change (including any change to its
   markers), deletion (tombstone), and settings change is stamped with a
   unique, strictly increasing `seq`.
2. Let *R* be every podcast, episode, tombstone, and settings row with
   `seq > since`. The page cutoff is the `limit`-th smallest seq in *R*, or the
   largest seq in *R* if there are fewer. The page contains every row of *R*
   with `seq <= cutoff`.
3. **Referential closure**: every returned episode's podcast is also
   included, even if that podcast's seq is above the cutoff. Clients apply
   podcasts before episodes, and deletions last; duplicates across pages are
   expected and harmless because applies are idempotent upserts.
4. `settings` is the complete settings object if any settings row is in the
   page (always on `since=0`), else null.
5. `nextSince` = cutoff; `hasMore` = rows exist above the cutoff. Clients loop
   while `hasMore`, and persist `nextSince` only after a page is fully applied.
6. Deletions: `podcast` means the podcast and all its episodes are gone
   (clients cascade locally); `episode` means just that episode.
7. `410 {"error": {"code": "cursorExpired"}}` if `since` predates the
   tombstone retention window. The client then does a full sync from 0 and,
   after the last page, removes local mirror rows that were not seen.
8. If `instanceId` differs from the one the client last saw, the database was
   recreated: IDs are meaningless, so the client wipes its mirrors and
   full-syncs (device-local state can be re-attached by `guid` + `feedUrl`).

Fine-grained progress does **not** bump seq (see `/jobs/active`); pipeline
state transitions do.

### `GET /api/v1/jobs/active`

Progress for in-flight episodes. Supports `ETag` / `If-None-Match` → `304`.

```json
{"items": [
  {"episodeId": 42, "jobId": 7, "state": "transcribing", "jobState": "running",
   "stage": "transcribe", "current": 1200.0, "total": 3918.9,
   "statusText": "Transcribing 20:00 of 65:19", "updatedAt": "…"}
]}
```

Units: `download` stage in bytes, `transcribe` in audio seconds, `classify`
indeterminate (null). When an episode leaves this list its final state
arrives via `/sync`.

### Podcasts

- `GET /api/v1/podcasts` → `200 {"items": [Podcast, …]}` in title order.
- `POST /api/v1/podcasts` `{"feedUrl": "…", "autoProcessEnabled"?: bool, "adAnalysisEnabled"?: bool, "initialBackfillCount"?: int}`
  → `201 {"podcast": Podcast}` after fetching and parsing inline (20 s
  budget); `200 {"podcast": Podcast}` if already subscribed (idempotent);
  `202 {"podcast": Podcast, "jobId": n}` if the fetch outlived the budget and
  continues in the background; `422 invalidFeed`; `502 upstreamFailed`.
  First subscribe admits only the newest `initialBackfillCount` (default 1)
  episodes for processing; the rest of the archive is `discovered`.
- `PATCH /api/v1/podcasts/{id}` `{"autoProcessEnabled"?: bool, "adAnalysisEnabled"?: bool}` → `200 Podcast`.
- `DELETE /api/v1/podcasts/{id}` → `204`. Deletes its episodes, transcripts, markers, and audio.
- `POST /api/v1/podcasts/{id}/refresh` → `202 {"jobId": n}`.
- `POST /api/v1/refresh` → `202 {"jobIds": [n, …]}` (all podcasts).

### Episodes

- `GET /api/v1/episodes?podcastId=&state=&q=&limit=50&offset=0` →
  `200 {"items": [Episode, …], "total": n, "limit": n, "offset": n}`.
  Ordered by publication time descending, then ID descending. `q` matches
  episode titles (ASCII case insensitive); `state` matches the exact pipeline state.
  Filters are optional; `limit` is 1–100, `offset` is nonnegative. Each item
  has the same complete marker set as episode detail. An empty page may still
  have a nonzero `total` when the offset is past the last match.
- `GET /api/v1/episodes/{id}` → `200 Episode`.
- `POST /api/v1/episodes/{id}/process` → `202 {"jobId": n | null}`. Ensures
  the audio is on the server (priority download) and, if analysis is enabled,
  transcribed and classified. Idempotent; `jobId` is null when nothing is
  left to do. Clients call this when the user plays or downloads an episode
  whose `audioState` is not `present`.
- `POST /api/v1/episodes/{id}/reanalyze` `{"provider"?: "openrouter", "model"?: str, "thinking"?: str, "retranscribe"?: bool}`
  → `202 {"jobId": n}`; `422` if the provider has no server-side key. Keeps prior
  classifications for comparison. One classify job runs per episode at a time, so a
  second request while one is live folds into it.
- `GET /api/v1/episodes/{id}/transcript?format=sentences|words|text` →
  `200 {"episodeId", "modelId", "language", "durationSeconds", "joinerVersion", "sentences": [{"index", "startSeconds", "endSeconds", "text", "flags"}]}`
  (`words`: adds `"words": [{"start","end","word","probability"}]`; `text`: `text/plain`).
- `GET /api/v1/episodes/{id}/classifications` → `200 {"items": [{"id", "provider", "model", "promptVersion", "renderFormat", "isActive", "inputTokens", "thoughtTokens", "outputTokens", "totalCostUsd", "latencyMs", "createdAt", "segments": [...]}]}`.
  Each `segments` entry contains `startSeconds`, `endSeconds`, `kind`, and
  a content `summary`. Production classifications use `segments-v3` with
  `renderFormat: "sentences"`; the model sees one joined sentence per line,
  formatted `[22.24-23.88] A complete sentence.`. Pause and length fragments
  are coalesced through punctuation when available. Older prompt and render
  formats remain available for evaluation.

### Audio

- `POST /api/v1/episodes/{id}/audio-url` →
  `200 {"path": "/api/v1/episodes/42/audio?exp=1790000000&sig=…", "url": "http://host:8765/api/v1/episodes/42/audio?exp=…&sig=…", "expiresAt": "…"}`.
  Clients should resolve `path` against their configured base URL. If the
  audio is not on the server: `409 {"error": {"code": "audioNotReady" | "audioEvicted"}, "jobId": n}`
  with `Retry-After`, and a priority download is enqueued.
- `GET|HEAD /api/v1/episodes/{id}/audio` — authenticated by the bearer
  header (downloads) **or** `?exp=<unix>&sig=<hex>` where
  `sig = HMAC-SHA256(signingSecret, "<episodeId>\n<exp>")` (AVPlayer, which
  cannot send headers). Always `Accept-Ranges: bytes`, `ETag` (the quoted
  `audioSha256`), `Content-Type`, `Content-Length`, `Last-Modified`. Single ranges
  → `206` with `Content-Range`; unsatisfiable → `416` with
  `Content-Range: bytes */<size>`; multi-range → `200` full body;
  `If-Range` mismatch → `200`; `If-None-Match` hit → `304`. `HEAD` returns
  identical headers with no body. Missing audio normally queues a priority
  download and returns `409` as above, except after a played release.
- `DELETE /api/v1/episodes/{id}/audio?reason=played|manual` → `204`. The
  **retention release**: the client sends `reason=played` when the user
  finishes or marks an episode played. This cancels pending and running
  download, transcription, and classification jobs for that episode, clears
  progress, and deletes complete or partial server audio, including when the
  audio was never downloaded. The episode leaves the active job list and will
  not be automatically resumed after a server restart. Transcript, markers,
  and classification history remain. The played flag itself is device-local.
  Repeating the request is harmless; an explicit later process, reanalysis,
  or audio request may start work again. `reason=manual` only releases audio;
  when a media job is live, that release is deferred until the job finishes.
  A later explicit audio request re-downloads (`409` meanwhile).
  After a `reason=played` release, late `GET|HEAD` requests (including an
  already-minted signed URL) return `409` with `jobId: null` and do not queue
  a download or redirect to the enclosure. The played-release stop intent is
  kept server-side until an explicit `POST /process`, `POST /audio-url`,
  `POST /reanalyze`, or job retry requests new work. This protects against
  stale player and background transfer requests. If a re-download yields
  different bytes (dynamic ad insertion), the server
  re-transcribes and reclassifies before markers are trusted again.

### Settings, usage, OPML

- `GET /api/v1/settings` → `200 Settings`; `PATCH /api/v1/settings`
  `{"adAnalysisEnabled"?, "autoProcessEnabled"?, "classifier"?, "classifierModel"?}` → `200 Settings`.
  `classifier` accepts only `openrouter`; `OPENROUTER_API_KEY` is required on the server.
  `classifierModel` (and reanalyze `model`) accepts three presets:
  `deepseek/deepseek-v4.1-flash` (default), `qwen/qwen3.8-flash`, and
  `openai/gpt-6-luna` (always high reasoning effort). Reasoning is determined
  by the preset, so no separate settings field is needed.
  `NOADCAST_OPENROUTER_MODEL` overrides the default model. Existing historical
  classifications retain their original provider, model, markers, and costs.
- `GET /api/v1/usage?days=30` →
  `200 {"days": [{"date": "2026-09-22", "calls", "inputTokens", "thoughtTokens", "outputTokens", "costUsd"}], "byModel": [{"provider", "model", "calls", "inputTokens", "thoughtTokens", "outputTokens", "costUsd"}], "totals": {…}}`.
  OpenRouter costs use the billed `usage.cost` reported by the provider, summed
  across requests and stored with price-table version `openrouter-reported-v1`.
  Its input/thought/output cost breakdown is unavailable (stored as zero), while
  the total remains billed cost. If the response omits billed cost, the server
  falls back to its price table for known models.
  `thoughtTokens` records separately reported reasoning usage. Historical
  provider rows preserve their original token accounting.
- `POST /api/v1/opml` (body: OPML XML) → `200 {"added": [Podcast], "existing": [Podcast], "failed": [{"feedUrl", "error"}]}`.
  Subscribes without inline fetches; refreshes are enqueued.
- `GET /api/v1/opml` → OPML export (`text/x-opml`).

### Operations

- `GET /api/v1/jobs?state=&kind=&limit=` → `200 {"items": [Job]}`.
- `POST /api/v1/jobs/{id}/retry` → `202`; `DELETE /api/v1/jobs/{id}` → `204` (cancel).
- `GET /api/v1/admin/stats` → pool, queue depths and oldest pending age,
  worker memory, disk used/free, audio bytes, 30-day spend by provider/model,
  episode counts per state, recent failures, cross-feed GUID collisions.
  `disk.usedBytes` describes the whole filesystem that contains the data
  directory; `audio.storedBytes` is the audio recorded as present by Noadcast.
