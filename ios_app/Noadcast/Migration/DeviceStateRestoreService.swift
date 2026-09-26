import Foundation
import SwiftData
import Observation
import os

/// One pending re-attach of a `DeviceStateSnapshot`, persisted as JSON in
/// `pending-restores/` so it survives relaunches until it completes or
/// expires.
nonisolated struct RestoreJob: Codable, Sendable, Equatable {
    var id: String
    var createdAt: Date
    var snapshot: DeviceStateSnapshot
    /// Legacy export only: `POST /api/v1/opml` the feeds the server lacks.
    var subscribeMissingFeeds: Bool
    /// No export could be written: find legacy files by recomputing the old
    /// filename rule (`guid` + MIME) against the mirror.
    var scanLegacyFilenames: Bool
    var subscriptionsSubmitted: Bool
    var resolvedPodcastFeeds: [String]
    var resolvedEpisodeKeys: [String]
    var processRequestedKeys: [String]
    var restoredQueueKeys: [String]
    /// Normalised feed URLs the server refused to subscribe (OPML import
    /// `failed`): their entries give up after the grace period.
    var unavailableFeeds: [String]
    var lastPlayedResolved: Bool
    var adoptedFiles: Int
    var discardedFiles: Int
    var restoredPlaybackStates: Int

    init(snapshot: DeviceStateSnapshot, subscribeMissingFeeds: Bool, scanLegacyFilenames: Bool, createdAt: Date = Date()) {
        self.id = UUID().uuidString
        self.createdAt = createdAt
        self.snapshot = snapshot
        self.subscribeMissingFeeds = subscribeMissingFeeds
        self.scanLegacyFilenames = scanLegacyFilenames
        self.subscriptionsSubmitted = !subscribeMissingFeeds
        self.resolvedPodcastFeeds = []
        self.resolvedEpisodeKeys = []
        self.processRequestedKeys = []
        self.restoredQueueKeys = []
        self.unavailableFeeds = []
        self.lastPlayedResolved = snapshot.lastPlayed == nil
        self.adoptedFiles = 0
        self.discardedFiles = 0
        self.restoredPlaybackStates = 0
    }

    nonisolated enum CodingKeys: String, CodingKey {
        case id, createdAt, snapshot, subscribeMissingFeeds, scanLegacyFilenames
        case subscriptionsSubmitted, resolvedPodcastFeeds, resolvedEpisodeKeys
        case processRequestedKeys, restoredQueueKeys, unavailableFeeds, lastPlayedResolved
        case adoptedFiles, discardedFiles, restoredPlaybackStates
    }
}

/// Re-applies device-local state (playback position, played flag, queue
/// order, per-podcast preferences, downloaded files) to the mirror by
/// `feedURL + guid` once the server's rows exist locally.
///
/// Runs after every successful sync while jobs are pending. Audio files are
/// adopted **only** when their byte size equals the server's `audioBytes`:
/// with dynamic ad insertion a different render has its ads in different
/// places, so the server's markers would be wrong by tens of seconds against
/// it. Mismatched files are deleted; if the episode is queued it is simply
/// downloaded again.
@MainActor
@Observable
final class DeviceStateRestoreService {
    static let shared = DeviceStateRestoreService(
        api: NoadcastAPIClient.shared,
        jobsDirectory: AudioStorage.applicationSupportDirectory
            .appendingPathComponent("pending-restores", isDirectory: true),
        performsSideEffects: true,
        isConfigured: { APIConfiguration.isConfigured }
    )

    /// Give up on anything still unresolved after this long.
    static let expiry: TimeInterval = 14 * 24 * 3_600
    /// How long a feed may take to (re)appear on the server before its
    /// entries stop holding up the queue.
    static let missingFeedGrace: TimeInterval = 24 * 3_600
    static let maxProcessRequestsPerPass = 20
    static let statusDefaultsKey = "LegacyMigrationStatus"
    static let legacyImportedDefaultsKey = "LegacyExportImported"

