import Foundation
import SwiftData
import BackgroundTasks
import Observation
import os

nonisolated enum SyncTrigger: String, Sendable {
    case launch
    case foreground
    case pullToRefresh
    case mutation
    case jobTransition
    case backgroundRefresh
    case configuration
    case fullResync
    case coalesced
    case manual
}

nonisolated enum SyncError: Error, LocalizedError, Equatable {
    case cursorDidNotAdvance(since: Int)
    case instanceChangedDuringResync

    var errorDescription: String? {
        switch self {
        case .cursorDidNotAdvance(let since): "The server's sync cursor did not advance past \(since)."
        case .instanceChangedDuringResync: "The server was replaced during a resync. Try again."
        }
    }
}

/// Persistent flags that must survive a relaunch mid-sync.
nonisolated enum SyncFlags {
    static let needsFullResyncKey = "SyncNeedsFullResync"

    /// Set while a full resync with sweep (after `410`, or "re-sync from
    /// scratch") is in progress, so an interrupted one restarts from 0.
    static var needsFullResync: Bool {
        get { UserDefaults.standard.bool(forKey: needsFullResyncKey) }
        set { UserDefaults.standard.set(newValue, forKey: needsFullResyncKey) }
    }
}

/// The small persisted list of retention releases (`DELETE …/audio`) that
/// could not be sent (offline). Retried on the next sync. Scoped to one
/// server instance: ids are meaningless across a rebuild.
nonisolated enum PendingReleaseStore {
    static let idsKey = "PendingAudioReleases.ids"
    static let instanceKey = "PendingAudioReleases.instance"

    static func load() -> (instanceId: String?, ids: [Int]) {
        let defaults = UserDefaults.standard
        let ids = (defaults.array(forKey: idsKey) as? [Int]) ?? []
        return (defaults.string(forKey: instanceKey), ids)
    }

    static func add(_ id: Int, instanceId: String?) {
        let defaults = UserDefaults.standard
        var ids = (defaults.array(forKey: idsKey) as? [Int]) ?? []
        if let stored = defaults.string(forKey: instanceKey), let instanceId, stored != instanceId {
            ids = []
        }
        if !ids.contains(id) {
            ids.append(id)
        }
        defaults.set(ids, forKey: idsKey)
        if let instanceId {
            defaults.set(instanceId, forKey: instanceKey)
        }
    }

    static func remove(_ id: Int) {
        let defaults = UserDefaults.standard
        var ids = (defaults.array(forKey: idsKey) as? [Int]) ?? []
        ids.removeAll { $0 == id }
        defaults.set(ids, forKey: idsKey)
    }

    static func clear() {
        UserDefaults.standard.removeObject(forKey: idsKey)
        UserDefaults.standard.removeObject(forKey: instanceKey)
    }
}

/// Orchestrates the offline mirror: delta sync (`/sync`), job-progress
/// polling (`/jobs/active`), background refresh, retention releases, and the
/// optimistic server-setting toggles. Page application itself happens off
/// the main actor in `SyncEngine`.
///
/// Triggers: launch, foreground after 120 s, pull-to-refresh, after
/// mutations, when `/jobs/active` shows an item finishing or changing
/// state, and `BGAppRefreshTask` (`backgroundRefreshTaskIdentifier`).
@MainActor
@Observable
final class SyncService {
    static let shared = SyncService(
        api: NoadcastAPIClient.shared,
        restoreService: DeviceStateRestoreService.shared,
        performsSideEffects: true,
        isConfigured: { APIConfiguration.isConfigured }
    )

    nonisolated static let backgroundRefreshTaskIdentifier = "com.isaackhor.Noadcast.refresh"
    static let pageLimit = 500
    static let foregroundResyncInterval: TimeInterval = 120
    static let backgroundDeadlineSeconds: Double = 20
    static let backgroundRefreshInterval: TimeInterval = 30 * 60

    // MARK: Observable state

    private(set) var isSyncing = false
    private(set) var lastSyncAt: Date?
    private(set) var lastError: String?
    /// The server rejected the token. Drives the banner; polling stops.
    private(set) var authFailed = false
    /// In-memory job progress keyed by episode `serverID`. Never written to
    /// SwiftData: a 2 s poll must not invalidate any `@Query`.
    private(set) var activeJobs: [Int: ActiveJobDTO] = [:]
    private(set) var instanceId: String?

    // MARK: Dependencies

