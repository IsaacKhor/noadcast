import Foundation
import AVFoundation
import SwiftData
import Observation
import os

/// Drives a single episode through the download → detect-ads
/// pipeline. Keeps an in-memory set of in-flight episode IDs so the UI can
/// show progress and so we don't double-enqueue.
///
/// Orchestrated on MainActor because every step `await`s into an actor
/// service (download, upload analysis) where the CPU/IO work
/// actually happens. The orchestrator itself only touches SwiftData.
@MainActor
@Observable
final class ProcessingPipeline {
    static let shared = ProcessingPipeline()
    static let maxQueuedPipelineStarts = 3

    private(set) var activeEpisodes: Set<PersistentIdentifier> = []

    private var modelContainer: ModelContainer?
    private var tasks: [PersistentIdentifier: Task<Void, Never>] = [:]

    var queuedStartCapacity: Int {
        max(0, Self.maxQueuedPipelineStarts - activeEpisodes.count)
    }

    func setModelContainer(_ container: ModelContainer) {
        self.modelContainer = container
    }

    func process(episode: Episode) {
        let id = episode.persistentModelID
        guard !activeEpisodes.contains(id) else { return }
        activeEpisodes.insert(id)

        let task = Task { [weak self] in
            await self?.run(episodeID: id)
            self?.activeEpisodes.remove(id)
            self?.tasks.removeValue(forKey: id)
            self?.startNextQueuedEpisodesIfPossible()
        }
        tasks[id] = task
    }

    func cancel(episodeID: PersistentIdentifier, episodeGUID: String? = nil) {
        tasks[episodeID]?.cancel()
        if let episodeGUID {
            Task {
                await CloudAdDetectionService.shared.cancelTasks(forEpisodeGUID: episodeGUID)
            }
        }
    }

    func isProcessing(episodeID: PersistentIdentifier) -> Bool {
        activeEpisodes.contains(episodeID)
    }

    /// Restart any episode left mid-processing by a previous app run. Called
    /// once at launch from `NoadcastApp.init`.
    ///
    /// If the app was terminated while a cloud upload or `generateContent`
    /// call was in flight, the background `URLSession` keeps running in
    /// `nsurlsessiond`. When we relaunch, our delegate is recreated but the
    /// in-memory continuation that was awaiting the response is gone, so
    /// the response is dropped on the floor and the episode would stay
    /// stuck in `.uploading` / `.detectingAds` forever.
    ///
    /// Recovery: for each in-progress episode, cancel any orphaned tasks
    /// the OS reconstituted into the session (so we don't double-upload),
    /// reset the episode to a step the pipeline can re-enter, and call
    /// `process(episode:)`. The re-enqueue costs a re-upload if we were
    /// interrupted mid-flight, but is correct.
    func recoverPendingEpisodes() async {
        guard let container = modelContainer else { return }
        let context = container.mainContext

        let descriptor = FetchDescriptor<Episode>(
            predicate: #Predicate { $0.isInProgress }
        )
        guard let stuck = try? context.fetch(descriptor), !stuck.isEmpty else { return }
        Log.pipeline.info("Recovering \(stuck.count) interrupted episode(s) after launch")

        for episode in stuck {
            if activeEpisodes.contains(episode.persistentModelID) { continue }

            await CloudAdDetectionService.shared.cancelTasks(forEpisodeGUID: episode.guid)

            switch episode.processingState {
            case .uploading, .detectingAds:
                // The audio-analysis leg was interrupted. If the audio is
                // still on disk, jump straight to analysis by claiming
                // `.downloaded`; otherwise start over from scratch.
                episode.processingState = episode.hasLocalFile ? .downloaded : .new
            case .downloading:
                // Download was interrupted; the partial file is in the
                // background session's scratch dir but we don't know
                // about it. Start over.
                episode.processingState = .new
            default:
                continue
            }
            episode.processingProgress = 0
            episode.processingCurrent = 0
            episode.processingTotal = nil
            episode.processingError = nil
            episode.processingStatusText = nil
            try? context.save()
            process(episode: episode)
        }
    }

    // MARK: - Pipeline

