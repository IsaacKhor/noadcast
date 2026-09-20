# Noadcast

iOS 26+ podcast app that downloads episodes, asks a cloud audio model to
identify ads/intros/outros, and skips those segments during playback. Built
with SwiftUI, SwiftData, AVFoundation, background `URLSession`, and Gemini.

## Architecture

```
Noadcast/
  Models/         SwiftData @Model types
  Services/       Singleton-style actors / observable services
  Views/          SwiftUI views, one folder per tab
  Util/           Small helpers (time formatting, logging, etc.)
  NoadcastApp.swift   App entry, builds ModelContainer + service graph
  ContentView.swift   Root TabView
  Info.plist
  Noadcast.entitlements
```

The Xcode project uses `PBXFileSystemSynchronizedRootGroup`: any file added
under `Noadcast/` is automatically compiled. You do not need to edit
`project.pbxproj` when adding or removing app sources.

## Data flow

1. **Subscribe** -> `FeedService.fetch(feedURL:)` parses an RSS feed, creates a
   `Podcast`, and imports `Episode` rows.
2. **Queue/download** -> `SubscriptionService` and `ProcessingPipeline` decide
   what can start under `AppSettings.autoDownloadPolicy` and `NetworkMonitor`.
   `DownloadService` writes the audio file to Application Support.
3. **Analyze audio** -> `CloudAdDetectionService.analyzeFile(...)` optionally
   down-samples the audio, uploads it through Gemini Files API on a background
   `URLSession`, then calls `generateContent` with a strict segments-only JSON
   schema.
4. **Persist markers** -> `ProcessingPipeline` replaces automatic `AdMarker`
   rows with sanitized `DetectedAd` results and updates
   `Episode.activeAdMarkerCount`.
5. **Play** -> `PlayerService` loads the local file in AVPlayer, snapshots
   markers into `AdRegion`s, and skips segments on periodic playback ticks.
   It also owns lock-screen metadata and remote commands.

## Current Detection Model

- The app stores skip segments, not transcripts.
- `CloudAdDetectionService.segmentsOnlyPrompt` is the live prompt.
- `DetectedAd`, `TokenUsage`, and timestamp sanitization live in
  `AdDetectionModels.swift`.
- `AdDetectionProvider` currently exposes Gemini audio-capable models.
- Token usage and cost records are historical counters, accumulated at the
  provider rates in effect when each call completes.

## Threading Rules

- The project sets `SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor`. Top-level types
  are MainActor by default.
- `ModelContext` is not `Sendable`. Background services that need storage
  should use their own context or hop back to the main context intentionally.
- Long-running transfers use background `URLSession`; the app delegate forwards
  system completion callbacks to the owning service.

## Adding A New Tab / View

Drop a SwiftUI file into `Views/<Tab>/`, add it to `ContentView`'s `TabView`,
and keep row bodies from reading high-frequency fields unless the view really
needs live progress updates.

## Adding Episode Metadata

1. Add the property to `Episode` in `Models/Episode.swift`.
2. SwiftData migrates additive schema changes automatically when the property is
   optional or has a default. For non-trivial migrations, write a
   `SchemaMigrationPlan`.
3. Prefer denormalized scalar fields for data shown in large scrolling lists.

## Product Choices

- Episodes are fully downloaded before playback starts.
- Ad analysis is globally toggleable and can also be disabled per podcast.
- Markers remain visible even when skipping is disabled.
- Queue auto-advances when an episode finishes.