    /// One-line migration status for Settings (nil when nothing to say).
    private(set) var statusLine: String?

    let api: NoadcastAPIClient
    let jobsDirectory: URL
    let performsSideEffects: Bool
    let isConfigured: () -> Bool

    @ObservationIgnored private var container: ModelContainer?
    @ObservationIgnored private var jobs: [RestoreJob] = []
    @ObservationIgnored private var isRunning = false

    init(api: NoadcastAPIClient, jobsDirectory: URL, performsSideEffects: Bool, isConfigured: @escaping () -> Bool) {
        self.api = api
        self.jobsDirectory = jobsDirectory
        self.performsSideEffects = performsSideEffects
        self.isConfigured = isConfigured
        try? FileManager.default.createDirectory(at: jobsDirectory, withIntermediateDirectories: true)
        self.jobs = Self.loadJobs(from: jobsDirectory)
        refreshStatusLine()
    }

    /// Wires the container and, once, imports the legacy export: the parts
    /// that need no server (preferences, lifetime stats, daily history)
    /// immediately, the rest as a pending job.
    func configure(container: ModelContainer) {
        self.container = container
        if performsSideEffects {
            importLegacyExportIfNeeded(context: container.mainContext)
        }
        refreshStatusLine()
    }

    var hasPendingJobs: Bool {
        !jobs.isEmpty
    }

    func enqueue(_ snapshot: DeviceStateSnapshot, subscribeMissingFeeds: Bool) {
        let job = RestoreJob(snapshot: snapshot, subscribeMissingFeeds: subscribeMissingFeeds, scanLegacyFilenames: false)
        jobs.append(job)
        persist(job)
        refreshStatusLine()
    }

    func enqueueLegacyFilenameScan() {
        let job = RestoreJob(
            snapshot: DeviceStateSnapshot(source: .legacyStore),
            subscribeMissingFeeds: false,
            scanLegacyFilenames: true
        )
        jobs.append(job)
        persist(job)
        refreshStatusLine()
    }

    func discardAllJobs() {
        for job in jobs {
            try? FileManager.default.removeItem(at: jobURL(job.id))
        }
        jobs.removeAll()
        refreshStatusLine()
    }

    /// Files a pending job still intends to adopt; the download manager's
    /// orphan sweep must leave them alone.
    func protectedFilenames() -> Set<String> {
        var names = Set<String>()
        for job in jobs {
            let resolved = Set(job.resolvedEpisodeKeys)
            for state in job.snapshot.episodes where !resolved.contains(state.key.matchKey) {
                if let filename = state.localFilename {
                    names.insert(filename)
                }
            }
            if job.scanLegacyFilenames {
                for name in AudioStorage.listEpisodeFiles() where !AudioStorage.isServerFilename(name) {
                    names.insert(name)
                }
            }
        }
        return names
    }

    /// Runs one pass of every pending job. Returns `true` when a follow-up
    /// sync would help (feeds were just subscribed, or the server was asked
    /// to fetch audio so a file can be size-checked).
    @discardableResult
    func runPendingRestores() async -> Bool {
        guard !isRunning, !jobs.isEmpty, isConfigured(), let context = container?.mainContext else {
            return false
        }
        isRunning = true
        defer { isRunning = false }

        var wantsFollowUpSync = false
        var restoredLastPlayed = false
        for jobID in jobs.map(\.id) {
            guard var job = jobs.first(where: { $0.id == jobID }) else { continue }
            let outcome = await runPass(&job, context: context)
            wantsFollowUpSync = wantsFollowUpSync || outcome.wantsFollowUpSync
            restoredLastPlayed = restoredLastPlayed || outcome.restoredLastPlayed
            if outcome.isComplete {
                finish(job)
            } else if let index = jobs.firstIndex(where: { $0.id == jobID }) {
                jobs[index] = job
                persist(job)
            }
        }
        if context.hasChanges {
            try? context.save()
        }
        if restoredLastPlayed, performsSideEffects {
            PlayerService.shared.restoreLastPlayedEpisode(context: context)
        }
        refreshStatusLine()
        return wantsFollowUpSync
    }