    private func run(episodeID: PersistentIdentifier) async {
        guard let container = modelContainer else { return }
        let context = container.mainContext
        guard let episode = context.model(for: episodeID) as? Episode else { return }

        let title = episode.title
        Log.pipeline.info("Pipeline start — episode=\"\(title, privacy: .public)\" state=\(episode.processingState.rawValue, privacy: .public) hasFile=\(episode.hasLocalFile) markers=\(episode.activeAdMarkerCount)")

        do {
            if !episode.hasLocalFile {
                try await downloadStep(episode: episode, context: context)
            } else {
                Log.pipeline.info("Skipping download for \"\(title, privacy: .public)\" — file already on disk")
            }
            try Task.checkCancellation()

            let settings = AppSettings.current(in: context)
            let aiEnabled = settings.adAnalysisEnabled && (episode.podcast?.aiProcessingEnabled ?? true)
            if aiEnabled {
                try await cloudAnalyzeStep(episode: episode, context: context)
            } else {
                Log.pipeline.info("Skipping ad detection for \"\(title, privacy: .public)\" — disabled on its podcast")
            }

            episode.processingState = .ready
            episode.processingProgress = 1.0
            episode.processingError = nil
            episode.processingStatusText = nil
            try? context.save()
            Log.pipeline.info("Pipeline done — episode=\"\(title, privacy: .public)\"")
        } catch is CancellationError {
            recordCancellation(for: episode, title: title, context: context)
        } catch {
            if Task.isCancelled || (error as? URLError)?.code == .cancelled {
                recordCancellation(for: episode, title: title, context: context)
            } else {
                episode.processingState = .failed
                episode.processingError = error.localizedDescription
                episode.processingStatusText = nil
                try? context.save()
                Log.pipeline.error("Pipeline failed — episode=\"\(title, privacy: .public)\" \(Log.describe(error), privacy: .public)")
            }
        }
    }

    /// Delete/reprocess flows reset an episode to `.new` before the old task
    /// unwinds. Preserve that deliberate reset instead of racing it with a
    /// stale `.failed` write from the cancelled task.
    private func recordCancellation(
        for episode: Episode,
        title: String,
        context: ModelContext
    ) {
        if episode.processingState != .new {
            episode.processingState = .failed
            episode.processingError = "Cancelled."
            episode.processingStatusText = nil
            try? context.save()
        }
        Log.pipeline.notice("Pipeline cancelled — episode=\"\(title, privacy: .public)\"")
    }

    private func startNextQueuedEpisodesIfPossible() {
        guard queuedStartCapacity > 0, let container = modelContainer else { return }
        SubscriptionService.shared.processQueuedEpisodes(context: container.mainContext)
    }

    // MARK: - Steps

    private func downloadStep(episode: Episode, context: ModelContext) async throws {
        episode.processingState = .downloading
        episode.processingProgress = 0
        episode.processingCurrent = 0
        episode.processingTotal = nil
        episode.processingStatusText = "Downloading audio…"
        try? context.save()

        let filename = DownloadService.suggestedFilename(
            for: episode.guid,
            mimeType: episode.audioMimeType
        )
        let stream = DownloadService.shared.download(
            from: episode.audioURL,
            suggestedFilename: filename
        )
        for try await event in stream {
            try Task.checkCancellation()
            switch event {
            case .progress(let p):
                episode.processingProgress = p.fraction
                episode.processingCurrent = Double(p.bytesWritten)
                episode.processingTotal = p.totalBytes.map(Double.init)
            case .completed(let name, let size):
                episode.localFilename = name
                episode.fileSizeBytes = size
                episode.processingState = .downloaded
                episode.processingProgress = 1.0
                episode.processingCurrent = Double(size)
                episode.processingTotal = Double(size)
                try? context.save()
            }
        }
    }