    let api: NoadcastAPIClient
    let restoreService: DeviceStateRestoreService
    /// `false` in unit tests: no player / download / artwork / queue side
    /// effects on the app-wide singletons.
    let performsSideEffects: Bool
    /// Injectable so tests can sync against a stubbed `URLSession` without
    /// a server address in `UserDefaults`.
    let isConfigured: () -> Bool

    @ObservationIgnored private(set) var container: ModelContainer?
    @ObservationIgnored private(set) var engine: SyncEngine?

    @ObservationIgnored private var runningSync: Task<Void, Never>?
    @ObservationIgnored private var syncRequestedWhileRunning = false
    @ObservationIgnored private var lastSyncAttemptAt: Date?
    @ObservationIgnored private var pollTask: Task<Void, Never>?
    @ObservationIgnored private var jobsETag: String?
    @ObservationIgnored private var downloadsTabVisible = false
    @ObservationIgnored private var isInForeground = true
    @ObservationIgnored private var followUpTask: Task<Void, Never>?
    @ObservationIgnored private var globalAdAnalysisOverride: Bool?
    @ObservationIgnored private var podcastAdAnalysisOverrides: [Int: Bool] = [:]
    @ObservationIgnored private var isFlushingReleases = false

    init(
        api: NoadcastAPIClient,
        restoreService: DeviceStateRestoreService,
        performsSideEffects: Bool,
        isConfigured: @escaping () -> Bool
    ) {
        self.api = api
        self.restoreService = restoreService
        self.performsSideEffects = performsSideEffects
        self.isConfigured = isConfigured
    }

    func configure(container: ModelContainer) {
        self.container = container
        let engine = SyncEngine(modelContainer: container)
        self.engine = engine
        Task { @MainActor [weak self] in
            let state = await engine.cursorState()
            self?.lastSyncAt = state.lastSyncAt
            self?.instanceId = state.instanceId
        }
    }

    // MARK: - Lifecycle

    /// Once per process, after the container is ready.
    func launch() async {
        guard isConfigured() else { return }
        await syncNow(.launch)
        startJobPolling()
    }

    func appDidBecomeActive() {
        isInForeground = true
        guard isConfigured() else { return }
        startJobPolling()
        let stale = lastSyncAttemptAt.map { Date().timeIntervalSince($0) > Self.foregroundResyncInterval } ?? true
        if stale {
            Task { await self.syncNow(.foreground) }
        } else {
            Task { await self.flushPendingReleases() }
        }
    }

    func appDidEnterBackground() {
        isInForeground = false
        stopJobPolling()
        scheduleBackgroundRefresh()
    }

    /// After the user saves a new server address or token.
    func serverConfigurationDidChange() async {
        setAuthFailed(false)
        jobsETag = nil
        activeJobs = [:]
        stopJobPolling()
        await syncNow(.configuration)
        startJobPolling()
    }

    /// Forgets the credentials; local data stays so downloads keep playing.
    func signOut(forgetServerAddress: Bool) {
        APIConfiguration.signOut(forgetServerAddress: forgetServerAddress)
        stopJobPolling()
        cancelRunningSync()
        followUpTask?.cancel()
        activeJobs = [:]
        jobsETag = nil
        authFailed = false
        lastError = nil
        if performsSideEffects {
            DownloadManager.shared.cancelAll(keepResumeData: true)
        }
    }

    func setAuthFailed(_ failed: Bool) {
        guard authFailed != failed else { return }
        authFailed = failed
        if failed {
            stopJobPolling()
        } else if isInForeground {
            startJobPolling()
        }
    }

    // MARK: - Sync

    /// Runs a sync, or joins the one in flight (and schedules one more pass
    /// after it, so a trigger that arrives mid-run is never lost).
    func syncNow(_ trigger: SyncTrigger) async {
        guard engine != nil, isConfigured() else { return }
        if let running = runningSync {
            syncRequestedWhileRunning = true
            await running.value
            return
        }
        let task = Task { @MainActor [weak self] in
            await self?.runSyncLoop(trigger)
        }
        runningSync = task
        await task.value
        runningSync = nil
        if syncRequestedWhileRunning {
            syncRequestedWhileRunning = false
            await syncNow(.coalesced)
        }
    }

    func requestSync(_ trigger: SyncTrigger) {
        Task { await self.syncNow(trigger) }
    }

    func cancelRunningSync() {
        runningSync?.cancel()
        syncRequestedWhileRunning = false
    }

