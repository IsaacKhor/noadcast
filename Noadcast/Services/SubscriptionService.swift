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
        return podcast
    }

    func refresh(podcast: Podcast, in context: ModelContext) async throws {
        let parsed = try await FeedService.shared.fetch(feedURL: podcast.feedURL)
        if podcast.title.isEmpty { podcast.title = parsed.title }
        podcast.author = parsed.author ?? podcast.author
        podcast.summary = parsed.summary ?? podcast.summary
        if podcast.artworkURL == nil { podcast.artworkURL = parsed.artworkURL }
        importEpisodes(parsed.episodes, into: podcast, context: context)
        podcast.lastFetched = .now
        try context.save()
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

    // MARK: - Private

    private func importEpisodes(
        _ parsed: [ParsedEpisode],
        into podcast: Podcast,
        context: ModelContext
    ) {
        let existingGUIDs = Set(podcast.episodes.map(\.guid))
        var newestSeen: Date? = podcast.latestEpisodeAt
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
        }
        if let newestSeen { podcast.latestEpisodeAt = newestSeen }
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
            case .ready, .downloading, .transcribing, .detectingAds:
                continue
            case .new, .downloaded, .failed:
                ProcessingPipeline.shared.process(episode: episode)
            }
        }
    }
}
