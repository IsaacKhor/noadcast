# Noadcast

iOS 26+ podcast app that auto-skips ads by transcribing each episode locally with **SpeechAnalyzer** (Speech framework, iOS 26) and detecting ad segments with **FoundationModels** (Apple Intelligence on-device LLM). Built with SwiftUI + SwiftData.

## Architecture

```
Noadcast/
  Models/         SwiftData @Model types
  Services/       Singleton-style actors / observable services
  Views/          SwiftUI views, one folder per tab
  Util/           Small helpers (time formatting, etc.)
  NoadcastApp.swift   App entry, builds ModelContainer + service graph
  ContentView.swift   Root TabView
  Info.plist
  Noadcast.entitlements
```

The Xcode project uses `PBXFileSystemSynchronizedRootGroup` — **any file added under `Noadcast/` is automatically compiled**. You do not need to edit `project.pbxproj` when adding new sources.

## Data flow

1. **Subscribe** → `FeedService.fetch(rss:)` parses the RSS feed, creates a `Podcast` and inserts `Episode` rows.
2. **Auto-download** → `ProcessingPipeline` watches new episodes and (when allowed by `AppSettings.autoDownloadPolicy` + `NetworkMonitor`) hands them to `DownloadService`.
3. **Transcribe** → On download completion the pipeline calls `TranscriptionService.transcribe(fileURL:)`, which runs `SpeechAnalyzer` with a `SpeechTranscriber` module and writes `TranscriptSegment` rows with `CMTimeRange`-derived start/end seconds.
4. **Detect ads** → `AdDetectionService.detectAds(in:promptOverride:)` chunks the transcript, calls `LanguageModelSession.respond(to:generating:)` with the `AdSegments` `@Generable` schema, and writes `AdMarker` rows.
5. **Play** → `PlayerService` (AVPlayer) loads the local file, publishes time via `AsyncStream`, and on each periodic boundary checks whether the current time is inside an `AdMarker` — if so, seeks past the marker's end. Also drives `MPNowPlayingInfoCenter` + `MPRemoteCommandCenter` for lock-screen controls.

## Key Apple APIs (iOS 26)

### SpeechAnalyzer

```swift
import Speech

let transcriber = SpeechTranscriber(
    locale: .current,
    transcriptionOptions: [],
    reportingOptions: [.volatileResults],
    attributeOptions: [.audioTimeRange]
)
try await ensureModelInstalled(for: transcriber)

let analyzer = SpeechAnalyzer(modules: [transcriber])
let audioFile = try AVAudioFile(forReading: url)

// Stream results concurrently while feeding the analyzer.
async let _ = analyzer.analyzeSequence(from: audioFile)
for try await result in transcriber.results {
    let text = String(result.text.characters)
    let range = result.range            // CMTimeRange
    // result.isFinal == true for stable segments
}
```

Model assets are downloaded once via `AssetInventory.assetInstallationRequest(supporting:)`. Always check `SpeechTranscriber.installedLocales` before downloading.

### FoundationModels

```swift
import FoundationModels

@Generable
struct AdSegment {
    @Guide(description: "Start time in seconds, from the transcript timestamps.")
    let startSeconds: Double
    @Guide(description: "End time in seconds, from the transcript timestamps.")
    let endSeconds: Double
    @Guide(description: "Short description of what is being advertised.")
    let summary: String
}

@Generable
struct AdSegments { let segments: [AdSegment] }

let session = LanguageModelSession(instructions: settings.adDetectionPrompt)
let response = try await session.respond(to: prompt, generating: AdSegments.self)
let ads = response.content.segments
```

Check `SystemLanguageModel.default.availability` before using; gracefully degrade if Apple Intelligence is unavailable on-device.

## Threading rules

- The project sets `SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor`. Top-level types are MainActor by default. Mark CPU/I-O heavy services as `actor` or annotate methods `nonisolated`.
- `ModelContext` is not `Sendable`. Background services that need to write should create a fresh `ModelContext(modelContainer)` on the background task and merge by saving — never pass a context across actors.

## Adding a new tab / view

Drop a SwiftUI file into `Views/<Tab>/`. Add a case to `ContentView`'s `TabView` and update the `AppTab` enum.

## Adding a new piece of episode metadata

1. Add the property to `Episode` in `Models/Episode.swift`.
2. SwiftData migrates additive schema changes automatically when the property is optional or has a default. For non-trivial migrations write a `SchemaMigrationPlan`.
3. Read it where needed — no separate DTO layer.

## Things that are intentionally simple in v1

- No CarPlay scene (lock-screen + Now Playing only).
- Episodes are fully downloaded before playback starts (we wait for transcription + ad detection to finish — see `Wait + show progress` UX).
- Auto-delete after fully played; no per-podcast retention cap.
- One ad-detection prompt for all podcasts, editable in Settings.
