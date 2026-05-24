import Foundation
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

    func cancel(episodeID: PersistentIdentifier) {
        tasks[episodeID]?.cancel()
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

            await CloudTranscriptionService.shared.cancelTasks(forEpisodeGUID: episode.guid)

            switch episode.processingState {
            case .uploading, .detectingAds, .transcribing:
                // The cloud or local-transcription leg was interrupted.
                // If the audio is still on disk, jump straight to the
                // cloud/transcription step by claiming `.downloaded`;
                // otherwise start over from scratch.
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

            let aiEnabled = episode.podcast?.aiProcessingEnabled ?? true
            if aiEnabled {
                try await cloudAnalyzeStep(episode: episode, context: context)
            } else {
                Log.pipeline.info("Skipping ad detection for \"\(title, privacy: .public)\" — disabled on its podcast")
            }

            episode.processingState = .ready
            episode.processingProgress = 1.0
            episode.processingError = nil
            try? context.save()
            Log.pipeline.info("Pipeline done — episode=\"\(title, privacy: .public)\"")
        } catch is CancellationError {
            episode.processingState = .failed
            episode.processingError = "Cancelled."
            try? context.save()
            Log.pipeline.notice("Pipeline cancelled — episode=\"\(title, privacy: .public)\"")
        } catch {
            episode.processingState = .failed
            episode.processingError = error.localizedDescription
            try? context.save()
            Log.pipeline.error("Pipeline failed — episode=\"\(title, privacy: .public)\" \(Log.describe(error), privacy: .public)")
        }
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

    /// File-upload step: upload audio and ask the API to return only skip
    /// segments, without storing any transcript text locally.
    private func cloudAnalyzeStep(episode: Episode, context: ModelContext) async throws {
        guard let fileURL = episode.localFileURL else { return }
        // Initial state: bytes about to go up. We set `.uploading` here
        // rather than `.transcribing` so the UI's progress label and
        // unit-aware formatter (`TimeFormatting.progressDetail`) render
        // the byte counter (`12 MB / 50 MB`) as soon as the row appears.
        episode.processingState = .uploading
        episode.processingProgress = 0
        episode.processingCurrent = 0
        episode.processingTotal = (try? fileURL.resourceValues(forKeys: [.fileSizeKey]).fileSize)
            .map { Double($0) }
        try? context.save()

        let settings = AppSettings.current(in: context)
        let provider = settings.adDetectionProvider
        let googleKey = settings.googleAPIKey
        let downsampleBeforeUpload = settings.downsampleAudioBeforeUpload
        let mimeType = episode.audioMimeType ?? "audio/mpeg"
        let episodeID = episode.persistentModelID
        let container = modelContainer

        let result = try await CloudTranscriptionService.shared.analyzeFile(
            fileURL: fileURL,
            provider: provider,
            googleAPIKey: googleKey,
            mimeType: mimeType,
            downsampleBeforeUpload: downsampleBeforeUpload,
            episodeGUID: episode.guid,
            onStage: { stage in
                Task { @MainActor in
                    guard let container,
                          let ep = container.mainContext.model(for: episodeID) as? Episode
                    else { return }
                    switch stage {
                    case .uploading(let sent, let total):
                        if ep.processingState != .uploading {
                            ep.processingState = .uploading
                        }
                        ep.processingCurrent = Double(sent)
                        ep.processingTotal = Double(total)
                        ep.processingProgress = total > 0 ? Double(sent) / Double(total) : 0
                    case .analyzing:
                        ep.processingState = .detectingAds
                        ep.processingCurrent = nil
                        ep.processingTotal = nil
                        // Indeterminate spinner-style — the LLM call has
                        // no incremental progress to report.
                        ep.processingProgress = 0
                    }
                }
            }
        )

        if let usage = result.usage {
            Self.accumulateUsage(
                usage,
                provider: provider,
                episode: episode,
                into: settings,
                context: context
            )
        }

        for old in episode.transcript { context.delete(old) }
        let preservedActiveMarkerCount = episode.adMarkers.filter {
            $0.manuallyEdited && !$0.isDeleted
        }.count
        for old in episode.adMarkers where !old.manuallyEdited {
            context.delete(old)
        }
        for ad in result.ads {
            let m = AdMarker(
                startSeconds: ad.startSeconds,
                endSeconds: ad.endSeconds,
                summary: ad.summary,
                kind: ad.kind,
                episode: episode
            )
            context.insert(m)
        }
        episode.activeAdMarkerCount = preservedActiveMarkerCount + result.ads.count
        episode.processingProgress = 1.0
        try? context.save()
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