    /// Full resync from `since = 0`, then removes local rows the server no
    /// longer has. Device-local state on surviving rows is untouched.
    func resyncFromScratch() async {
        SyncFlags.needsFullResync = true
        await syncNow(.fullResync)
    }

    /// Drops the mirror and every downloaded file, keeps playback positions,
    /// played flags, queue order and per-podcast preferences (re-attached by
    /// `feedURL + guid` after the full sync).
    func resetLocalCache() async {
        guard let engine else { return }
        cancelRunningSync()
        if performsSideEffects {
            PlayerService.shared.unload()
            DownloadManager.shared.cancelAll(keepResumeData: false)
        }
        do {
            let snapshot = try await engine.snapshotDeviceState(source: .localReset, includeFiles: false)
            let state = await engine.cursorState()
            try await engine.wipeMirrors(newInstanceId: state.instanceId)
            restoreService.discardAllJobs()
            for name in AudioStorage.listEpisodeFiles() {
                AudioStorage.deleteFile(named: name)
            }
            AudioStorage.deleteAllResumeData()
            if !snapshot.isEmpty {
                restoreService.enqueue(snapshot, subscribeMissingFeeds: false)
            }
            SyncFlags.needsFullResync = false
            activeJobs = [:]
            jobsETag = nil
        } catch {
            lastError = error.localizedDescription
            Log.sync.error("Reset local cache failed: \(Log.describe(error), privacy: .public)")
        }
        await syncNow(.fullResync)
    }

    /// A few extra syncs after a server-side refresh was requested (it runs
    /// asynchronously, so new episodes land seconds later).
    func scheduleFollowUpSyncs(delays: [Double] = [4, 12, 30]) {
        followUpTask?.cancel()
        followUpTask = Task { @MainActor [weak self] in
            for delay in delays {
                try? await Task.sleep(for: .seconds(delay))
                if Task.isCancelled { return }
                await self?.syncNow(.mutation)
            }
        }
    }

    private func runSyncLoop(_ trigger: SyncTrigger) async {
        guard let engine else { return }
        isSyncing = true
        lastSyncAttemptAt = Date()
        defer { isSyncing = false }
        do {
            try await performSync(engine: engine, trigger: trigger)
            lastError = nil
        } catch APIError.cancelled {
            // Deadline or suspension. Only fully applied pages advanced the cursor.
        } catch {
            lastError = error.localizedDescription
            Log.sync.error("Sync (\(trigger.rawValue, privacy: .public)) failed: \(Log.describe(error), privacy: .public)")
        }
    }

    private func performSync(engine: SyncEngine, trigger: SyncTrigger) async throws {
        var cursor = await engine.cursorState()
        var sweepAfterwards = SyncFlags.needsFullResync
        var since = sweepAfterwards ? 0 : cursor.nextSince
        var isFullSync = since == 0
        var seenPodcasts = Set<Int>()
        var seenEpisodes = Set<Int>()
        var aggregate = SyncApplyReport()
        var restartedForInstance = false

        while true {
            if Task.isCancelled {
                throw APIError.cancelled
            }
            let page: SyncPageDTO
            do {
                page = try await api.sync(since: since, limit: Self.pageLimit)
            } catch APIError.cursorExpired {
                guard since != 0 else { throw APIError.cursorExpired }
                let expiredCursor = since
                Log.sync.notice("Cursor \(expiredCursor) expired; full resync with sweep")
                SyncFlags.needsFullResync = true
                sweepAfterwards = true
                since = 0
                isFullSync = true
                seenPodcasts = []
                seenEpisodes = []
                aggregate = SyncApplyReport()
                continue
            }

            if let known = cursor.instanceId, let incoming = page.instanceId, known != incoming {
                guard !restartedForInstance else { throw SyncError.instanceChangedDuringResync }
                restartedForInstance = true
                try await handleInstanceChange(engine: engine, newInstanceId: incoming)
                cursor = await engine.cursorState()
                sweepAfterwards = false
                SyncFlags.needsFullResync = false
                seenPodcasts = []
                seenEpisodes = []
                aggregate = SyncApplyReport()
                isFullSync = true
                if since != 0 {
                    // That page was a delta against the old database.
                    since = 0
                    continue
                }
            }

            unloadPlayerIfDeleted(by: page)
            let options = SyncApplyOptions(
                allowAutoQueue: performsSideEffects,
                overrides: currentOverrides(),
                now: Date()
            )
            let report = try await engine.apply(page: page, options: options)
            if cursor.instanceId == nil, let incoming = page.instanceId {
                cursor.instanceId = incoming
            }
            if let incoming = page.instanceId, instanceId != incoming {
                instanceId = incoming
            }
            seenPodcasts.formUnion(report.seenPodcastIDs)
            seenEpisodes.formUnion(report.seenEpisodeIDs)
            aggregate.merge(report)
            handleAppliedPage(report)

            if !page.hasMore {
                break
            }
            guard page.nextSince > since else {
                throw SyncError.cursorDidNotAdvance(since: since)
            }
            since = page.nextSince
        }

        if sweepAfterwards {
            let unseen = try await engine.unseenMirrorIDs(seenPodcastIDs: seenPodcasts, seenEpisodeIDs: seenEpisodes)
            if !unseen.podcasts.isEmpty || !unseen.episodes.isEmpty {
                Log.sync.notice("Full resync removed \(unseen.podcasts.count) podcast(s) and \(unseen.episodes.count) episode(s) the server no longer has")
                unloadPlayerIfAffected(podcastIDs: unseen.podcasts, episodeIDs: unseen.episodes)
                let deleted = try await engine.deleteMirrors(podcastIDs: unseen.podcasts, episodeIDs: unseen.episodes)
                if performsSideEffects {
                    DownloadManager.shared.forget(episodeServerIDs: deleted)
                }
            }
            SyncFlags.needsFullResync = false
        }

        let now = Date()
        try await engine.markSyncCompleted(at: now, full: isFullSync)
        lastSyncAt = now

        let wantsFollowUp = await restoreService.runPendingRestores()
        if wantsFollowUp {
            scheduleFollowUpSyncs()
        }
        if performsSideEffects {
            await afterSuccessfulSync(aggregate)
        }
    }