    // MARK: - One pass

    private struct PassOutcome {
        var isComplete: Bool
        var wantsFollowUpSync: Bool
        var restoredLastPlayed: Bool
    }

    private func runPass(_ job: inout RestoreJob, context: ModelContext) async -> PassOutcome {
        let now = Date()
        let age = now.timeIntervalSince(job.createdAt)
        let expired = age > Self.expiry
        let graceOver = age > Self.missingFeedGrace
        var wantsFollowUpSync = false
        var restoredLastPlayed = false

        let podcasts = (try? context.fetch(FetchDescriptor<Podcast>())) ?? []
        var podcastByFeed: [String: Podcast] = [:]
        var feedKeyByPodcastID: [Int: String] = [:]
        for podcast in podcasts {
            let key = FeedURLKey.normalize(podcast.feedURL.absoluteString)
            podcastByFeed[key] = podcast
            feedKeyByPodcastID[podcast.serverID] = key
        }
        func mirroredPodcast(forFeed feedURL: String) -> Podcast? {
            podcastByFeed[FeedURLKey.normalize(feedURL)]
        }
        let unavailableFeeds = Set(job.unavailableFeeds)
        /// The server has fetched the feed at least once (or refused it), so
        /// a missing episode is really gone rather than still importing.
        func isCaughtUp(feed feedURL: String) -> Bool {
            if unavailableFeeds.contains(FeedURLKey.normalize(feedURL)) {
                return true
            }
            return mirroredPodcast(forFeed: feedURL)?.lastFetched != nil
        }

        // 1. Re-subscribe feeds the server lacks (legacy export only), once.
        if job.subscribeMissingFeeds, !job.subscriptionsSubmitted {
            var seen = Set<String>()
            let missing = job.snapshot.podcasts.filter { state in
                let key = FeedURLKey.normalize(state.feedURL)
                return podcastByFeed[key] == nil && seen.insert(key).inserted
            }
            if missing.isEmpty {
                job.subscriptionsSubmitted = true
            } else {
                let opml = OPMLWriter.document(feeds: missing.map { (url: $0.feedURL, title: $0.title) })
                do {
                    let result = try await api.importOPML(opml)
                    job.subscriptionsSubmitted = true
                    job.unavailableFeeds = result.failed.map { FeedURLKey.normalize($0.feedUrl) }
                    wantsFollowUpSync = true
                    Log.migration.notice("Subscribed \(result.added.count) feed(s) on the server (\(result.existing.count) existing, \(result.failed.count) failed)")
                } catch {
                    Log.migration.error("OPML import failed: \(error.localizedDescription, privacy: .public)")
                }
            }
        }

        // 2. Per-podcast device-local preferences.
        var resolvedFeeds = Set(job.resolvedPodcastFeeds)
        for state in job.snapshot.podcasts {
            let key = FeedURLKey.normalize(state.feedURL)
            guard !resolvedFeeds.contains(key) else { continue }
            guard let podcast = podcastByFeed[key] else {
                if expired || job.unavailableFeeds.contains(key) {
                    resolvedFeeds.insert(key)
                }
                continue
            }
            if podcast.autoDownloadEnabled != state.autoDownloadEnabled {
                podcast.autoDownloadEnabled = state.autoDownloadEnabled
            }
            if podcast.customPlaybackSpeed != state.customPlaybackSpeed {
                podcast.customPlaybackSpeed = state.customPlaybackSpeed
            }
            if state.adAnalysisEnabled == false, podcast.adAnalysisEnabled {
                // The user had turned analysis off for this show on-device.
                do {
                    let dto = try await api.updatePodcast(id: podcast.serverID, adAnalysisEnabled: false)
                    if podcast.adAnalysisEnabled != dto.adAnalysisEnabled {
                        podcast.adAnalysisEnabled = dto.adAnalysisEnabled
                    }
                } catch {
                    continue
                }
            }
            resolvedFeeds.insert(key)
        }
        job.resolvedPodcastFeeds = Array(resolvedFeeds)

        // 3. Episodes: playback state and audio files.
        var wantedGUIDs = Set(job.snapshot.episodes.map(\.guid))
        wantedGUIDs.formUnion(job.snapshot.queue.map(\.guid))
        if let last = job.snapshot.lastPlayed {
            wantedGUIDs.insert(last.guid)
        }
        let lookup = localEpisodes(guids: wantedGUIDs, feedKeyByPodcastID: feedKeyByPodcastID, context: context)

        var resolvedEpisodes = Set(job.resolvedEpisodeKeys)
        var processRequested = Set(job.processRequestedKeys)
        var processBudget = Self.maxProcessRequestsPerPass
        for state in job.snapshot.episodes {
            let key = state.key.matchKey
            guard !resolvedEpisodes.contains(key) else { continue }
            guard let episode = lookup[key] else {
                // Not on the server (yet). Give up once the feed has been
                // fetched and the grace period is over, or at expiry.
                if expired || (graceOver && isCaughtUp(feed: state.feedURL)) {
                    if let filename = state.localFilename, AudioStorage.fileExists(named: filename) {
                        AudioStorage.deleteFile(named: filename)
                        job.discardedFiles += 1
                    }
                    resolvedEpisodes.insert(key)
                }
                continue
            }

            // Playback state, only onto a row that has none of its own.
            if episode.playbackPosition == 0, !episode.isPlayed, state.playbackPosition > 0 || state.isPlayed {
                if state.playbackPosition > 0 {
                    episode.playbackPosition = state.playbackPosition
                }
                if state.isPlayed {
                    episode.isPlayed = true
                    episode.datePlayed = state.datePlayed
                }
                job.restoredPlaybackStates += 1
            }

            if let filename = state.localFilename, AudioStorage.fileExists(named: filename) {
                if episode.localFilename == filename {
                    // Adopted by an earlier pass.
                } else if episode.localFilename != nil {
                    // It already has a download of its own.
                    AudioStorage.deleteFile(named: filename)
                    job.discardedFiles += 1
                } else if let serverBytes = episode.audioBytes {
                    if AudioStorage.fileSize(named: filename) == serverBytes {
                        adopt(filename: filename, size: serverBytes, into: episode)
                        job.adoptedFiles += 1
                    } else {
                        AudioStorage.deleteFile(named: filename)
                        job.discardedFiles += 1
                    }
                } else if expired {
                    AudioStorage.deleteFile(named: filename)
                    job.discardedFiles += 1
                } else {
                    // The server has not fetched this episode's audio, so
                    // there is nothing to compare the file against yet. Ask
                    // it to (the user had it downloaded, so wants it) and
                    // keep the file protected until the size is known.
                    if !state.isPlayed, !processRequested.contains(key), processBudget > 0 {
                        processBudget -= 1
                        do {
                            try await api.process(episodeID: episode.serverID)
                            processRequested.insert(key)
                            wantsFollowUpSync = true
                        } catch {
                            Log.migration.notice("process request failed: \(error.localizedDescription, privacy: .public)")
                        }
                    }
                    continue
                }
            }
            resolvedEpisodes.insert(key)
        }
        job.resolvedEpisodeKeys = Array(resolvedEpisodes)
        job.processRequestedKeys = Array(processRequested)

        // 4. No export: recompute the old filename rule.
        if job.scanLegacyFilenames {
            if await scanLegacyFiles(&job, context: context, expired: expired, processBudget: &processBudget) {
                wantsFollowUpSync = true
            }
        }

        // 5. Queue, in snapshot order. An entry whose feed has not been
        // fetched yet holds back the entries after it (so the order is kept)
        // until the grace period ends.
        var restoredQueue = Set(job.restoredQueueKeys)
        let existingItems = (try? context.fetch(
            FetchDescriptor<QueueItem>(sortBy: [SortDescriptor(\QueueItem.position)])
        )) ?? []
        var queuedIDs = Set(existingItems.compactMap { $0.episode?.serverID })
        var nextPosition = (existingItems.map(\.position).max() ?? -1) + 1
        for key in job.snapshot.queue {
            let match = key.matchKey
            guard !restoredQueue.contains(match) else { continue }
            if let episode = lookup[match] {
                if !queuedIDs.contains(episode.serverID), !episode.isPlayed {
                    context.insert(QueueItem(position: nextPosition, episode: episode))
                    nextPosition += 1
                    queuedIDs.insert(episode.serverID)
                }
                restoredQueue.insert(match)
            } else if expired || graceOver || isCaughtUp(feed: key.feedURL) {
                restoredQueue.insert(match)
            } else {
                break
            }
        }
        job.restoredQueueKeys = Array(restoredQueue)

        // 6. Last played.
        if !job.lastPlayedResolved, let key = job.snapshot.lastPlayed {
            if let episode = lookup[key.matchKey] {
                let settings = AppSettings.current(in: context)
                if settings.lastPlayedEpisodeServerID == nil {
                    settings.lastPlayedEpisodeServerID = episode.serverID
                    restoredLastPlayed = true
                }
                job.lastPlayedResolved = true
            } else if expired || graceOver || isCaughtUp(feed: key.feedURL) {
                job.lastPlayedResolved = true
            }
        }

        let allFeeds = Set(job.snapshot.podcasts.map { FeedURLKey.normalize($0.feedURL) })
        let allEpisodes = Set(job.snapshot.episodes.map { $0.key.matchKey })
        let allQueue = Set(job.snapshot.queue.map(\.matchKey))
        let complete = expired || (
            job.subscriptionsSubmitted
                && allFeeds.isSubset(of: resolvedFeeds)
                && allEpisodes.isSubset(of: resolvedEpisodes)
                && allQueue.isSubset(of: restoredQueue)
                && job.lastPlayedResolved
                && !job.scanLegacyFilenames
        )
        if complete, expired {
            for state in job.snapshot.episodes where !resolvedEpisodes.contains(state.key.matchKey) {
                if let filename = state.localFilename, AudioStorage.fileExists(named: filename),
                   lookup[state.key.matchKey]?.localFilename != filename {
                    AudioStorage.deleteFile(named: filename)
                    job.discardedFiles += 1
                }
            }
        }
        return PassOutcome(isComplete: complete, wantsFollowUpSync: wantsFollowUpSync, restoredLastPlayed: restoredLastPlayed)
    }

