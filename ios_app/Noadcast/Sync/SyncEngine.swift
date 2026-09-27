import Foundation
import SwiftData
import os

/// Optimistic local values that must win over a sync page while the PATCH
/// that sets them is still in flight (otherwise a concurrent sync would flip
/// the toggle back until the next page).
nonisolated struct SyncOverrides: Sendable, Equatable {
    var globalAdAnalysisEnabled: Bool?
    var podcastAdAnalysis: [Int: Bool]

    init(globalAdAnalysisEnabled: Bool? = nil, podcastAdAnalysis: [Int: Bool] = [:]) {
        self.globalAdAnalysisEnabled = globalAdAnalysisEnabled
        self.podcastAdAnalysis = podcastAdAnalysis
    }

    static let empty = SyncOverrides()
}

nonisolated struct SyncApplyOptions: Sendable {
    /// Candidates for the Queue are reported only when allowed (tests and
    /// out-of-band applies turn it off).
    var allowAutoQueue: Bool
    var overrides: SyncOverrides
    var now: Date

    init(allowAutoQueue: Bool = true, overrides: SyncOverrides = .empty, now: Date = Date()) {
        self.allowAutoQueue = allowAutoQueue
        self.overrides = overrides
        self.now = now
    }

    /// Never auto-queue an episode published longer ago than this, even if
    /// it is new to the mirror.
    static let autoQueueWindow: TimeInterval = 14 * 24 * 3_600
}

/// What one apply changed — the orchestrator turns this into side effects
/// (artwork, player marker refresh, downloads, Queue).
nonisolated struct SyncApplyReport: Sendable, Equatable {
    var insertedPodcastIDs: [Int] = []
    var changedPodcastIDs: [Int] = []
    var podcastIDsNeedingArtwork: [Int] = []
    var insertedEpisodeIDs: [Int] = []
    var changedEpisodeIDs: [Int] = []
    var autoQueueEpisodeIDs: [Int] = []
    var markersChangedEpisodeIDs: [Int] = []
    var audioBecamePresentEpisodeIDs: [Int] = []
    var deletedPodcastIDs: [Int] = []
    var deletedEpisodeIDs: [Int] = []
    /// Episodes whose podcast was unknown (the server guarantees referential
    /// closure, so this should stay empty).
    var orphanedEpisodeIDs: [Int] = []
    var seenPodcastIDs: Set<Int> = []
    var seenEpisodeIDs: Set<Int> = []
    var settingsChanged = false
    var didSave = false

    init() {}

    var hasMirrorChanges: Bool {
        !insertedPodcastIDs.isEmpty || !changedPodcastIDs.isEmpty
            || !insertedEpisodeIDs.isEmpty || !changedEpisodeIDs.isEmpty
            || !deletedPodcastIDs.isEmpty || !deletedEpisodeIDs.isEmpty
            || settingsChanged
    }

    mutating func merge(_ other: SyncApplyReport) {
        insertedPodcastIDs += other.insertedPodcastIDs
        changedPodcastIDs += other.changedPodcastIDs
        podcastIDsNeedingArtwork += other.podcastIDsNeedingArtwork
        insertedEpisodeIDs += other.insertedEpisodeIDs
        changedEpisodeIDs += other.changedEpisodeIDs
        autoQueueEpisodeIDs += other.autoQueueEpisodeIDs
        markersChangedEpisodeIDs += other.markersChangedEpisodeIDs
        audioBecamePresentEpisodeIDs += other.audioBecamePresentEpisodeIDs
        deletedPodcastIDs += other.deletedPodcastIDs
        deletedEpisodeIDs += other.deletedEpisodeIDs
        orphanedEpisodeIDs += other.orphanedEpisodeIDs
        seenPodcastIDs.formUnion(other.seenPodcastIDs)
        seenEpisodeIDs.formUnion(other.seenEpisodeIDs)
        settingsChanged = settingsChanged || other.settingsChanged
        didSave = didSave || other.didSave
    }
}

nonisolated struct SyncCursorState: Sendable, Equatable {
    var instanceId: String?
    var nextSince: Int
    var lastSyncAt: Date?
    var lastFullSyncAt: Date?
}

nonisolated struct MirrorCounts: Sendable, Equatable {
    var podcasts: Int
    var episodes: Int
    var markers: Int
    var queueItems: Int
}

nonisolated struct PlayedReconciliationSnapshot: Sendable, Equatable {
    var releaseEpisodeIDs: [Int]
    var localCleanupEpisodeIDs: [Int]
}