    private func handleInstanceChange(engine: SyncEngine, newInstanceId: String) async throws {
        Log.sync.notice("Server instanceId changed; wiping the mirror and re-attaching device-local state after a full sync")
        if performsSideEffects {
            PlayerService.shared.unload()
            DownloadManager.shared.cancelAll(keepResumeData: false)
        }
        let snapshot = try await engine.snapshotDeviceState(source: .instanceChange, includeFiles: true)
        if !snapshot.isEmpty {
            restoreService.enqueue(snapshot, subscribeMissingFeeds: false)
        }
        try await engine.wipeMirrors(newInstanceId: newInstanceId)
        if performsSideEffects {
            // Ids from the old database are meaningless now.
            PendingReleaseStore.clear()
            AudioStorage.deleteAllResumeData()
        }
        activeJobs = [:]
        jobsETag = nil
        instanceId = newInstanceId
    }

    private func currentOverrides() -> SyncOverrides {
        SyncOverrides(
            globalAdAnalysisEnabled: globalAdAnalysisOverride,
            podcastAdAnalysis: podcastAdAnalysisOverrides
        )
    }

    /// The player must let go of an episode before its row disappears.
    private func unloadPlayerIfDeleted(by page: SyncPageDTO) {
        guard performsSideEffects, !page.deletions.isEmpty else { return }
        let podcastIDs = page.deletions.filter { $0.entity.lowercased() == "podcast" }.map(\.id)
        let episodeIDs = page.deletions.filter { $0.entity.lowercased() == "episode" }.map(\.id)
        unloadPlayerIfAffected(podcastIDs: podcastIDs, episodeIDs: episodeIDs)
    }

    private func unloadPlayerIfAffected(podcastIDs: [Int], episodeIDs: [Int]) {
        guard performsSideEffects else { return }
        let player = PlayerService.shared
        if let episode = player.currentEpisodeServerID, episodeIDs.contains(episode) {
            player.unload()
            return
        }
        if let podcast = player.currentPodcastServerID, podcastIDs.contains(podcast) {
            player.unload()
        }
    }

    /// Per-page side effects that should not wait for the whole sync.
    private func handleAppliedPage(_ report: SyncApplyReport) {
        guard performsSideEffects else { return }
        let player = PlayerService.shared
        if let current = player.currentEpisodeServerID {
            if report.markersChangedEpisodeIDs.contains(current) {
                player.adMarkersDidChange(episodeServerID: current)
            }
            if report.audioBecamePresentEpisodeIDs.contains(current) {
                player.serverAudioBecameAvailable(episodeServerID: current)
            }
        }
        if !report.deletedEpisodeIDs.isEmpty {
            DownloadManager.shared.forget(episodeServerIDs: report.deletedEpisodeIDs)
        }
    }