    /// Analysis step: direct backends may upload audio, transcript backends
    /// transcribe first, then Gemini returns skip segments.
    private func cloudAnalyzeStep(episode: Episode, context: ModelContext) async throws {
        guard let fileURL = episode.localFileURL else { return }
        let analysisDuration = await Self.resolvedEpisodeDuration(
            fileURL: fileURL,
            fallback: episode.duration
        )
        if let analysisDuration,
           episode.duration == nil || abs((episode.duration ?? 0) - analysisDuration) > 0.01 {
            // Once downloaded, the local file is authoritative. This also
            // handles feeds with no or stale iTunes duration metadata and
            // dynamically inserted audio whose length differs per download.
            episode.duration = analysisDuration
        }
        let settings = AppSettings.current(in: context)
        let backend = settings.adDetectionBackend
        let provider = settings.adDetectionProvider
        let googleKey = settings.googleAPIKey
        let openRouterKey = settings.openRouterAPIKey
        let thinkingLevel = settings.adDetectionThinkingLevel
        let downsampleBeforeUpload = settings.downsampleAudioBeforeUpload
        let serverHost = settings.adDetectionServerHost
        let serverPort = settings.adDetectionServerPort
        let mimeType = episode.audioMimeType ?? "audio/mpeg"
        let episodeID = episode.persistentModelID
        let container = modelContainer

        setInitialAnalysisProgress(
            episode: episode,
            backend: backend,
            fileURL: fileURL,
            downsampleBeforeUpload: downsampleBeforeUpload
        )
        try? context.save()

        let result = try await CloudAdDetectionService.shared.analyzeFile(
            fileURL: fileURL,
            backend: backend,
            provider: provider,
            googleAPIKey: googleKey,
            mimeType: mimeType,
            openRouterAPIKey: openRouterKey,
            episodeDuration: analysisDuration,
            thinkingLevel: thinkingLevel,
            downsampleBeforeUpload: downsampleBeforeUpload,
            serverHost: serverHost,
            serverPort: serverPort,
            episodeGUID: episode.guid,
            onStage: { stage in
                Task { @MainActor in
                    guard let container,
                          let ep = container.mainContext.model(for: episodeID) as? Episode
                    else { return }
                    // A queue dismissal marks the episode played before
                    // cancelling its transfer. Ignore already-enqueued
                    // progress callbacks from that stale task.
                    guard !ep.isPlayed else { return }
                    switch stage {
                    case .uploading(let sent, let total, let status):
                        if ep.processingState != .uploading {
                            ep.processingState = .uploading
                        }
                        ep.processingCurrent = Double(sent)
                        ep.processingTotal = Double(total)
                        ep.processingProgress = total > 0 ? Double(sent) / Double(total) : 0
                        ep.processingStatusText = status
                    case .transcribing(
                        status: let status,
                        currentSeconds: let currentSeconds,
                        totalSeconds: let totalSeconds
                    ):
                        ep.processingState = .detectingAds
                        if let currentSeconds,
                           let totalSeconds,
                           totalSeconds > 0 {
                            ep.processingCurrent = max(0, min(currentSeconds, totalSeconds))
                            ep.processingTotal = totalSeconds
                            ep.processingProgress = max(0, min(1, currentSeconds / totalSeconds))
                        } else {
                            ep.processingCurrent = nil
                            ep.processingTotal = nil
                            ep.processingProgress = 0
                        }
                        ep.processingStatusText = status
                    case .analyzing(let status):
                        ep.processingState = .detectingAds
                        ep.processingCurrent = nil
                        ep.processingTotal = nil
                        // Indeterminate spinner-style — the LLM call has
                        // no incremental progress to report.
                        ep.processingProgress = 0
                        ep.processingStatusText = status
                    }
                }
            }
        )
        try Task.checkCancellation()

        if let usage = result.usage {
            Self.accumulateUsage(
                usage,
                provider: provider,
                episode: episode,
                into: settings,
                context: context
            )
        }

        let preservedActiveMarkerCount = episode.adMarkers.filter {
            $0.manuallyEdited && !$0.isDeleted
        }.count
        for old in episode.adMarkers where !old.manuallyEdited {
            context.delete(old)
        }
        let sanitizedAds = result.ads.compactMap {
            $0.sanitized(episodeDuration: analysisDuration)
        }
        let droppedAdCount = result.ads.count - sanitizedAds.count
        if droppedAdCount > 0 {
            Log.pipeline.notice("Dropped \(droppedAdCount) ad marker(s) with invalid timestamps for \"\(episode.title, privacy: .public)\"")
        }
        for ad in sanitizedAds {
            let m = AdMarker(
                startSeconds: ad.startSeconds,
                endSeconds: ad.endSeconds,
                summary: ad.summary,
                kind: ad.kind,
                episode: episode
            )
            context.insert(m)
        }
        episode.activeAdMarkerCount = preservedActiveMarkerCount + sanitizedAds.count
        episode.processingProgress = 1.0
        episode.processingStatusText = nil
        try? context.save()
    }