/// Applies server data to the SwiftData mirror on its own context, off the
/// main actor (the pattern the old `DatabaseMaintenanceActor` proved).
///
/// Invariants:
/// * **Write-if-changed, always.** SwiftData dirties a model on any
///   assignment and a dirty row invalidates every `@Query` containing it;
///   re-applying an unchanged page must leave `context.hasChanges`
///   false.
/// * **Ownership.** Only server-mirror fields are written. Download state,
///   file, playback position, played flag, queue, auto-download and speed
///   preferences are device-local and never touched here (except when a
///   server deletion removes the row).
/// * Upserts are keyed by `serverID`, fetched in batches of ≤250 ids per
///   `#Predicate` (SwiftData chokes on huge `IN` clauses). The store's
///   unique constraint is a backstop, not the mechanism.
@ModelActor
actor SyncEngine {
    /// A fresh context per operation. A long-lived background context keeps
    /// the values it first fetched, so device-local fields written since by
    /// the main context (playback position, downloads, queue) would read
    /// stale — and saving a stale row risks a merge conflict. Every
    /// operation is synchronous, so one context never spans a suspension
    /// point. `hasUnsavedChanges()` inspects the last operation's context.
    private var operationContext: ModelContext?

    private var context: ModelContext {
        if let operationContext {
            return operationContext
        }
        return modelContext
    }

    private func beginOperation() {
        let fresh = ModelContext(modelContainer)
        fresh.autosaveEnabled = false
        operationContext = fresh
    }

    // MARK: - Cursor

    func cursorState() -> SyncCursorState {
        beginOperation()
        let cursor = SyncCursor.fetchOrInsert(in: context)
        if context.hasChanges {
            try? context.save()
        }
        return SyncCursorState(
            instanceId: cursor.instanceId,
            nextSince: cursor.nextSince,
            lastSyncAt: cursor.lastSyncAt,
            lastFullSyncAt: cursor.lastFullSyncAt
        )
    }

    func markSyncCompleted(at date: Date, full: Bool) throws {
        beginOperation()
        let cursor = SyncCursor.fetchOrInsert(in: context)
        cursor.lastSyncAt = date
        if full {
            cursor.lastFullSyncAt = date
        }
        try context.save()
    }

    // MARK: - Applying

    /// Applies one `/sync` page: podcasts → episodes → deletions → settings,
    /// then advances the cursor in the same save, so `nextSince` is
    /// persisted only once the page is fully applied.
    func apply(page: SyncPageDTO, options: SyncApplyOptions, save: Bool = true) throws -> SyncApplyReport {
        beginOperation()
        var report = SyncApplyReport()
        let podcasts = try upsertPodcasts(page.podcasts, options: options, report: &report)
        try upsertEpisodes(page.episodes, knownPodcasts: podcasts, options: options, report: &report)
        try applyDeletions(page.deletions, report: &report)
        if let settings = page.settings {
            applySettings(settings, overrides: options.overrides, report: &report)
        }
        let cursor = SyncCursor.fetchOrInsert(in: context)
        if let instanceId = page.instanceId, cursor.instanceId != instanceId {
            cursor.instanceId = instanceId
        }
        if cursor.nextSince != page.nextSince {
            cursor.nextSince = page.nextSince
        }
        if save {
            report.didSave = try saveIfNeeded()
        }
        return report
    }

    /// Out-of-band podcast rows (subscribe / PATCH responses). The cursor is
    /// untouched: the same rows arrive again through `/sync`, idempotently.
    func applyPodcasts(_ dtos: [PodcastDTO], overrides: SyncOverrides = .empty) throws -> SyncApplyReport {
        beginOperation()
        var report = SyncApplyReport()
        _ = try upsertPodcasts(dtos, options: SyncApplyOptions(allowAutoQueue: false, overrides: overrides), report: &report)
        report.didSave = try saveIfNeeded()
        return report
    }

    /// Out-of-band episode rows (`GET /episodes/{id}` while preparing audio).
    func applyEpisodes(_ dtos: [EpisodeDTO]) throws -> SyncApplyReport {
        beginOperation()
        var report = SyncApplyReport()
        try upsertEpisodes(dtos, knownPodcasts: [:], options: SyncApplyOptions(allowAutoQueue: false), report: &report)
        report.didSave = try saveIfNeeded()
        return report
    }

    /// Test/diagnostic hook: are there unsaved changes on the engine context?
    func hasUnsavedChanges() -> Bool {
        context.hasChanges
    }

    func mirrorCounts() -> MirrorCounts {
        beginOperation()
        return MirrorCounts(
            podcasts: (try? context.fetchCount(FetchDescriptor<Podcast>())) ?? 0,
            episodes: (try? context.fetchCount(FetchDescriptor<Episode>())) ?? 0,
            markers: (try? context.fetchCount(FetchDescriptor<AdMarker>())) ?? 0,
            queueItems: (try? context.fetchCount(FetchDescriptor<QueueItem>())) ?? 0
        )
    }

    /// Read device-local played intent from a fresh context after every sync,
    /// including an empty delta. This never writes the server mirror.
    func playedReconciliationSnapshot() throws -> PlayedReconciliationSnapshot {
        beginOperation()
        let played = try context.fetch(FetchDescriptor<Episode>(predicate: #Predicate<Episode> { $0.isPlayed }))
        var releases: [Int] = []
        var cleanup = Set<Int>()
        for episode in played {
            if episode.audioState == .present || episode.audioState == .partial || episode.serverState.isActive {
                releases.append(episode.serverID)
            }
            if episode.localFilename != nil || episode.fileSizeBytes != nil || episode.localAudioSha256 != nil
                || episode.downloadState != .idle || episode.downloadTaskIdentifier != nil
                || episode.downloadIsUserInitiated || episode.downloadRequestedAt != nil || episode.downloadError != nil
                || episode.downloadProgress != 0 || episode.downloadedBytes != nil || episode.downloadTotalBytes != nil {
                cleanup.insert(episode.serverID)
            }
        }
        for item in try context.fetch(FetchDescriptor<QueueItem>()) {
            if let episode = item.episode, episode.isPlayed {
                cleanup.insert(episode.serverID)
            }
        }
        return PlayedReconciliationSnapshot(
            releaseEpisodeIDs: releases.sorted(),
            localCleanupEpisodeIDs: cleanup.sorted()
        )
    }

    // MARK: - Wipe / sweep

    /// Deletes every mirror row (podcasts, episodes, markers, queue items)
    /// and points the cursor at `instanceId` from `since = 0`. Files on disk
    /// are left alone; the caller snapshots device-local state first.
    func wipeMirrors(newInstanceId: String?) throws {
        beginOperation()
        for item in try context.fetch(FetchDescriptor<QueueItem>()) {
            context.delete(item)
        }
        for marker in try context.fetch(FetchDescriptor<AdMarker>()) {
            context.delete(marker)
        }
        for episode in try context.fetch(FetchDescriptor<Episode>()) {
            context.delete(episode)
        }
        for podcast in try context.fetch(FetchDescriptor<Podcast>()) {
            context.delete(podcast)
        }
        let cursor = SyncCursor.fetchOrInsert(in: context)
        cursor.instanceId = newInstanceId
        cursor.nextSince = 0
        cursor.lastFullSyncAt = nil
        if let settings = try context.fetch(FetchDescriptor<AppSettings>()).first,
           settings.lastPlayedEpisodeServerID != nil {
            settings.lastPlayedEpisodeServerID = nil
        }
        try context.save()
    }

    /// Rows a full resync (after `410 cursorExpired`, or "re-sync from
    /// scratch") did not see.
    func unseenMirrorIDs(seenPodcastIDs: Set<Int>, seenEpisodeIDs: Set<Int>) throws -> (podcasts: [Int], episodes: [Int]) {
        beginOperation()
        let podcasts = try context.fetch(FetchDescriptor<Podcast>())
            .map(\.serverID)
            .filter { !seenPodcastIDs.contains($0) }
        let episodes = try context.fetch(FetchDescriptor<Episode>())
            .map(\.serverID)
            .filter { !seenEpisodeIDs.contains($0) }
        return (podcasts, episodes)
    }

    /// Deletes mirror rows with their device-local side effects (queue items,
    /// audio file, resume data). Returns every deleted episode id.
    @discardableResult
    func deleteMirrors(podcastIDs: [Int], episodeIDs: [Int]) throws -> [Int] {
        beginOperation()
        var deleted = try deleteEpisodeRows(Array(try fetchEpisodes(ids: episodeIDs).values))
        let (_, podcastEpisodes) = try deletePodcastRows(ids: podcastIDs)
        deleted += podcastEpisodes
        _ = try saveIfNeeded()
        return deleted
    }

    // MARK: - Snapshot

    /// Device-local state worth keeping across a mirror wipe, keyed by
    /// `feedURL + guid`.
    func snapshotDeviceState(source: DeviceStateSnapshot.Source, includeFiles: Bool) throws -> DeviceStateSnapshot {
        beginOperation()
        let podcasts = try context.fetch(FetchDescriptor<Podcast>())
        var feedByPodcastID: [Int: String] = [:]
        var podcastStates: [DeviceStateSnapshot.PodcastState] = []
        for podcast in podcasts {
            let feed = podcast.feedURL.absoluteString
            feedByPodcastID[podcast.serverID] = feed
            podcastStates.append(DeviceStateSnapshot.PodcastState(
                feedURL: feed,
                title: podcast.title,
                autoDownloadEnabled: podcast.autoDownloadEnabled,
                customPlaybackSpeed: podcast.customPlaybackSpeed,
                adAnalysisEnabled: nil
            ))
        }

        let interesting = FetchDescriptor<Episode>(
            predicate: #Predicate<Episode> { episode in
                episode.localFilename != nil || episode.playbackPosition > 0 || episode.isPlayed
            }
        )
        var episodeStates: [DeviceStateSnapshot.EpisodeState] = []
        for episode in try context.fetch(interesting) {
            guard let feed = feedByPodcastID[episode.podcastServerID] else { continue }
            episodeStates.append(DeviceStateSnapshot.EpisodeState(
                feedURL: feed,
                guid: episode.guid,
                title: episode.title,
                localFilename: includeFiles ? episode.localFilename : nil,
                fileSizeBytes: includeFiles ? episode.fileSizeBytes : nil,
                audioMimeType: episode.audioMimeType,
                playbackPosition: episode.playbackPosition,
                isPlayed: episode.isPlayed,
                datePlayed: episode.datePlayed
            ))
        }

        var queue: [DeviceStateSnapshot.EpisodeKey] = []
        let queueDescriptor = FetchDescriptor<QueueItem>(sortBy: [SortDescriptor(\QueueItem.position)])
        for item in try context.fetch(queueDescriptor) {
            guard let episode = item.episode, let feed = feedByPodcastID[episode.podcastServerID] else { continue }
            queue.append(DeviceStateSnapshot.EpisodeKey(feedURL: feed, guid: episode.guid))
        }

        var lastPlayed: DeviceStateSnapshot.EpisodeKey?
        if let settings = try context.fetch(FetchDescriptor<AppSettings>()).first,
           let lastID = settings.lastPlayedEpisodeServerID,
           let episode = try fetchEpisodes(ids: [lastID]).values.first,
           let feed = feedByPodcastID[episode.podcastServerID] {
            lastPlayed = DeviceStateSnapshot.EpisodeKey(feedURL: feed, guid: episode.guid)
        }

        return DeviceStateSnapshot(
            source: source,
            podcasts: podcastStates,
            episodes: episodeStates,
            queue: queue,
            lastPlayed: lastPlayed
        )
    }

    // MARK: - Podcasts

    private func upsertPodcasts(
        _ dtos: [PodcastDTO],
        options: SyncApplyOptions,
        report: inout SyncApplyReport
    ) throws -> [Int: Podcast] {
        guard !dtos.isEmpty else { return [:] }
        // Last occurrence wins if a page repeats an id.
        var latest: [Int: PodcastDTO] = [:]
        var order: [Int] = []
        for dto in dtos {
            if latest.updateValue(dto, forKey: dto.id) == nil {
                order.append(dto.id)
            }
        }
        var byID = try fetchPodcasts(ids: order)
        for id in order {
            guard let dto = latest[id] else { continue }
            report.seenPodcastIDs.insert(id)
            if let podcast = byID[id] {
                let outcome = update(podcast, from: dto, overrides: options.overrides)
                if outcome.changed {
                    report.changedPodcastIDs.append(id)
                }
                if outcome.artworkChanged {
                    report.podcastIDsNeedingArtwork.append(id)
                }
            } else {
                let podcast = Podcast(
                    serverID: id,
                    feedURL: LenientURL.make(dto.feedUrl) ?? Self.placeholderFeedURL(id: id),
                    title: dto.title,
                    author: dto.author,
                    summary: dto.summary,
                    artworkURL: LenientURL.make(dto.artworkUrl),
                    dateAdded: dto.createdAt ?? options.now,
                    adAnalysisEnabled: options.overrides.podcastAdAnalysis[id] ?? dto.adAnalysisEnabled,
                    autoProcessEnabled: dto.autoProcessEnabled,
                    firstSyncedAt: options.now
                )
                context.insert(podcast)
                _ = update(podcast, from: dto, overrides: options.overrides)
                byID[id] = podcast
                report.insertedPodcastIDs.append(id)
                if podcast.artworkURL != nil {
                    report.podcastIDsNeedingArtwork.append(id)
                }
            }
        }
        return byID
    }

    /// Server-mirror fields only; every assignment is guarded.
    private func update(
        _ podcast: Podcast,
        from dto: PodcastDTO,
        overrides: SyncOverrides
    ) -> (changed: Bool, artworkChanged: Bool) {
        var changed = false
        var artworkChanged = false
        var snapshotChanged = false

        if let feedURL = LenientURL.make(dto.feedUrl), podcast.feedURL != feedURL {
            podcast.feedURL = feedURL
            changed = true
        }
        if !dto.title.isEmpty, podcast.title != dto.title {
            podcast.title = dto.title
            changed = true
            snapshotChanged = true
        }
        if podcast.author != dto.author {
            podcast.author = dto.author
            changed = true
        }
        if podcast.summary != dto.summary {
            podcast.summary = dto.summary
            changed = true
        }
        let artworkURL = LenientURL.make(dto.artworkUrl)
        if podcast.artworkURL != artworkURL {
            podcast.artworkURL = artworkURL
            changed = true
            artworkChanged = true
            snapshotChanged = true
        }
        if let createdAt = dto.createdAt, podcast.dateAdded != createdAt {
            podcast.dateAdded = createdAt
            changed = true
        }
        if podcast.lastFetched != dto.lastFetchAt {
            podcast.lastFetched = dto.lastFetchAt
            changed = true
        }
        if podcast.lastFetchError != dto.lastFetchError {
            podcast.lastFetchError = dto.lastFetchError
            changed = true
        }
        let adAnalysisEnabled = overrides.podcastAdAnalysis[dto.id] ?? dto.adAnalysisEnabled
        if podcast.adAnalysisEnabled != adAnalysisEnabled {
            podcast.adAnalysisEnabled = adAnalysisEnabled
            changed = true
        }
        if podcast.autoProcessEnabled != dto.autoProcessEnabled {
            podcast.autoProcessEnabled = dto.autoProcessEnabled
            changed = true
        }
        if podcast.latestEpisodeAt != dto.latestEpisodeAt {
            podcast.latestEpisodeAt = dto.latestEpisodeAt
            changed = true
        }
        if podcast.episodeCount != dto.episodeCount {
            podcast.episodeCount = dto.episodeCount
            changed = true
        }
        if snapshotChanged {
            podcast.syncEpisodeSnapshots()
        }
        return (changed, artworkChanged)
    }

    // MARK: - Episodes

    private func upsertEpisodes(
        _ dtos: [EpisodeDTO],
        knownPodcasts: [Int: Podcast],
        options: SyncApplyOptions,
        report: inout SyncApplyReport
    ) throws {
        guard !dtos.isEmpty else { return }
        var latest: [Int: EpisodeDTO] = [:]
        var order: [Int] = []
        for dto in dtos {
            if latest.updateValue(dto, forKey: dto.id) == nil {
                order.append(dto.id)
            }
        }
        let existing = try fetchEpisodes(ids: order)

        var podcasts = knownPodcasts
        let missingPodcastIDs = Set(latest.values.map(\.podcastId)).subtracting(podcasts.keys)
        if !missingPodcastIDs.isEmpty {
            for (id, podcast) in try fetchPodcasts(ids: Array(missingPodcastIDs)) {
                podcasts[id] = podcast
            }
        }

        for id in order {
            guard let dto = latest[id] else { continue }
            report.seenEpisodeIDs.insert(id)
            if let episode = existing[id] {
                let wasPresent = episode.audioState.isPresent
                var changed = false
                if episode.podcastServerID != dto.podcastId, let podcast = podcasts[dto.podcastId] {
                    episode.podcast = podcast
                    episode.podcastServerID = dto.podcastId
                    episode.syncPodcastSnapshot(from: podcast)
                    changed = true
                }
                if update(episode, from: dto) {
                    changed = true
                }
                let markers = syncMarkers(of: episode, with: dto, isNew: false)
                if markers.anyChange {
                    changed = true
                }
                if markers.contentChanged {
                    report.markersChangedEpisodeIDs.append(id)
                }
                if changed {
                    report.changedEpisodeIDs.append(id)
                }
                if !wasPresent, episode.audioState.isPresent {
                    report.audioBecamePresentEpisodeIDs.append(id)
                }
            } else {
                guard let podcast = podcasts[dto.podcastId] else {
                    report.orphanedEpisodeIDs.append(id)
                    Log.sync.error("Episode \(id) references unknown podcast \(dto.podcastId); skipped")
                    continue
                }
                let episode = Episode(
                    serverID: id,
                    podcastServerID: dto.podcastId,
                    guid: dto.guid,
                    title: dto.title,
                    episodeDescription: dto.description,
                    publishedAt: dto.publishedAt,
                    duration: dto.durationSeconds,
                    enclosureURL: LenientURL.make(dto.enclosureUrl),
                    audioMimeType: dto.enclosureType,
                    podcast: podcast
                )
                context.insert(episode)
                _ = update(episode, from: dto)
                _ = syncMarkers(of: episode, with: dto, isNew: true)
                report.insertedEpisodeIDs.append(id)
                if dto.audioState.isPresent {
                    report.audioBecamePresentEpisodeIDs.append(id)
                }
                if shouldAutoQueue(dto, podcast: podcast, options: options) {
                    report.autoQueueEpisodeIDs.append(id)
                }
            }
        }
    }

    /// Server-mirror fields only; every assignment is guarded. Returns
    /// whether anything changed.
    private func update(_ episode: Episode, from dto: EpisodeDTO) -> Bool {
        var changed = false
        if !dto.guid.isEmpty, episode.guid != dto.guid {
            episode.guid = dto.guid
            changed = true
        }
        if episode.title != dto.title {
            episode.title = dto.title
            changed = true
        }
        if episode.episodeDescription != dto.description {
            episode.episodeDescription = dto.description
            changed = true
        }
        if episode.publishedAt != dto.publishedAt {
            episode.publishedAt = dto.publishedAt
            changed = true
        }
        if episode.duration != dto.durationSeconds {
            episode.duration = dto.durationSeconds
            changed = true
        }
        if episode.durationIsMeasured != dto.durationIsMeasured {
            episode.durationIsMeasured = dto.durationIsMeasured
            changed = true
        }
        let enclosureURL = LenientURL.make(dto.enclosureUrl)
        if episode.enclosureURL != enclosureURL {
            episode.enclosureURL = enclosureURL
            changed = true
        }
        if episode.audioMimeType != dto.enclosureType {
            episode.audioMimeType = dto.enclosureType
            changed = true
        }
        if episode.audioStateRaw != dto.audioState.rawValue {
            episode.audioStateRaw = dto.audioState.rawValue
            changed = true
        }
        if episode.audioBytes != dto.audioBytes {
            episode.audioBytes = dto.audioBytes
            changed = true
        }
        if episode.audioSha256 != dto.audioSha256 {
            episode.audioSha256 = dto.audioSha256
            changed = true
        }
        if episode.audioContentType != dto.audioContentType {
            episode.audioContentType = dto.audioContentType
            changed = true
        }
        if episode.serverStateRaw != dto.state.rawValue {
            changed = true
        }
        let wasBusy = episode.isBusy
        episode.applyServerState(dto.state.rawValue)
        if episode.isBusy != wasBusy {
            changed = true
        }
        if episode.serverError != dto.error {
            episode.serverError = dto.error
            changed = true
        }
        if episode.transcriptStateRaw != dto.transcriptState.rawValue {
            episode.transcriptStateRaw = dto.transcriptState.rawValue
            changed = true
        }
        if episode.classifyStateRaw != dto.classifyState.rawValue {
            episode.classifyStateRaw = dto.classifyState.rawValue
            changed = true
        }
        return changed
    }

    /// Replaces the episode's markers wholesale when they differ from the
    /// server's (markers have no independent sync identity). The revision +
    /// count fast path avoids faulting the relationship for unchanged
    /// episodes.
    private func syncMarkers(
        of episode: Episode,
        with dto: EpisodeDTO,
        isNew: Bool
    ) -> (contentChanged: Bool, anyChange: Bool) {
        let incoming = Self.sanitizedMarkers(dto.adMarkers)
        if !isNew,
           episode.markerRevision == dto.markerRevision,
           episode.activeAdMarkerCount == incoming.count {
            return (false, false)
        }

        var contentChanged = false
        var anyChange = false
        var current: [MarkerSignature] = []
        if !isNew {
            for marker in episode.adMarkers where !marker.isDeleted {
                current.append(MarkerSignature(
                    startSeconds: marker.startSeconds,
                    endSeconds: marker.endSeconds,
                    kindRaw: marker.kind.rawValue,
                    summary: marker.summary,
                    manual: marker.manuallyEdited
                ))
            }
            current.sort()
        }
        if current != incoming {
            for marker in episode.adMarkers {
                context.delete(marker)
            }
            for signature in incoming {
                let marker = AdMarker(
                    startSeconds: signature.startSeconds,
                    endSeconds: signature.endSeconds,
                    summary: signature.summary,
                    kind: SegmentKind(rawValue: signature.kindRaw) ?? .ad,
                    manuallyEdited: signature.manual,
                    episode: episode
                )
                context.insert(marker)
            }
            contentChanged = true
            anyChange = true
        }
        if episode.activeAdMarkerCount != incoming.count {
            episode.activeAdMarkerCount = incoming.count
            anyChange = true
        }
        if episode.markerRevision != dto.markerRevision {
            episode.markerRevision = dto.markerRevision
            anyChange = true
        }
        return (contentChanged, anyChange)
    }

    /// Validates server markers through `DetectedAd` (finite, non-empty,
    /// non-negative). Deliberately passes no duration: the server already
    /// clamps to its measured duration and extends outros only when the tail
    /// is silence, and `DetectedAd`'s unconditional outro extension would
    /// undo that. The player clamps to the real item duration
    /// (`AdRegion.sanitized`).
    nonisolated static func sanitizedMarkers(_ markers: [AdMarkerDTO]) -> [MarkerSignature] {
        markers.compactMap { dto -> MarkerSignature? in
            let detected = DetectedAd(
                startSeconds: dto.startSeconds,
                endSeconds: dto.endSeconds,
                summary: dto.summary ?? "",
                kind: dto.segmentKind
            )
            guard let sane = detected.sanitized(episodeDuration: nil) else { return nil }
            return MarkerSignature(
                startSeconds: sane.startSeconds,
                endSeconds: sane.endSeconds,
                kindRaw: sane.kind.rawValue,
                summary: sane.summary,
                manual: dto.isManual
            )
        }
        .sorted()
    }

    private func shouldAutoQueue(_ dto: EpisodeDTO, podcast: Podcast, options: SyncApplyOptions) -> Bool {
        guard options.allowAutoQueue, podcast.autoDownloadEnabled else { return false }
        guard let published = dto.publishedAt else { return false }
        // Published after this podcast first appeared on the device: a
        // genuinely new episode, not the archive of a fresh subscription or
        // a rebuilt mirror.
        guard published > podcast.firstSyncedAt else { return false }
        return options.now.timeIntervalSince(published) <= SyncApplyOptions.autoQueueWindow
    }

    // MARK: - Deletions

    private func applyDeletions(_ deletions: [DeletionDTO], report: inout SyncApplyReport) throws {
        guard !deletions.isEmpty else { return }
        var podcastIDs: [Int] = []
        var episodeIDs: [Int] = []
        for deletion in deletions {
            switch deletion.entity.lowercased() {
            case "podcast": podcastIDs.append(deletion.id)
            case "episode": episodeIDs.append(deletion.id)
            default: continue
            }
        }
        if !episodeIDs.isEmpty {
            report.deletedEpisodeIDs += try deleteEpisodeRows(Array(try fetchEpisodes(ids: episodeIDs).values))
        }
        if !podcastIDs.isEmpty {
            let (podcasts, episodes) = try deletePodcastRows(ids: podcastIDs)
            report.deletedPodcastIDs += podcasts
            report.deletedEpisodeIDs += episodes
        }
    }

    /// Deletes episodes with their device-local side effects: queue items
    /// (which have no inverse relationship to nullify), the audio file, and
    /// any resume data.
    private func deleteEpisodeRows(_ episodes: [Episode]) throws -> [Int] {
        guard !episodes.isEmpty else { return [] }
        let doomed = Set(episodes.map(\.serverID))
        for item in try context.fetch(FetchDescriptor<QueueItem>()) {
            if let episode = item.episode, doomed.contains(episode.serverID) {
                context.delete(item)
            }
        }
        for episode in episodes {
            AudioStorage.deleteFile(named: episode.localFilename)
            AudioStorage.deleteResumeData(serverID: episode.serverID)
            context.delete(episode)
        }
        return Array(doomed)
    }

    /// A podcast deletion removes the podcast and all its episodes.
    private func deletePodcastRows(ids: [Int]) throws -> (podcasts: [Int], episodes: [Int]) {
        guard !ids.isEmpty else { return ([], []) }
        let podcasts = try fetchPodcasts(ids: ids)
        var deletedEpisodes: [Int] = []
        for podcast in podcasts.values {
            let episodes = try fetchEpisodes(podcastServerID: podcast.serverID)
            deletedEpisodes += try deleteEpisodeRows(episodes)
            if let filename = podcast.cachedArtworkFilename {
                try? FileManager.default.removeItem(at: ArtworkService.localURL(filename: filename))
            }
            context.delete(podcast)
        }
        return (Array(podcasts.keys), deletedEpisodes)
    }

    // MARK: - Settings

    private func applySettings(_ dto: ServerSettingsDTO, overrides: SyncOverrides, report: inout SyncApplyReport) {
        let settings: AppSettings
        if let existing = try? context.fetch(FetchDescriptor<AppSettings>()).first {
            settings = existing
        } else {
            settings = AppSettings()
            context.insert(settings)
        }
        var changed = false
        let adAnalysisEnabled = overrides.globalAdAnalysisEnabled ?? dto.adAnalysisEnabled
        if settings.adAnalysisEnabled != adAnalysisEnabled {
            settings.adAnalysisEnabled = adAnalysisEnabled
            changed = true
        }
        if settings.serverAutoProcessEnabled != dto.autoProcessEnabled {
            settings.serverAutoProcessEnabled = dto.autoProcessEnabled
            changed = true
        }
        if settings.serverClassifier != dto.classifier {
            settings.serverClassifier = dto.classifier
            changed = true
        }
        if settings.serverClassifierModel != dto.classifierModel {
            settings.serverClassifierModel = dto.classifierModel
            changed = true
        }
        if settings.serverFeedIntervalMinutes != dto.feedIntervalMinutes {
            settings.serverFeedIntervalMinutes = dto.feedIntervalMinutes
            changed = true
        }
        let openRouterAvailable = dto.availableClassifiers["openrouter"]
        if settings.serverOpenRouterAvailable != openRouterAvailable {
            settings.serverOpenRouterAvailable = openRouterAvailable
            changed = true
        }
        report.settingsChanged = changed
    }

    // MARK: - Fetch helpers

    private func fetchPodcasts(ids: [Int]) throws -> [Int: Podcast] {
        var result: [Int: Podcast] = [:]
        for batch in Self.batches(of: ids) {
            let descriptor = FetchDescriptor<Podcast>(
                predicate: #Predicate<Podcast> { batch.contains($0.serverID) }
            )
            for podcast in try context.fetch(descriptor) {
                result[podcast.serverID] = podcast
            }
        }
        return result
    }

    private func fetchEpisodes(ids: [Int]) throws -> [Int: Episode] {
        var result: [Int: Episode] = [:]
        for batch in Self.batches(of: ids) {
            let descriptor = FetchDescriptor<Episode>(
                predicate: #Predicate<Episode> { batch.contains($0.serverID) }
            )
            for episode in try context.fetch(descriptor) {
                result[episode.serverID] = episode
            }
        }
        return result
    }

    private func fetchEpisodes(podcastServerID: Int) throws -> [Episode] {
        let descriptor = FetchDescriptor<Episode>(
            predicate: #Predicate<Episode> { $0.podcastServerID == podcastServerID }
        )
        return try context.fetch(descriptor)
    }

    private func saveIfNeeded() throws -> Bool {
        guard context.hasChanges else { return false }
        try context.save()
        return true
    }

    /// ≤250 unique ids per `#Predicate`.
    nonisolated static func batches(of ids: [Int], size: Int = 250) -> [[Int]] {
        let unique = Array(Set(ids)).sorted()
        guard !unique.isEmpty else { return [] }
        return stride(from: 0, to: unique.count, by: size).map { start in
            Array(unique[start..<min(start + size, unique.count)])
        }
    }

    nonisolated static func placeholderFeedURL(id: Int) -> URL {
        URL(string: "noadcast-invalid-feed://podcast/\(id)") ?? URL(fileURLWithPath: "/")
    }
}

/// Content identity of one marker, for "did the server's set change?".
nonisolated struct MarkerSignature: Sendable, Equatable, Comparable {
    let startSeconds: Double
    let endSeconds: Double
    let kindRaw: String
    let summary: String
    let manual: Bool

    init(startSeconds: Double, endSeconds: Double, kindRaw: String, summary: String, manual: Bool) {
        self.startSeconds = startSeconds
        self.endSeconds = endSeconds
        self.kindRaw = kindRaw
        self.summary = summary
        self.manual = manual
    }

    static func < (lhs: MarkerSignature, rhs: MarkerSignature) -> Bool {
        if lhs.startSeconds != rhs.startSeconds { return lhs.startSeconds < rhs.startSeconds }
        if lhs.endSeconds != rhs.endSeconds { return lhs.endSeconds < rhs.endSeconds }
        if lhs.kindRaw != rhs.kindRaw { return lhs.kindRaw < rhs.kindRaw }
        if lhs.summary != rhs.summary { return lhs.summary < rhs.summary }
        return !lhs.manual && rhs.manual
    }
}