    private func afterSuccessfulSync(_ report: SyncApplyReport) async {
        if !report.podcastIDsNeedingArtwork.isEmpty {
            let ids = report.podcastIDsNeedingArtwork
            Task { @MainActor [weak self] in
                await self?.cacheArtwork(forPodcastIDs: ids)
            }
        }
        if let context = container?.mainContext, !report.autoQueueEpisodeIDs.isEmpty {
            SubscriptionService.shared.enqueueNewEpisodes(serverIDs: report.autoQueueEpisodeIDs, in: context)
        }
        await flushPendingReleases()
        if let context = container?.mainContext {
            SubscriptionService.shared.processQueuedEpisodes(context: context)
        }
        DownloadManager.shared.startEligibleDownloads()
    }

    private func cacheArtwork(forPodcastIDs ids: [Int]) async {
        guard let context = container?.mainContext else { return }
        for batch in SyncEngine.batches(of: ids) {
            let descriptor = FetchDescriptor<Podcast>(
                predicate: #Predicate<Podcast> { batch.contains($0.serverID) }
            )
            let podcasts = (try? context.fetch(descriptor)) ?? []
            for podcast in podcasts {
                await ArtworkService.shared.cache(for: podcast)
            }
        }
        if context.hasChanges {
            try? context.save()
        }
    }

    /// Mirrors one episode fetched out of band (the player's "Preparing…"
    /// poll), write-if-changed like any page.
    @discardableResult
    func applyEpisode(_ dto: EpisodeDTO) async -> SyncApplyReport? {
        guard let engine else { return nil }
        let report = try? await engine.applyEpisodes([dto])
        if let report {
            handleAppliedPage(report)
        }
        return report
    }

    func applyPodcasts(_ dtos: [PodcastDTO]) async {
        guard let engine, !dtos.isEmpty else { return }
        if let report = try? await engine.applyPodcasts(dtos, overrides: currentOverrides()),
           performsSideEffects, !report.podcastIDsNeedingArtwork.isEmpty {
            await cacheArtwork(forPodcastIDs: report.podcastIDsNeedingArtwork)
        }
    }

    // MARK: - Job progress polling

    /// 2 s while the Downloads tab is visible, 5 s in the foreground with
    /// active jobs, 30 s idle; stopped in the background.
    private var pollInterval: Double {
        if downloadsTabVisible { return 2 }
        return activeJobs.isEmpty ? 30 : 5
    }