    /// Size-gated adoption for the no-export fallback. Returns whether the
    /// server was asked to fetch audio.
    private func scanLegacyFiles(
        _ job: inout RestoreJob,
        context: ModelContext,
        expired: Bool,
        processBudget: inout Int
    ) async -> Bool {
        let files = AudioStorage.listEpisodeFiles().filter { !AudioStorage.isServerFilename($0) }
        guard !files.isEmpty else {
            job.scanLegacyFilenames = false
            return false
        }
        if expired {
            for name in files {
                AudioStorage.deleteFile(named: name)
                job.discardedFiles += 1
            }
            job.scanLegacyFilenames = false
            return false
        }
        var remaining = Set(files)
        var requested = Set(job.processRequestedKeys)
        var wantsFollowUp = false
        let descriptor = FetchDescriptor<Episode>(predicate: #Predicate<Episode> { $0.localFilename == nil })
        let candidates = (try? context.fetch(descriptor)) ?? []
        for episode in candidates {
            guard !remaining.isEmpty else { break }
            let name = AudioStorage.legacyFilename(guid: episode.guid, mimeType: episode.audioMimeType)
            guard remaining.contains(name) else { continue }
            if let serverBytes = episode.audioBytes {
                if AudioStorage.fileSize(named: name) == serverBytes {
                    adopt(filename: name, size: serverBytes, into: episode)
                    job.adoptedFiles += 1
                } else {
                    AudioStorage.deleteFile(named: name)
                    job.discardedFiles += 1
                }
                remaining.remove(name)
            } else if !requested.contains(name), processBudget > 0 {
                processBudget -= 1
                do {
                    try await api.process(episodeID: episode.serverID)
                    requested.insert(name)
                    wantsFollowUp = true
                } catch {
                    Log.migration.notice("process request failed: \(error.localizedDescription, privacy: .public)")
                }
            }
        }
        job.processRequestedKeys = Array(requested)
        if remaining.isEmpty {
            job.scanLegacyFilenames = false
        }
        return wantsFollowUp
    }

    private func adopt(filename: String, size: Int64, into episode: Episode) {
        episode.localFilename = filename
        episode.fileSizeBytes = size
        episode.localAudioSha256 = episode.audioSha256
        episode.setDownloadState(.downloaded)
        if episode.downloadProgress != 1 {
            episode.downloadProgress = 1
        }
        if episode.downloadError != nil {
            episode.downloadError = nil
        }
    }

    /// Mirror episodes for these GUIDs, keyed by `EpisodeKey.matchKey`.
    private func localEpisodes(
        guids: Set<String>,
        feedKeyByPodcastID: [Int: String],
        context: ModelContext
    ) -> [String: Episode] {
        var result: [String: Episode] = [:]
        let sorted = guids.sorted()
        var start = 0
        while start < sorted.count {
            let end = min(start + 250, sorted.count)
            let batch = Array(sorted[start..<end])
            let descriptor = FetchDescriptor<Episode>(predicate: #Predicate<Episode> { batch.contains($0.guid) })
            for episode in (try? context.fetch(descriptor)) ?? [] {
                guard let feedKey = feedKeyByPodcastID[episode.podcastServerID] else { continue }
                result[feedKey + "\u{1F}" + episode.guid] = episode
            }
            start = end
        }
        return result
    }

    // MARK: - Legacy phase 1

    private func importLegacyExportIfNeeded(context: ModelContext) {
        let defaults = UserDefaults.standard
        guard !defaults.bool(forKey: Self.legacyImportedDefaultsKey) else { return }
        defer { defaults.set(true, forKey: Self.legacyImportedDefaultsKey) }
        switch LocalStoreGeneration.outcome {
        case .exported?:
            guard let data = try? Data(contentsOf: LocalStoreGeneration.legacyExportURL),
                  let snapshot = try? DeviceStateSnapshot.decode(data)
            else {
                defaults.set("The previous library's export could not be read; a backup of it is in legacy-store-backup.", forKey: Self.statusDefaultsKey)
                enqueueLegacyFilenameScan()
                return
            }
            applyLocalOnlyState(snapshot, context: context)
            enqueue(snapshot, subscribeMissingFeeds: true)
        case .exportFailed?:
            defaults.set("The previous library couldn't be read; a backup of it is in legacy-store-backup.", forKey: Self.statusDefaultsKey)
            enqueueLegacyFilenameScan()
        case .noLegacyStore?, nil:
            break
        }
    }

    /// Preferences, lifetime stats and daily listening history need no
    /// server, so they are restored at the first launch.
    private func applyLocalOnlyState(_ snapshot: DeviceStateSnapshot, context: ModelContext) {
        let settings = AppSettings.current(in: context)
        if let preferences = snapshot.preferences {
            settings.defaultPlaybackSpeed = preferences.defaultPlaybackSpeed
            if let policy = AutoDownloadPolicy(rawValue: preferences.autoDownloadPolicy) {
                settings.autoDownloadPolicy = policy
            }
            settings.autoDeleteAfterPlayed = preferences.autoDeleteAfterPlayed
            if let mode = PodcastSortMode(rawValue: preferences.podcastSortMode) {
                settings.podcastSortMode = mode
            }
            settings.skipAds = preferences.skipAds
            settings.skipIntrosAndOutros = preferences.skipIntrosAndOutros
            settings.chainSkipGapSeconds = preferences.chainSkipGapSeconds
        }
        if let stats = snapshot.stats {
            settings.lifetimePlayedSeconds += stats.lifetimePlayedSeconds
            settings.lifetimeAdSkipSeconds += stats.lifetimeAdSkipSeconds
        }
        for day in snapshot.usageDays ?? [] {
            let row = UsageHistoryDay(dayStart: day.dayStart)
            row.playbackSeconds = day.playbackSeconds
            row.adSkippedSeconds = day.adSkippedSeconds
            context.insert(row)
        }
        try? context.save()
    }

    // MARK: - Persistence

    private func jobURL(_ id: String) -> URL {
        jobsDirectory.appendingPathComponent("\(id).json")
    }

    private func persist(_ job: RestoreJob) {
        do {
            let data = try JSONEncoder().encode(job)
            try data.write(to: jobURL(job.id), options: .atomic)
        } catch {
            Log.migration.error("Could not persist restore job: \(error.localizedDescription, privacy: .public)")
        }
    }

    private func finish(_ job: RestoreJob) {
        jobs.removeAll { $0.id == job.id }
        try? FileManager.default.removeItem(at: jobURL(job.id))
        let summary: String
        switch job.snapshot.source {
        case .legacyStore:
            summary = "Migrated the previous library: \(job.restoredPlaybackStates) playback positions restored, \(job.adoptedFiles) downloads kept, \(job.discardedFiles) removed (different audio on the server)."
        case .instanceChange:
            summary = "Re-attached playback state after a server change: \(job.adoptedFiles) downloads kept, \(job.discardedFiles) removed."
        case .localReset:
            summary = "Local cache reset: \(job.restoredPlaybackStates) playback positions restored."
        }
        if performsSideEffects {
            UserDefaults.standard.set(summary, forKey: Self.statusDefaultsKey)
        }
        Log.migration.notice("\(summary, privacy: .public)")
    }

    private static func loadJobs(from directory: URL) -> [RestoreJob] {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: directory.path)) ?? []
        var loaded: [RestoreJob] = []
        for name in names where name.hasSuffix(".json") {
            let url = directory.appendingPathComponent(name)
            if let data = try? Data(contentsOf: url), let job = try? JSONDecoder().decode(RestoreJob.self, from: data) {
                loaded.append(job)
            }
        }
        return loaded.sorted { $0.createdAt < $1.createdAt }
    }

    private func refreshStatusLine() {
        if let legacy = jobs.first(where: { $0.snapshot.source == .legacyStore }) {
            if !isConfigured() {
                let count = legacy.snapshot.podcasts.count
                statusLine = count > 0
                    ? "Migration pending: connect to your server to restore \(count) podcasts."
                    : "Migration pending: connect to your server to check old downloads."
            } else {
                let total = legacy.snapshot.episodes.count
                let done = legacy.resolvedEpisodeKeys.count
                statusLine = "Migration in progress: \(done) of \(total) episodes restored, \(legacy.adoptedFiles) downloads kept."
            }
        } else if let other = jobs.first {
            let total = other.snapshot.episodes.count
            statusLine = "Restoring playback state (\(other.resolvedEpisodeKeys.count) of \(total))."
        } else {
            statusLine = UserDefaults.standard.string(forKey: Self.statusDefaultsKey)
        }
    }
}
