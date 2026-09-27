import Foundation
import SwiftData
import os

nonisolated struct OPMLImportSummary: Sendable, Equatable {
    let added: Int
    let existing: Int
    let failed: Int

    var message: String {
        if added == 0 && existing == 0 && failed == 0 {
            return "No podcast feeds were found in the OPML file."
        }
        var parts = ["Added \(added)."]
        if existing > 0 {
            parts.append("Skipped \(existing) already subscribed.")
        }
        if failed > 0 {
            parts.append("\(failed) couldn't be subscribed.")
        }
        return parts.joined(separator: " ")
    }
}

/// Podcast and episode actions against the Noadcast server, plus the
/// device-local queue and download bookkeeping. Keeps its name and most
/// signatures from the on-device-RSS era to limit call-site churn; the
/// bodies now go through `NoadcastAPIClient` and `SyncService`.
///
/// Rows for podcasts/episodes are only ever *inserted* by `SyncEngine`
/// (mutation responses are applied through it too), so two contexts never
/// race to insert the same `serverID`.
@MainActor
final class SubscriptionService {
    static let shared = SubscriptionService()

    private let releasePlayedAudio: @MainActor (Int) -> Void
    private let cancelPendingRelease: @MainActor (Int) -> Void
    private let cancelLocalTransfer: @MainActor (Int) -> Void
    private let cancelServerAudioRequest: @MainActor (Int) -> Void

    init(
        releasePlayedAudio: @escaping @MainActor (Int) -> Void = { SyncService.shared.releaseAudio(episodeServerID: $0) },
        cancelPendingRelease: @escaping @MainActor (Int) -> Void = { PendingReleaseStore.remove($0) },
        cancelLocalTransfer: @escaping @MainActor (Int) -> Void = { DownloadManager.shared.cancelTransfer(serverID: $0, discardResumeData: true) },
        cancelServerAudioRequest: @escaping @MainActor (Int) -> Void = { DownloadManager.shared.cancelServerAudioRequest(serverID: $0) }
    ) {
        self.releasePlayedAudio = releasePlayedAudio
        self.cancelPendingRelease = cancelPendingRelease
        self.cancelLocalTransfer = cancelLocalTransfer
        self.cancelServerAudioRequest = cancelServerAudioRequest
    }

    private var sync: SyncService { SyncService.shared }
    private var api: NoadcastAPIClient { SyncService.shared.api }

    // MARK: - Podcasts

    /// `POST /api/v1/podcasts` (idempotent). The podcast row is mirrored at
    /// once; its episodes arrive through the follow-up syncs.
    func subscribe(feedURL: URL, in context: ModelContext) async throws -> Podcast? {
        guard APIConfiguration.isConfigured else { throw APIError.notConfigured }
        let response = try await api.subscribe(feedURL: feedURL.absoluteString)
        await sync.applyPodcasts([response.podcast])
        await sync.syncNow(.mutation)
        sync.scheduleFollowUpSyncs()
        return fetchPodcast(serverID: response.podcast.id, in: context)
    }

    /// Pull-to-refresh on one podcast: ask the server to re-fetch the feed,
    /// then sync (now and a few times shortly after, since the fetch runs
    /// asynchronously on the server).
    func refresh(podcast: Podcast, in context: ModelContext) async {
        guard APIConfiguration.isConfigured else { return }
        let serverID = podcast.serverID
        do {
            try await api.refreshPodcast(id: serverID)
        } catch {
            Log.feed.notice("Refresh request failed: \(error.localizedDescription, privacy: .public)")
        }
        await sync.syncNow(.pullToRefresh)
        sync.scheduleFollowUpSyncs()
    }

    /// Requests an immediate server fetch for every feed, then syncs the
    /// mirror. The timestamp records acceptance; feed jobs finish asynchronously.
    func refreshAll(context: ModelContext) async throws {
        guard APIConfiguration.isConfigured else { throw APIError.notConfigured }
        try await api.refreshAll()
        let settings = AppSettings.current(in: context)
        settings.lastGlobalRefreshAt = .now
        try? context.save()
        await sync.syncNow(.pullToRefresh)
        sync.scheduleFollowUpSyncs()
    }

