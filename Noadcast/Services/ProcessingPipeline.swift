import Foundation
import SwiftData
import Observation
import os

/// Drives a single episode through the download → transcribe → detect-ads
/// pipeline. Keeps an in-memory set of in-flight episode IDs so the UI can
/// show progress and so we don't double-enqueue.
///
/// Orchestrated on MainActor because every step `await`s into an actor
/// service (download, transcription, ad detection) where the CPU/IO work
/// actually happens. The orchestrator itself only touches SwiftData.
@MainActor
@Observable
final class ProcessingPipeline {
    static let shared = ProcessingPipeline()

    private(set) var activeEpisodes: Set<PersistentIdentifier> = []

    private var modelContainer: ModelContainer?
    private var tasks: [PersistentIdentifier: Task<Void, Never>] = [:]

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
        }
        tasks[id] = task
    }

    func cancel(episodeID: PersistentIdentifier) {
        tasks[episodeID]?.cancel()
    }

    func isProcessing(episodeID: PersistentIdentifier) -> Bool {
        activeEpisodes.contains(episodeID)
    }

    // MARK: - Pipeline

    private func run(episodeID: PersistentIdentifier) async {
        guard let container = modelContainer else { return }
        let context = container.mainContext
        guard let episode = context.model(for: episodeID) as? Episode else { return }

        let title = episode.title
        Log.pipeline.info("Pipeline start — episode=\"\(title, privacy: .public)\" state=\(episode.processingState.rawValue, privacy: .public) hasFile=\(episode.hasLocalFile) transcript=\(episode.transcript.count)")

        do {
            if !episode.hasLocalFile {
                try await downloadStep(episode: episode, context: context)
            } else {
                Log.pipeline.info("Skipping download for \"\(title, privacy: .public)\" — file already on disk")
            }
            try Task.checkCancellation()

            let aiEnabled = episode.podcast?.aiProcessingEnabled ?? true
            if aiEnabled {
                if episode.transcript.isEmpty {
                    try await transcribeStep(episode: episode, context: context)
                } else {
                    Log.pipeline.info("Skipping transcription for \"\(title, privacy: .public)\" — \(episode.transcript.count) segments already cached; will only re-run ad detection")
                }
                try Task.checkCancellation()
                try await detectAdsStep(episode: episode, context: context)
            } else {
                Log.pipeline.info("Skipping transcription + ad detection for \"\(title, privacy: .public)\" — disabled on its podcast")
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

    private func transcribeStep(episode: Episode, context: ModelContext) async throws {
        guard let fileURL = episode.localFileURL else { return }
        episode.processingState = .transcribing
        episode.processingProgress = 0
        episode.processingCurrent = 0
        episode.processingTotal = episode.duration
        try? context.save()

        let episodeID = episode.persistentModelID
        let container = modelContainer
        let segments = try await TranscriptionService.shared.transcribe(
            fileURL: fileURL,
            progress: { current, total in
                Task { @MainActor in
                    guard let container,
                          let ep = container.mainContext.model(for: episodeID) as? Episode
                    else { return }
                    ep.processingCurrent = current
                    ep.processingTotal = total
                    ep.processingProgress = total > 0 ? min(1.0, current / total) : 0
                }
            }
        )

        for old in episode.transcript { context.delete(old) }
        for seg in segments {
            let t = TranscriptSegment(
                startSeconds: seg.startSeconds,
                endSeconds: seg.endSeconds,
                text: seg.text,
                episode: episode
            )
            context.insert(t)
        }
        episode.processingProgress = 1.0
        try? context.save()
    }

    private func detectAdsStep(episode: Episode, context: ModelContext) async throws {
        episode.processingState = .detectingAds
        episode.processingProgress = 0
        episode.processingCurrent = 0
        episode.processingTotal = nil
        try? context.save()

        let settings = AppSettings.current(in: context)
        let transcript = episode.transcript
            .sorted { $0.startSeconds < $1.startSeconds }
            .map {
                TranscribedSegment(
                    startSeconds: $0.startSeconds,
                    endSeconds: $0.endSeconds,
                    text: $0.text
                )
            }

        let provider = settings.adDetectionProvider
        let googleKey = settings.googleAPIKey
        let openAIKey = settings.openAIAPIKey
        let episodeID = episode.persistentModelID
        let container = modelContainer
        let ads = try await AdDetectionService.shared.detectAds(
            in: transcript,
            provider: provider,
            googleAPIKey: googleKey,
            openAIAPIKey: openAIKey,
            progress: { current, total in
                Task { @MainActor in
                    guard let container,
                          let ep = container.mainContext.model(for: episodeID) as? Episode
                    else { return }
                    ep.processingCurrent = Double(current)
                    ep.processingTotal = Double(total)
                    ep.processingProgress = total > 0 ? Double(current) / Double(total) : 0
                }
            }
        )

        for old in episode.adMarkers where !old.manuallyEdited {
            context.delete(old)
        }
        for ad in ads {
            let m = AdMarker(
                startSeconds: ad.startSeconds,
                endSeconds: ad.endSeconds,
                summary: ad.summary,
                episode: episode
            )
            context.insert(m)
        }
        episode.processingProgress = 1.0
        try? context.save()
    }
}