    func startJobPolling() {
        guard pollTask == nil, isInForeground, !authFailed, isConfigured() else { return }
        pollTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.pollJobsOnce()
                let interval = self.pollInterval
                try? await Task.sleep(for: .seconds(interval))
            }
        }
    }

    func stopJobPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    func setDownloadsTabVisible(_ visible: Bool) {
        guard downloadsTabVisible != visible else { return }
        downloadsTabVisible = visible
        // Restart so a pending 30 s sleep doesn't delay the 2 s cadence.
        if pollTask != nil {
            stopJobPolling()
            startJobPolling()
        }
    }

    /// A 304 changes nothing — no SwiftData writes happen on this path at
    /// all; progress lives in `activeJobs`.
    func pollJobsOnce() async {
        guard isConfigured(), !authFailed else { return }
        let result: ActiveJobsResult
        do {
            result = try await api.activeJobs(ifNoneMatch: jobsETag)
        } catch {
            return
        }
        guard case .updated(let items, let etag) = result else { return }
        jobsETag = etag
        var next: [Int: ActiveJobDTO] = [:]
        for item in items {
            next[item.episodeId] = item
        }
        let previous = activeJobs
        let finished = !Set(previous.keys).subtracting(next.keys).isEmpty
        let transitioned = next.contains { entry in
            previous[entry.key]?.state != entry.value.state
        }
        if next != previous {
            activeJobs = next
        }
        if finished || transitioned {
            requestSync(.jobTransition)
        }
    }

    // MARK: - Background refresh

    func scheduleBackgroundRefresh() {
        let request = BGAppRefreshTaskRequest(identifier: Self.backgroundRefreshTaskIdentifier)
        request.earliestBeginDate = Date(timeIntervalSinceNow: Self.backgroundRefreshInterval)
        do {
            try BGTaskScheduler.shared.submit(request)
        } catch {
            Log.sync.notice("Background refresh not scheduled: \(error.localizedDescription, privacy: .public)")
        }
    }

    /// `BGAppRefreshTask` handler: reschedule first, then sync, release,
    /// and start eligible background-session downloads, all within a 20 s
    /// self-imposed deadline.
    func performBackgroundRefresh() async {
        scheduleBackgroundRefresh()
        guard isConfigured(), engine != nil else { return }
        let work = Task { @MainActor [weak self] in
            guard let self else { return }
            await self.syncNow(.backgroundRefresh)
            await self.flushPendingReleases()
            if self.performsSideEffects, let context = self.container?.mainContext {
                SubscriptionService.shared.processQueuedEpisodes(context: context)
                DownloadManager.shared.startEligibleDownloads()
            }
        }
        let deadline = Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(Self.backgroundDeadlineSeconds))
            if Task.isCancelled { return }
            work.cancel()
            self?.cancelRunningSync()
        }
        await withTaskCancellationHandler {
            await work.value
        } onCancel: {
            work.cancel()
            Task { @MainActor [weak self] in
                self?.cancelRunningSync()
            }
        }
        deadline.cancel()
    }

    // MARK: - Retention release

    /// Tells the server it may delete its audio copy because the user
    /// finished (or dismissed as played) the episode. Fire-and-forget; kept
    /// in a small persisted list until it succeeds.
    func releaseAudio(episodeServerID: Int) {
        PendingReleaseStore.add(episodeServerID, instanceId: instanceId)
        Task { await self.flushPendingReleases() }
    }

    func flushPendingReleases() async {
        guard isConfigured(), !authFailed, !isFlushingReleases else { return }
        isFlushingReleases = true
        defer { isFlushingReleases = false }
        var attempted = Set<Int>()
        while true {
            let pending = PendingReleaseStore.load()
            if let stored = pending.instanceId, let current = instanceId, stored != current {
                PendingReleaseStore.clear()
                return
            }
            guard let id = pending.ids.first(where: { !attempted.contains($0) }) else { return }
            attempted.insert(id)
            do {
                try await api.releaseAudio(episodeID: id, reason: .played)
                PendingReleaseStore.remove(id)
            } catch APIError.notFound {
                PendingReleaseStore.remove(id)
            } catch {
                // Offline or server down: keep the rest for the next sync.
                return
            }
        }
    }

    // MARK: - Optimistic server settings

    /// Global "Detect & skip ads" (`PATCH /api/v1/settings`). Applied locally
    /// at once and rolled back if the server refuses.
    func setGlobalAdAnalysis(_ enabled: Bool) async throws {
        guard let context = container?.mainContext else { return }
        let settings = AppSettings.current(in: context)
        let previous = settings.adAnalysisEnabled
        globalAdAnalysisOverride = enabled
        if settings.adAnalysisEnabled != enabled {
            settings.adAnalysisEnabled = enabled
            try? context.save()
        }
        do {
            let dto = try await api.updateSettings(adAnalysisEnabled: enabled)
            globalAdAnalysisOverride = nil
            if settings.adAnalysisEnabled != dto.adAnalysisEnabled {
                settings.adAnalysisEnabled = dto.adAnalysisEnabled
                try? context.save()
            }
        } catch {
            globalAdAnalysisOverride = nil
            if settings.adAnalysisEnabled != previous {
                settings.adAnalysisEnabled = previous
                try? context.save()
            }
            throw error
        }
    }

    /// Per-podcast "Detect & skip ads" (`PATCH /api/v1/podcasts/{id}`).
    func setPodcastAdAnalysis(_ podcast: Podcast, enabled: Bool) async throws {
        guard let context = container?.mainContext else { return }
        let serverID = podcast.serverID
        let previous = podcast.adAnalysisEnabled
        podcastAdAnalysisOverrides[serverID] = enabled
        if podcast.adAnalysisEnabled != enabled {
            podcast.adAnalysisEnabled = enabled
            try? context.save()
        }
        do {
            let dto = try await api.updatePodcast(id: serverID, adAnalysisEnabled: enabled)
            podcastAdAnalysisOverrides[serverID] = nil
            await applyPodcasts([dto])
        } catch {
            podcastAdAnalysisOverrides[serverID] = nil
            if podcast.adAnalysisEnabled != previous {
                podcast.adAnalysisEnabled = previous
                try? context.save()
            }
            throw error
        }
    }
}
