import Foundation
import SwiftData

/// Wires `FeedService` to SwiftData: adds podcasts, refreshes feeds, inserts
/// new episodes, and triggers auto-download via `ProcessingPipeline` when the
/// settings + network conditions allow.
@MainActor
final class SubscriptionService {
    static let shared = SubscriptionService()

    func subscribe(feedURL: URL, in context: ModelContext) async throws -> Podcast {
        let descriptor = FetchDescriptor<Podcast>(
            predicate: #Predicate { $0.feedURL == feedURL }
        )
        if let existing = try? context.fetch(descriptor).first {
            try await refresh(podcast: existing, in: context)
            return existing
        }

        let parsed = try await FeedService.shared.fetch(feedURL: feedURL)
        let podcast = Podcast(
            feedURL: feedURL,
            title: parsed.title,
            author: parsed.author,
            summary: parsed.summary,
            artworkURL: parsed.artworkURL
        )
        context.insert(podcast)
        importEpisodes(parsed.episodes, into: podcast, context: context)
        podcast.lastFetched = .now
        try context.save()
        await ArtworkService.shared.cache(for: podcast)
        try? context.save()
        return podcast
    }

    func refresh(podcast: Podcast, in context: ModelContext) async throws {
        let parsed = try await FeedService.shared.fetch(feedURL: podcast.feedURL)
        if podcast.title.isEmpty { podcast.title = parsed.title }
        podcast.author = parsed.author ?? podcast.author
        podcast.summary = parsed.summary ?? podcast.summary
        // Always pick up updated artwork from the feed — the show might have
        // rebranded since we first subscribed. `ArtworkService.cache(for:)`
        // below diffs against `cachedArtworkSourceURL` and only re-downloads
        // when the URL actually changed.
        if let artwork = parsed.artworkURL {
            podcast.artworkURL = artwork
        }
        importEpisodes(parsed.episodes, into: podcast, context: context)
        podcast.lastFetched = .now
        try context.save()
        await ArtworkService.shared.cache(for: podcast)
        try? context.save()
        // Newly-imported episodes that auto-enqueued in importEpisodes now
        // exist with persisted IDs; tell the pipeline to download/analyze
        // anything in the queue that isn't already ready.
        processQueuedEpisodes(context: context)
    }

    func refreshAll(context: ModelContext) async {
        let podcasts = (try? context.fetch(FetchDescriptor<Podcast>())) ?? []
        for p in podcasts {
            try? await refresh(podcast: p, in: context)
        }
        AppSettings.current(in: context).lastGlobalRefreshAt = .now
        try? context.save()
    }

    func unsubscribe(_ podcast: Podcast, in context: ModelContext) throws {
        for episode in podcast.episodes {
            deleteEpisodeContent(episode, in: context, save: false)
        }
        ArtworkService.shared.deleteCache(for: podcast)
        context.delete(podcast)
        try context.save()
    }

    /// Single entry point for removing an episode's downloaded content from
    /// the device. Called by both the Downloads tab and the Queue tab so the
    /// two stay in sync:
    ///
    /// - Removes the local audio file.
    /// - Clears `localFilename`, `fileSizeBytes`, `playbackPosition`.
    /// - Resets `processingState` to `.new`.
    /// - Deletes existing transcript segments and ad markers — a fresh
    ///   download may have different audio (podcasts using dynamic ad
    ///   insertion change the ads and the file length per request), so the
    ///   cached transcript and ad markers are no longer trustworthy.
    /// - Deletes every `QueueItem` pointing to the episode.
    /// - Unloads the player if it's the episode currently being played.
    func deleteEpisodeContent(
        _ episode: Episode,
        in context: ModelContext,
        save: Bool = true
    ) {
        PlayerService.shared.unloadIfCurrent(episodeID: episode.persistentModelID)

        if let url = episode.localFileURL {
            try? FileManager.default.removeItem(at: url)
        }
        episode.localFilename = nil
        episode.fileSizeBytes = nil
        episode.playbackPosition = 0
        episode.isPlayed = false
        episode.datePlayed = nil
        episode.processingState = .new
        episode.processingProgress = 0
        episode.processingError = nil

        for segment in episode.transcript {
            context.delete(segment)
        }
        for marker in episode.adMarkers {
            context.delete(marker)
        }

        let allItems = (try? context.fetch(FetchDescriptor<QueueItem>())) ?? []
        for item in allItems where item.episode == episode {
            context.delete(item)
        }

        if save {
            try? context.save()
        }
    }

    /// Re-runs the entire AI pipeline (download + ad detection) for one
    /// episode. Wipes the local audio file, cached transcript, and
    /// existing ad markers, then enqueues processing. Preserves any
    /// `QueueItem`s pointing at the episode so the user's queue placement
    /// isn't lost when they ask the system to redo the analysis. Best for
    /// dynamically-ad-inserted feeds where the audio file itself may differ
    /// between downloads.
    func redownloadAndReprocess(_ episode: Episode, in context: ModelContext) {
        ProcessingPipeline.shared.cancel(episodeID: episode.persistentModelID)
        PlayerService.shared.unloadIfCurrent(episodeID: episode.persistentModelID)

        if let url = episode.localFileURL {
            try? FileManager.default.removeItem(at: url)
        }
        episode.localFilename = nil
        episode.fileSizeBytes = nil
        episode.playbackPosition = 0
        episode.isPlayed = false
        episode.datePlayed = nil
        episode.processingState = .new
        episode.processingProgress = 0
        episode.processingCurrent = nil
        episode.processingTotal = nil
        episode.processingError = nil
        for segment in episode.transcript { context.delete(segment) }
        for marker in episode.adMarkers { context.delete(marker) }
        try? context.save()

        ProcessingPipeline.shared.process(episode: episode)
    }