    nonisolated private static func resolvedEpisodeDuration(
        fileURL: URL,
        fallback: Double?
    ) async -> Double? {
        let asset = AVURLAsset(url: fileURL)
        if let time = try? await asset.load(.duration) {
            let measured = time.seconds
            if measured.isFinite, measured > 0 {
                return measured
            }
        }
        guard let fallback, fallback.isFinite, fallback > 0 else { return nil }
        return fallback
    }

    private func setInitialAnalysisProgress(
        episode: Episode,
        backend: AdDetectionBackend,
        fileURL: URL,
        downsampleBeforeUpload: Bool
    ) {
        episode.processingProgress = 0
        episode.processingCurrent = nil
        episode.processingTotal = nil
        switch backend {
        case .geminiFiles:
            if downsampleBeforeUpload {
                episode.processingState = .detectingAds
                episode.processingStatusText = "Preparing audio for Gemini…"
            } else {
                episode.processingState = .uploading
                episode.processingCurrent = 0
                episode.processingTotal = (try? fileURL.resourceValues(forKeys: [.fileSizeKey]).fileSize)
                    .map { Double($0) }
                episode.processingStatusText = "Preparing Gemini upload…"
            }
        case .openRouter:
            episode.processingState = .uploading
            episode.processingStatusText = downsampleBeforeUpload
                ? "Preparing audio for OpenRouter…"
                : "Preparing OpenRouter request…"
        case .whisperServer:
            episode.processingState = .uploading
            episode.processingStatusText = "Preparing server upload…"
        case .appleSpeech:
            episode.processingState = .detectingAds
            episode.processingStatusText = "Preparing Apple local transcription…"
        }
    }

    /// Bump `AppSettings`'s running lifetime token + cost counters using
    /// the provider's posted-rate prices at the moment of the call. We
    /// accumulate the cost as a stored historical figure rather than
    /// recomputing on display, so changing the price constants only
    /// affects future calls.
    private static func accumulateUsage(
        _ usage: TokenUsage,
        provider: AdDetectionProvider,
        episode: Episode,
        into settings: AppSettings,
        context: ModelContext
    ) {
        settings.lifetimeAdDetectionInputTokens += usage.inputTokens
        settings.lifetimeAdDetectionThoughtTokens += usage.thoughtTokens
        settings.lifetimeAdDetectionOutputTokens += usage.outputTokens
        let inputCost = Double(usage.inputTokens) / 1_000_000 * provider.pricePerMTokensAudioInput
        let thoughtCost = Double(usage.thoughtTokens) / 1_000_000 * provider.pricePerMTokensThoughtOutput
        let outputCost = Double(usage.outputTokens) / 1_000_000 * provider.pricePerMTokensOutput
        settings.lifetimeAdDetectionInputCostUSD += inputCost
        settings.lifetimeAdDetectionThoughtCostUSD += thoughtCost
        settings.lifetimeAdDetectionOutputCostUSD += outputCost
        settings.lifetimeAdDetectionCostUSD += inputCost + thoughtCost + outputCost
        let record = TokenUsageRecord(
            provider: provider,
            episodeGUID: episode.guid,
            episodeTitle: episode.title,
            inputTokens: usage.inputTokens,
            thoughtTokens: usage.thoughtTokens,
            outputTokens: usage.outputTokens,
            inputCostUSD: inputCost,
            thoughtCostUSD: thoughtCost,
            outputCostUSD: outputCost
        )
        context.insert(record)
    }
}