    /// `DELETE /api/v1/podcasts/{id}` first (not optimistic: an offline
    /// unsubscribe would otherwise silently come back), then the local rows
    /// with their files, queue items and artwork.
    func unsubscribe(_ podcast: Podcast, in context: ModelContext) async throws {
        let serverID = podcast.serverID
        try await api.deletePodcast(id: serverID)
        guard let podcast = fetchPodcast(serverID: serverID, in: context) else { return }
        let descriptor = FetchDescriptor<Episode>(predicate: #Predicate<Episode> { $0.podcastServerID == serverID })
        let episodes = (try? context.fetch(descriptor)) ?? []
        let queueItems = (try? context.fetch(FetchDescriptor<QueueItem>())) ?? []
        for episode in episodes {
            PlayerService.shared.unloadIfCurrent(episodeID: episode.persistentModelID)
            DownloadManager.shared.cancelTransfer(serverID: episode.serverID, discardResumeData: true)
            if let url = episode.localFileURL {
                try? FileManager.default.removeItem(at: url)
            }
            for item in queueItems where item.episode == episode {
                context.delete(item)
            }
        }
        ArtworkService.shared.deleteCache(for: podcast)
        context.delete(podcast)
        try context.save()
    }

    /// `POST /api/v1/opml` with the picked file. Validated locally first
    /// for a clearer error than a server 422.
    func importOPML(from url: URL, in context: ModelContext) async throws -> OPMLImportSummary {
        guard APIConfiguration.isConfigured else { throw APIError.notConfigured }
        let didStart = url.startAccessingSecurityScopedResource()
        defer {
            if didStart {
                url.stopAccessingSecurityScopedResource()
            }
        }
        let data = try Data(contentsOf: url)
        let entries = try await OPMLService.shared.parse(data: data)
        guard !entries.isEmpty else {
            return OPMLImportSummary(added: 0, existing: 0, failed: 0)
        }
        let result = try await api.importOPML(data)
        await sync.applyPodcasts(result.added + result.existing)
        await sync.syncNow(.mutation)
        sync.scheduleFollowUpSyncs()
        return OPMLImportSummary(added: result.added.count, existing: result.existing.count, failed: result.failed.count)
    }

    // MARK: - Episodes

    /// Single entry point for removing an episode's downloaded content from
    /// the device. Called by both the Status tab and the Queue tab so the
    /// two stay in sync:
    ///
    /// - Cancels any transfer and removes the local audio file.
    /// - Deletes every `QueueItem` pointing to the episode.
    /// - Unloads the player if it's the episode currently being played.
    /// - Optionally records the episode as played (a deliberate dismissal
    ///   from the queue), which also sends the retention release so the
    ///   server may delete its copy.
    ///
    /// Markers are server-owned and stay even if the server releases its
    /// audio copy; a later download can fetch the same episode again.
    func deleteEpisodeContent(
        _ episode: Episode,
        in context: ModelContext,
        markAsPlayed: Bool = false,
        save: Bool = true
    ) {
        PlayerService.shared.unloadIfCurrent(episodeID: episode.persistentModelID)
        cancelLocalTransfer(episode.serverID)
        cancelServerAudioRequest(episode.serverID)

        if let url = episode.localFileURL {
            try? FileManager.default.removeItem(at: url)
        }
        if episode.localFilename != nil {
            episode.localFilename = nil
        }
        if episode.fileSizeBytes != nil {
            episode.fileSizeBytes = nil
        }
        if episode.localAudioSha256 != nil {
            episode.localAudioSha256 = nil
        }
        episode.setDownloadState(.idle)
        if episode.downloadIsUserInitiated {
            episode.downloadIsUserInitiated = false
        }
        episode.downloadRequestedAt = nil
        episode.downloadError = nil
        if episode.downloadProgress != 0 {
            episode.downloadProgress = 0
        }
        episode.downloadedBytes = nil
        episode.downloadTotalBytes = nil

        if markAsPlayed {
            episode.playbackPosition = 0
            episode.isPlayed = true
            episode.datePlayed = .now
        }

        let allItems = (try? context.fetch(FetchDescriptor<QueueItem>())) ?? []
        for item in allItems where item.episode == episode {
            context.delete(item)
        }

        if save {
            try? context.save()
        }
        if markAsPlayed {
            releasePlayedAudio(episode.serverID)
        }
    }

    /// Repairs content retained by an older app or an interrupted cleanup.
    /// The played flag is the durable removal intent; preserve its history
    /// and let sync reconcile the server release separately.
    func cleanUpPlayedContent(episodeServerIDs: [Int], in context: ModelContext) {
        for batch in SyncEngine.batches(of: episodeServerIDs) {
            let descriptor = FetchDescriptor<Episode>(
                predicate: #Predicate<Episode> { batch.contains($0.serverID) && $0.isPlayed }
            )
            for episode in (try? context.fetch(descriptor)) ?? [] {
                // The user may have revived it since the sync snapshot.
                guard episode.isPlayed else { continue }
                deleteEpisodeContent(episode, in: context, save: false)
            }
        }
        if context.hasChanges {
            try? context.save()
        }
    }

    /// User-initiated download to the device (bypasses the auto-download
    /// network policy).
    func download(_ episode: Episode, in context: ModelContext) {
        reviveAfterUserRequest(episode, in: context)
        DownloadManager.shared.enqueue(episode, userInitiated: true)
    }

    func cancelDownload(_ episode: Episode) {
        DownloadManager.shared.cancel(episode)
    }

    /// Retry after a failure on either axis: a device download failure
    /// downloads again; a server pipeline failure asks the server to process
    /// the episode again (`POST /process`, idempotent).
    func retry(_ episode: Episode, in context: ModelContext) {
        if episode.downloadState == .failed {
            download(episode, in: context)
        }
        if episode.serverState == .failed {
            requestServerProcessing(episode, in: context)
        }
    }

    /// Ensures the server has the audio and, if analysis is on, markers.
    func requestServerProcessing(_ episode: Episode, in context: ModelContext) {
        reviveAfterUserRequest(episode, in: context)
        let serverID = episode.serverID
        Task {
            do {
                try await self.api.process(episodeID: serverID)
                self.sync.scheduleFollowUpSyncs()
            } catch {
                Log.feed.notice("process request failed: \(error.localizedDescription, privacy: .public)")
            }
        }
    }

    /// Re-runs ad detection on the server (`POST /reanalyze`). No local file
    /// is needed any more: the server keeps the audio.
    func reanalyzeEpisode(_ episode: Episode, in context: ModelContext) async throws {
        reviveAfterUserRequest(episode, in: context)
        try await api.reanalyze(episodeID: episode.serverID)
        sync.scheduleFollowUpSyncs()
    }

    // MARK: - Queue (device-local)

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

        reviveAfterUserRequest(episode, in: context)

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

    /// A later explicit request supersedes a queued played-release. The
    /// episode becomes eligible for downloads and Status again.
    func reviveAfterUserRequest(_ episode: Episode, in context: ModelContext) {
        guard episode.isPlayed else { return }
        episode.isPlayed = false
        episode.datePlayed = nil
        cancelPendingRelease(episode.serverID)
        try? context.save()
    }

    /// Appends newly published episodes (reported by the sync engine for
    /// podcasts with auto-download on) to the end of the queue, oldest first.
    func enqueueNewEpisodes(serverIDs: [Int], in context: ModelContext) {
        let ids = Array(Set(serverIDs))
        guard !ids.isEmpty else { return }
        var episodes: [Episode] = []
        for batch in SyncEngine.batches(of: ids) {
            let descriptor = FetchDescriptor<Episode>(predicate: #Predicate<Episode> { batch.contains($0.serverID) })
            episodes += (try? context.fetch(descriptor)) ?? []
        }
        let existing = (try? context.fetch(FetchDescriptor<QueueItem>(sortBy: [SortDescriptor(\QueueItem.position)]))) ?? []
        var queued = Set(existing.compactMap { $0.episode?.serverID })
        var nextPosition = (existing.map(\.position).max() ?? -1) + 1
        let ordered = episodes.sorted { ($0.publishedAt ?? .distantPast) < ($1.publishedAt ?? .distantPast) }
        var inserted = 0
        for episode in ordered {
            guard !episode.isPlayed, !queued.contains(episode.serverID) else { continue }
            queued.insert(episode.serverID)
            context.insert(QueueItem(position: nextPosition, episode: episode))
            nextPosition += 1
            inserted += 1
        }
        guard inserted > 0 else { return }
        try? context.save()
        Log.feed.info("Queued \(inserted) newly published episode(s)")
        processQueuedEpisodes(context: context)
    }

    /// Marks every queued episode that is not on the device as wanted
    /// (subject to the auto-download policy); `DownloadManager` starts them
    /// as the network and its concurrency cap allow. Call whenever the queue
    /// changes or after a sync.
    func processQueuedEpisodes(context: ModelContext) {
        let settings = AppSettings.current(in: context)
        guard settings.autoDownloadPolicy != .manualOnly else { return }
        let descriptor = FetchDescriptor<QueueItem>(
            sortBy: [SortDescriptor(\QueueItem.position)]
        )
        let queued = (try? context.fetch(descriptor)) ?? []
        var wanted: [Episode] = []
        for item in queued {
            guard let episode = item.episode, !episode.isMarkedDownloaded else { continue }
            switch episode.downloadState {
            case .idle:
                wanted.append(episode)
            case .queued, .downloading, .downloaded, .failed:
                // Failed downloads wait for an explicit retry.
                continue
            }
        }
        guard !wanted.isEmpty else {
            DownloadManager.shared.startEligibleDownloads()
            return
        }
        DownloadManager.shared.enqueue(wanted, userInitiated: false)
    }

    // MARK: - Helpers

    private func fetchPodcast(serverID: Int, in context: ModelContext) -> Podcast? {
        let descriptor = FetchDescriptor<Podcast>(predicate: #Predicate<Podcast> { $0.serverID == serverID })
        return try? context.fetch(descriptor).first
    }
}