    /// Re-runs ad detection on the **existing** local file (does not
    /// re-download). Useful when the user has changed models and wants
    /// fresh markers without re-fetching audio. Falls back to a full
    /// re-download if the file is no longer on disk. Doesn't unload the
    /// player — playback can keep going against the same audio while AI
    /// re-runs in the background.
    func reanalyzeEpisode(_ episode: Episode, in context: ModelContext) {
        guard episode.hasLocalFile else {
            redownloadAndReprocess(episode, in: context)
            return
        }
        ProcessingPipeline.shared.cancel(episodeID: episode.persistentModelID)

        for segment in episode.transcript { context.delete(segment) }
        for marker in episode.adMarkers { context.delete(marker) }
        episode.processingState = .downloaded
        episode.processingProgress = 0
        episode.processingCurrent = nil
        episode.processingTotal = nil
        episode.processingError = nil
        try? context.save()

        ProcessingPipeline.shared.process(episode: episode)
    }

    /// Adds an episode to the **top** of the queue (just after the
    /// currently-playing episode, if any) so manually-queued episodes are
    /// the next thing to play. Returns `true` if a new `QueueItem` was
    /// inserted, `false` if it was already present.
    @discardableResult
    func addToQueue(_ episode: Episode, in context: ModelContext) -> Bool {
        let descriptor = FetchDescriptor<QueueItem>(
            sortBy: [SortDescriptor(\QueueItem.position)]
        )
        let existing = (try? context.fetch(descriptor)) ?? []
        if existing.contains(where: { $0.episode == episode }) {
            return false
        }

        let playingID = PlayerService.shared.currentEpisodeID
        let playing = existing.first { $0.episode?.persistentModelID == playingID }

        var pos = 0
        if let playing {
            playing.position = pos
            pos += 1
        }
        let newItem = QueueItem(position: pos, episode: episode)
        context.insert(newItem)
        pos += 1
        for item in existing where item !== playing {
            item.position = pos
            pos += 1
        }
        try? context.save()
        processQueuedEpisodes(context: context)
        return true
    }

    // MARK: - Private

    private func importEpisodes(
        _ parsed: [ParsedEpisode],
        into podcast: Podcast,
        context: ModelContext
    ) {
        let existingGUIDs = Set(podcast.episodes.map(\.guid))
        var newestSeen: Date? = podcast.latestEpisodeAt

        // `lastFetched == nil` is the first import for this podcast — i.e.,
        // the initial subscribe. We don't auto-enqueue then, otherwise the
        // user's queue would get flooded with the show's entire archive.
        // On subsequent refreshes, anything new in the feed is genuinely a
        // newly-published episode and should join the up-next queue
        // (gated by the per-podcast `autoDownloadEnabled` switch).
        let isRefresh = podcast.lastFetched != nil
        let autoEnqueue = isRefresh && podcast.autoDownloadEnabled

        // Pre-compute the next queue position once so we can append
        // multiple new episodes in order without re-querying.
        var nextQueuePosition: Int = {
            guard autoEnqueue else { return 0 }
            let descriptor = FetchDescriptor<QueueItem>(sortBy: [SortDescriptor(\QueueItem.position)])
            let existing = (try? context.fetch(descriptor)) ?? []
            return (existing.last?.position ?? -1) + 1
        }()

        for entry in parsed where !existingGUIDs.contains(entry.guid) {
            let ep = Episode(
                guid: entry.guid,
                title: entry.title,
                episodeDescription: entry.description,
                publishedAt: entry.publishedAt,
                duration: entry.duration,
                audioURL: entry.audioURL,
                audioMimeType: entry.audioMimeType,
                podcast: podcast
            )
            context.insert(ep)
            if let pub = entry.publishedAt,
               newestSeen == nil || pub > newestSeen! {
                newestSeen = pub
            }
            if autoEnqueue {
                let item = QueueItem(position: nextQueuePosition, episode: ep)
                context.insert(item)
                nextQueuePosition += 1
            }
        }
        if let newestSeen { podcast.latestEpisodeAt = newestSeen }
        // Caller is responsible for saving and then invoking
        // `processQueuedEpisodes` (idempotent + cheap) so the pipeline only
        // ever looks up *persisted* `PersistentIdentifier`s.
    }

    /// Triggers `ProcessingPipeline` for every queued episode that isn't yet
    /// ready, subject to the auto-download policy. Call this whenever the
    /// queue changes or the network becomes more permissive (e.g. the Queue
    /// tab appears, or the user just added an item).
    func processQueuedEpisodes(context: ModelContext) {
        let settings = AppSettings.current(in: context)
        guard NetworkMonitor.shared.canAutoDownload(under: settings.autoDownloadPolicy) else {
            return
        }
        let queued = (try? context.fetch(FetchDescriptor<QueueItem>())) ?? []
        for item in queued {
            guard let episode = item.episode else { continue }
            switch episode.processingState {
            case .ready, .downloading, .uploading, .transcribing, .detectingAds:
                continue
            case .new, .downloaded, .failed:
                ProcessingPipeline.shared.process(episode: episode)
            }
        }
    }
}
