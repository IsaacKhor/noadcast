import Foundation
import SwiftData
import Observation
import os

/// What the nonisolated session delegate hands to the main actor. Every
/// case carries plain values; model objects never cross threads.
nonisolated enum DownloadSessionEvent: Sendable {
    case progress(serverID: Int, taskID: Int, written: Int64, expected: Int64?)
    /// The file is already moved into `AudioStorage.episodesDirectory`.
    case finished(serverID: Int, taskID: Int, filename: String, size: Int64, sha256: String?)
    case httpFailure(serverID: Int, taskID: Int, status: Int, errorCode: String?, message: String?)
    case failed(serverID: Int, taskID: Int, errorCode: Int, message: String, resumeData: Data?, cancelled: Bool)
    case completedCleanly(serverID: Int, taskID: Int)
    case backgroundEventsFinished
}

/// Downloads episode audio from the Noadcast server to the device on a
/// **background** `URLSession`, so transfers continue while the app is
/// suspended and survive a background relaunch.
///
/// Replaces the old per-call `AsyncThrowingStream` design, whose awaiting
/// continuation died with the process. State lives in SwiftData
/// (`Episode.downloadState`), every task carries its episode's `serverID` in
/// `taskDescription`, and at launch `reconcile()` matches `session.allTasks`
/// against the mirror instead of restarting anything still running.
///
/// * Resume data: produced on cancel / transport failure, persisted per
///   episode, used on the next start; a stale blob degrades silently to a
///   fresh transfer.
/// * Requests carry the bearer header (background sessions preserve it)
///   rather than a signed URL, so an expiring signature can never poison
///   persisted resume data.
/// * Progress is throttled (0.5 s / 512 KB / 1 %) and written on a
///   background context so download bytes do not invalidate list queries.
/// * Auto-downloads obey `AppSettings.autoDownloadPolicy` and a concurrency
///   cap; user-initiated downloads bypass both (a background session waits
///   for connectivity on its own).
@MainActor
@Observable
final class DownloadManager {
    static let shared = DownloadManager()

    nonisolated static let backgroundSessionIdentifier = "com.isaackhor.Noadcast.background-downloads"
    static let maxConcurrentAutoDownloads = 3
    /// `srv-` files younger than this are never swept (their completion may
    /// still be on its way to the main actor).
    static let orphanSweepGrace: TimeInterval = 10 * 60

    /// Episodes with a live transfer (drives nothing persistent; handy for
    /// diagnostics and tests).
    private(set) var liveTransferIDs: Set<Int> = []

    private let session: URLSession
    private let delegate: DownloadSessionDelegate

    @ObservationIgnored private var container: ModelContainer?
    @ObservationIgnored private var progressWriter: DownloadProgressWriter?
    @ObservationIgnored private var tasks: [Int: URLSessionDownloadTask] = [:]
    @ObservationIgnored private var startedFromResumeData: Set<Int> = []
    @ObservationIgnored private var freshRetryUsed: Set<Int> = []
    @ObservationIgnored private var cancelledOnPurpose: Set<Int> = []
    @ObservationIgnored private var serverAudioRequested: Set<Int> = []
    @ObservationIgnored private var serverAudioTasks: [Int: Task<Void, Never>] = [:]
    @ObservationIgnored private var serverAudioRequestVersions: [Int: Int] = [:]
    /// Downloading rows from an older app version had no persisted task ID.
    /// Keep them eligible for one completion until reconciled or canceled.
    @ObservationIgnored private var legacyTransferIDs: Set<Int> = []
    @ObservationIgnored private var pendingBackgroundCompletion: (() -> Void)?

    private init() {
        let delegate = DownloadSessionDelegate { event in
            Task { @MainActor in
                DownloadManager.shared.handle(event)
            }
        }
        let configuration = URLSessionConfiguration.background(withIdentifier: Self.backgroundSessionIdentifier)
        configuration.sessionSendsLaunchEvents = true
        configuration.isDiscretionary = false
        // Gated upstream by AutoDownloadPolicy; user-initiated downloads may
        // use cellular.
        configuration.allowsCellularAccess = true
        self.delegate = delegate
        self.session = URLSession(configuration: configuration, delegate: delegate, delegateQueue: nil)
    }

    func configure(container: ModelContainer) {
        self.container = container
        self.progressWriter = DownloadProgressWriter(modelContainer: container)
        let downloadingRaw = DownloadState.downloading.rawValue
        let legacy = (try? container.mainContext.fetch(FetchDescriptor<Episode>(
            predicate: #Predicate<Episode> {
                $0.downloadStateRaw == downloadingRaw && $0.downloadTaskIdentifier == nil
            }
        ))) ?? []
        legacyTransferIDs = Set(legacy.map(\.serverID))
        observeNetwork()
    }

    /// Stores the completion handler iOS hands over when it relaunches the
    /// app for background-session events (`AppDelegate`).
    func storePendingBackgroundCompletion(_ handler: @escaping () -> Void) {
        pendingBackgroundCompletion = handler
    }

    // MARK: - Launch reconciliation

    /// Matches live session tasks against the mirror: running transfers are
    /// adopted (never restarted), rows whose transfer vanished go back to
    /// `queued` (and resume from their blob if there is one), and tasks for
    /// episodes that no longer want them are cancelled.
    func reconcile() async {
        guard let context = container?.mainContext else { return }
        let liveTasks = await session.allTasks
        var liveByServerID: [Int: URLSessionDownloadTask] = [:]
        for task in liveTasks {
            guard let download = task as? URLSessionDownloadTask,
                  let serverID = Int(task.taskDescription ?? "")
            else {
                task.cancel()
                continue
            }
            switch task.state {
            case .running, .suspended:
                liveByServerID[serverID] = download
            case .canceling, .completed:
                continue
            @unknown default:
                continue
            }
        }

        let downloadingRaw = DownloadState.downloading.rawValue
        let queuedRaw = DownloadState.queued.rawValue
        let inFlight = (try? context.fetch(FetchDescriptor<Episode>(
            predicate: #Predicate<Episode> { $0.downloadStateRaw == downloadingRaw || $0.downloadStateRaw == queuedRaw }
        ))) ?? []
        var wanted = Set<Int>()
        for episode in inFlight {
            if episode.isPlayed && !episode.downloadIsUserInitiated {
                episode.setDownloadState(.idle)
                continue
            }
            wanted.insert(episode.serverID)
            if let task = liveByServerID[episode.serverID] {
                tasks[episode.serverID] = task
                legacyTransferIDs.remove(episode.serverID)
                episode.setDownloadState(.downloading)
                episode.downloadTaskIdentifier = task.taskIdentifier
            } else if episode.downloadState == .downloading {
                episode.setDownloadState(.queued)
            }
        }
        for (serverID, task) in liveByServerID where !wanted.contains(serverID) {
            cancelledOnPurpose.insert(serverID)
            task.cancel()
        }
        liveTransferIDs = Set(tasks.keys)
        if context.hasChanges {
            try? context.save()
        }
        sweepOrphanFiles(context: context)
        startEligibleDownloads()
    }

    // MARK: - Requests

    /// Marks the episode as wanted on the device and starts it when allowed.
    /// If the server does not hold the audio yet, it is asked to fetch it
    /// (`POST /process`) and the download starts once a sync shows it.
    func enqueue(_ episode: Episode, userInitiated: Bool) {
        enqueue([episode], userInitiated: userInitiated)
    }

    /// Batch form: one save and one start pass for many episodes.
    func enqueue(_ episodes: [Episode], userInitiated: Bool) {
        guard let context = container?.mainContext else { return }
        var needServerAudio: [Int] = []
        for episode in episodes {
            if episode.isMarkedDownloaded {
                if episode.hasLocalFile { continue }
                // The row points at a file that is gone.
                episode.localFilename = nil
                episode.fileSizeBytes = nil
                episode.localAudioSha256 = nil
            }
            if userInitiated, !episode.downloadIsUserInitiated {
                episode.downloadIsUserInitiated = true
            }
            if episode.downloadState != .downloading {
                episode.setDownloadState(.queued)
            }
            if episode.downloadRequestedAt == nil {
                episode.downloadRequestedAt = Date()
            }
            if episode.downloadError != nil {
                episode.downloadError = nil
            }
            if userInitiated {
                freshRetryUsed.remove(episode.serverID)
            }
            if !episode.audioState.isPresent {
                needServerAudio.append(episode.serverID)
            }
        }
        if context.hasChanges {
            try? context.save()
        }
        for serverID in needServerAudio {
            requestServerAudio(serverID: serverID, force: userInitiated)
        }
        startEligibleDownloads()
    }

    /// User cancel: keeps resume data so a later download continues where
    /// this one stopped. The row is marked failed ("cancelled") rather than
    /// idle so the queue's auto-download does not immediately restart it;
    /// Retry / Download starts it again.
    func cancel(_ episode: Episode) {
        let serverID = episode.serverID
        cancelTransfer(serverID: serverID, discardResumeData: false)
        markFailed(episode, message: "Download cancelled.")
        if episode.downloadIsUserInitiated {
            episode.downloadIsUserInitiated = false
        }
        episode.downloadRequestedAt = nil
        try? container?.mainContext.save()
        startEligibleDownloads()
    }

    /// Stops a live transfer without touching the row.
    func cancelTransfer(serverID: Int, discardResumeData: Bool) {
        legacyTransferIDs.remove(serverID)
        if let task = tasks.removeValue(forKey: serverID) {
            cancelledOnPurpose.insert(serverID)
            if discardResumeData {
                task.cancel()
            } else {
                task.cancel(byProducingResumeData: { data in
                    if let data {
                        AudioStorage.saveResumeData(data, serverID: serverID)
                    }
                })
            }
        }
        if discardResumeData {
            AudioStorage.deleteResumeData(serverID: serverID)
        }
        startedFromResumeData.remove(serverID)
        liveTransferIDs.remove(serverID)
    }

    /// Sign-out / server change. In-flight rows go back to `queued` when
    /// resume data is kept, else to `idle`.
    func cancelAll(keepResumeData: Bool) {
        for serverID in Array(tasks.keys) {
            cancelTransfer(serverID: serverID, discardResumeData: !keepResumeData)
        }
        if !keepResumeData {
            session.getAllTasks { tasks in
                for task in tasks {
                    task.cancel()
                }
            }
        }
        guard let context = container?.mainContext else { return }
        let downloadingRaw = DownloadState.downloading.rawValue
        let rows = (try? context.fetch(FetchDescriptor<Episode>(
            predicate: #Predicate<Episode> { $0.downloadStateRaw == downloadingRaw }
        ))) ?? []
        for episode in rows {
            episode.setDownloadState(keepResumeData ? .queued : .idle)
        }
        if context.hasChanges {
            try? context.save()
        }
    }

    /// The rows are already gone (server deletion): drop any transfer.
    func forget(episodeServerIDs: [Int]) {
        for serverID in episodeServerIDs {
            cancelTransfer(serverID: serverID, discardResumeData: true)
            serverAudioRequested.remove(serverID)
        }
    }

    /// Starts queued downloads the policy allows, oldest request first.
    func startEligibleDownloads() {
        guard let context = container?.mainContext,
              let endpoint = APIConfiguration.currentEndpoint
        else { return }
        let queuedRaw = DownloadState.queued.rawValue
        let descriptor = FetchDescriptor<Episode>(
            predicate: #Predicate<Episode> { $0.downloadStateRaw == queuedRaw },
            sortBy: [SortDescriptor(\Episode.downloadRequestedAt)]
        )
        let queued = (try? context.fetch(descriptor)) ?? []
        guard !queued.isEmpty else { return }

        let policy = AppSettings.current(in: context).autoDownloadPolicy
        let autoAllowed = NetworkMonitor.shared.canAutoDownload(under: policy)
        var autoSlots = max(0, Self.maxConcurrentAutoDownloads - tasks.count)
        for episode in queued {
            if episode.isPlayed && !episode.downloadIsUserInitiated {
                episode.setDownloadState(.idle)
                continue
            }
            guard tasks[episode.serverID] == nil else { continue }
            // Waiting for the server to fetch the audio.
            guard episode.audioState.isPresent else { continue }
            let userInitiated = episode.downloadIsUserInitiated
            if !userInitiated {
                guard autoAllowed, autoSlots > 0 else { continue }
            }
            guard let request = NoadcastAPIClient.audioDownloadRequest(
                episodeID: episode.serverID,
                endpoint: endpoint
            ) else { continue }
            start(episode, request: request)
            if !userInitiated {
                autoSlots -= 1
            }
        }
        if context.hasChanges {
            try? context.save()
        }
    }

    private func start(_ episode: Episode, request: URLRequest) {
        let serverID = episode.serverID
        legacyTransferIDs.remove(serverID)
        let task: URLSessionDownloadTask
        if let resumeData = AudioStorage.loadResumeData(serverID: serverID) {
            task = session.downloadTask(withResumeData: resumeData)
            startedFromResumeData.insert(serverID)
        } else {
            task = session.downloadTask(with: request)
            startedFromResumeData.remove(serverID)
        }
        task.taskDescription = String(serverID)
        if let expected = episode.audioBytes, expected > 0 {
            task.countOfBytesClientExpectsToReceive = expected
        }
        cancelledOnPurpose.remove(serverID)
        task.resume()
        tasks[serverID] = task
        liveTransferIDs.insert(serverID)
        episode.setDownloadState(.downloading)
        episode.downloadTaskIdentifier = task.taskIdentifier
        if episode.downloadError != nil {
            episode.downloadError = nil
        }
    }

    private func requestServerAudio(serverID: Int, force: Bool) {
        guard force || !serverAudioRequested.contains(serverID) else { return }
        serverAudioRequested.insert(serverID)
        serverAudioTasks[serverID]?.cancel()
        let version = (serverAudioRequestVersions[serverID] ?? 0) + 1
        serverAudioRequestVersions[serverID] = version
        serverAudioTasks[serverID] = Task {
            defer {
                if serverAudioRequestVersions[serverID] == version {
                    serverAudioTasks.removeValue(forKey: serverID)
                }
            }
            guard !Task.isCancelled,
                  serverAudioRequestVersions[serverID] == version,
                  let context = container?.mainContext,
                  let episode = fetchEpisode(serverID: serverID, in: context),
                  !episode.isPlayed
            else { return }
            do {
                try await NoadcastAPIClient.shared.process(episodeID: serverID)
                if !Task.isCancelled {
                    SyncService.shared.scheduleFollowUpSyncs()
                }
            } catch {
                if !Task.isCancelled {
                    Log.download.notice("process request for \(serverID) failed: \(error.localizedDescription, privacy: .public)")
                }
            }
        }
    }

    /// Prevents a queued `/process` request from starting after the episode
    /// was dismissed; an already submitted server job is canceled by the
    /// retention release request.
    func cancelServerAudioRequest(serverID: Int) {
        serverAudioRequestVersions[serverID] = (serverAudioRequestVersions[serverID] ?? 0) + 1
        serverAudioTasks.removeValue(forKey: serverID)?.cancel()
        serverAudioRequested.remove(serverID)
    }

    // MARK: - Session events

    func handle(_ event: DownloadSessionEvent) {
        switch event {
        case .progress(let serverID, let taskID, let written, let expected):
            guard tasks[serverID]?.taskIdentifier == taskID else { return }
            guard let writer = progressWriter else { return }
            Task {
                await writer.record(serverID: serverID, taskID: taskID, written: written, expected: expected)
            }
        case .finished(let serverID, let taskID, let filename, let size, let sha256):
            handleFinished(serverID: serverID, taskID: taskID, filename: filename, size: size, sha256: sha256)
        case .httpFailure(let serverID, let taskID, let status, let errorCode, let message):
            handleHTTPFailure(serverID: serverID, taskID: taskID, status: status, errorCode: errorCode, message: message)
        case .failed(let serverID, let taskID, let errorCode, let message, let resumeData, let cancelled):
            handleTransportFailure(
                serverID: serverID,
                taskID: taskID,
                errorCode: errorCode,
                message: message,
                resumeData: resumeData,
                cancelled: cancelled
            )
        case .completedCleanly(let serverID, let taskID):
            guard tasks[serverID]?.taskIdentifier == taskID else { return }
            tasks.removeValue(forKey: serverID)
            liveTransferIDs.remove(serverID)
            startedFromResumeData.remove(serverID)
            cancelledOnPurpose.remove(serverID)
            startEligibleDownloads()
        case .backgroundEventsFinished:
            let handler = pendingBackgroundCompletion
            pendingBackgroundCompletion = nil
            handler?()
        }
    }

    private func matchesActiveTransfer(serverID: Int, taskID: Int, episode: Episode) -> Bool {
        if let task = tasks[serverID] {
            return task.taskIdentifier == taskID
        }
        if let storedID = episode.downloadTaskIdentifier {
            return storedID == taskID
        }
        guard Self.acceptsLegacyCompletion(
            wasDownloadingAtStartup: legacyTransferIDs.contains(serverID),
            isPlayed: episode.isPlayed,
            state: episode.downloadState
        ) else { return false }
        legacyTransferIDs.remove(serverID)
        return true
    }

    static func acceptsLegacyCompletion(
        wasDownloadingAtStartup: Bool,
        isPlayed: Bool,
        state: DownloadState
    ) -> Bool {
        wasDownloadingAtStartup && !isPlayed && (state == .downloading || state == .queued)
    }

    private func handleFinished(serverID: Int, taskID: Int, filename: String, size: Int64, sha256: String?) {
        guard let context = container?.mainContext,
              let episode = fetchEpisode(serverID: serverID, in: context),
              matchesActiveTransfer(serverID: serverID, taskID: taskID, episode: episode)
        else {
            AudioStorage.deleteFile(named: filename)
            return
        }
        tasks.removeValue(forKey: serverID)
        liveTransferIDs.remove(serverID)
        startedFromResumeData.remove(serverID)
        freshRetryUsed.remove(serverID)
        AudioStorage.deleteResumeData(serverID: serverID)
        if episode.isPlayed || episode.downloadState == .idle {
            // Cancelled by the user after the bytes were already in.
            AudioStorage.deleteFile(named: filename)
            return
        }
        if let old = episode.localFilename, old != filename {
            AudioStorage.deleteFile(named: old)
        }
        episode.localFilename = filename
        episode.fileSizeBytes = size
        episode.localAudioSha256 = sha256 ?? episode.audioSha256
        episode.setDownloadState(.downloaded)
        if episode.downloadProgress != 1 {
            episode.downloadProgress = 1
        }
        if episode.downloadedBytes != size {
            episode.downloadedBytes = size
        }
        if episode.downloadTotalBytes != size {
            episode.downloadTotalBytes = size
        }
        episode.downloadError = nil
        episode.downloadIsUserInitiated = false
        episode.downloadRequestedAt = nil
        try? context.save()
        Log.download.info("Downloaded episode \(serverID) (\(size) bytes)")
        PlayerService.shared.localFileBecameAvailable(episodeServerID: serverID)
        startEligibleDownloads()
    }

    private func handleHTTPFailure(serverID: Int, taskID: Int, status: Int, errorCode: String?, message: String?) {
        guard let context = container?.mainContext,
              let episode = fetchEpisode(serverID: serverID, in: context),
              matchesActiveTransfer(serverID: serverID, taskID: taskID, episode: episode)
        else { return }
        let wasResumed = startedFromResumeData.contains(serverID)
        tasks.removeValue(forKey: serverID)
        liveTransferIDs.remove(serverID)
        startedFromResumeData.remove(serverID)
        if episode.isPlayed || episode.downloadState == .idle {
            AudioStorage.deleteResumeData(serverID: serverID)
            return
        }
        switch status {
        case 409:
            // The server lacks the audio (not ready / evicted) and has queued
            // a priority fetch. Stay queued; a sync starts us again.
            AudioStorage.deleteResumeData(serverID: serverID)
            episode.setDownloadState(.queued)
            requestServerAudio(serverID: serverID, force: true)
        case 401:
            SyncService.shared.setAuthFailed(true)
            markFailed(episode, message: "The server rejected the access token.")
        case 404:
            AudioStorage.deleteResumeData(serverID: serverID)
            markFailed(episode, message: "This episode is no longer on the server.")
        default:
            if wasResumed {
                // Most likely stale resume data (416, validator mismatch).
                AudioStorage.deleteResumeData(serverID: serverID)
                restartFresh(episode)
            } else {
                markFailed(episode, message: message ?? "Server returned HTTP \(status).")
            }
        }
        try? context.save()
        Log.download.notice("Download of \(serverID) got HTTP \(status) (\(errorCode ?? "-", privacy: .public))")
        startEligibleDownloads()
    }

    private func handleTransportFailure(
        serverID: Int,
        taskID: Int,
        errorCode: Int,
        message: String,
        resumeData: Data?,
        cancelled: Bool
    ) {
        let currentTaskID = tasks[serverID]?.taskIdentifier
        let context = container?.mainContext
        let episode = context.flatMap { fetchEpisode(serverID: serverID, in: $0) }
        if cancelled, cancelledOnPurpose.remove(serverID) != nil, currentTaskID != taskID {
            // A canceled old task may finish after a replacement has started.
            // Only keep its resume blob if no replacement is active.
            if currentTaskID == nil, let episode, !episode.isPlayed, episode.downloadState != .idle {
                if let resumeData { AudioStorage.saveResumeData(resumeData, serverID: serverID) }
            } else if currentTaskID == nil {
                AudioStorage.deleteResumeData(serverID: serverID)
            }
            return
        }
        guard let episode, matchesActiveTransfer(serverID: serverID, taskID: taskID, episode: episode) else { return }
        let wasResumed = startedFromResumeData.contains(serverID)
        tasks.removeValue(forKey: serverID)
        liveTransferIDs.remove(serverID)
        startedFromResumeData.remove(serverID)
        if episode.isPlayed || episode.downloadState == .idle {
            AudioStorage.deleteResumeData(serverID: serverID)
            return
        }
        if let resumeData {
            AudioStorage.saveResumeData(resumeData, serverID: serverID)
        }
        let isNetworkError = APIError.retryableTransportCodes.contains(errorCode)
        if wasResumed, resumeData == nil, !isNetworkError, !cancelled {
            // The blob could not be resumed: degrade silently to a fresh
            // transfer, once.
            AudioStorage.deleteResumeData(serverID: serverID)
            restartFresh(episode)
        } else if isNetworkError || cancelled {
            // Offline, or the system cancelled us (force quit): retry when
            // the network / next launch allows.
            episode.setDownloadState(.queued)
            if episode.downloadError != nil {
                episode.downloadError = nil
            }
        } else {
            markFailed(episode, message: message)
        }
        if let context { try? context.save() }
        Log.download.notice("Download of \(serverID) failed (\(errorCode)): \(message, privacy: .public)")
    }

    private func restartFresh(_ episode: Episode) {
        let serverID = episode.serverID
        guard !freshRetryUsed.contains(serverID) else {
            markFailed(episode, message: "The download could not be resumed.")
            return
        }
        freshRetryUsed.insert(serverID)
        episode.setDownloadState(.queued)
        // Deferred so the current event finishes updating the row first.
        Task { @MainActor in
            DownloadManager.shared.startEligibleDownloads()
        }
    }

    private func markFailed(_ episode: Episode, message: String) {
        episode.setDownloadState(.failed)
        if episode.downloadError != message {
            episode.downloadError = message
        }
    }

    private func fetchEpisode(serverID: Int, in context: ModelContext) -> Episode? {
        let descriptor = FetchDescriptor<Episode>(predicate: #Predicate<Episode> { $0.serverID == serverID })
        return try? context.fetch(descriptor).first
    }

    // MARK: - Housekeeping

    /// Deletes `srv-` files that no episode references (a completion that
    /// was lost, or a row deleted mid-download). Legacy files are left to
    /// `DeviceStateRestoreService`, and anything a pending restore wants to
    /// adopt is protected.
    private func sweepOrphanFiles(context: ModelContext) {
        let rows = (try? context.fetch(FetchDescriptor<Episode>(
            predicate: #Predicate<Episode> { $0.localFilename != nil }
        ))) ?? []
        let referenced = Set(rows.compactMap(\.localFilename))
        let protected = DeviceStateRestoreService.shared.protectedFilenames()
        let cutoff = Date().addingTimeInterval(-Self.orphanSweepGrace)
        for name in AudioStorage.listEpisodeFiles() where AudioStorage.isServerFilename(name) {
            guard !referenced.contains(name), !protected.contains(name) else { continue }
            let url = AudioStorage.fileURL(for: name)
            let modified = (try? url.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate ?? .distantPast
            guard modified < cutoff else { continue }
            AudioStorage.deleteFile(named: name)
            Log.download.notice("Removed orphaned download \(name, privacy: .public)")
        }
    }

    /// Re-evaluates the auto-download policy whenever reachability or Wi-Fi
    /// changes (`NetworkMonitor` is `@Observable`).
    private func observeNetwork() {
        withObservationTracking {
            _ = NetworkMonitor.shared.isOnline
            _ = NetworkMonitor.shared.isWiFi
        } onChange: {
            Task { @MainActor in
                DownloadManager.shared.networkDidChange()
            }
        }
    }

    private func networkDidChange() {
        startEligibleDownloads()
        observeNetwork()
    }
}

/// Writes throttled download progress off the main actor, so byte counts
/// do not dirty the main context's list queries. A fresh context per write:
/// a long-lived one would read the row's download state as it was when first
/// fetched, not as the main context has since set it.
@ModelActor
actor DownloadProgressWriter {
    func record(serverID: Int, taskID: Int, written: Int64, expected: Int64?) {
        let context = ModelContext(modelContainer)
        context.autosaveEnabled = false
        let descriptor = FetchDescriptor<Episode>(predicate: #Predicate<Episode> { $0.serverID == serverID })
        guard let episode = try? context.fetch(descriptor).first else { return }
        // A late progress event must not regress a finished row.
        guard episode.downloadStateRaw == DownloadState.downloading.rawValue,
              episode.downloadTaskIdentifier == taskID else { return }
        var fraction = 0.0
        if let expected, expected > 0 {
            fraction = min(1, max(0, Double(written) / Double(expected)))
        }
        var changed = false
        if episode.downloadedBytes != written {
            episode.downloadedBytes = written
            changed = true
        }
        if episode.downloadTotalBytes != expected {
            episode.downloadTotalBytes = expected
            changed = true
        }
        if abs(episode.downloadProgress - fraction) > 0.000_1 {
            episode.downloadProgress = fraction
            changed = true
        }
        if changed {
            try? context.save()
        }
    }
}

/// `URLSessionDownloadDelegate` for the background session. Runs on the
/// session's delegate queue; touches no model objects. The temp file at
/// `location` is deleted when `didFinishDownloadingTo` returns, so the move
/// happens synchronously here.
nonisolated final class DownloadSessionDelegate: NSObject, URLSessionDownloadDelegate, @unchecked Sendable {
    private static let progressThrottleInterval: TimeInterval = 0.5
    private static let progressThrottleBytes: Int64 = 512 * 1024
    private static let progressThrottleFraction: Double = 0.01

    private let onEvent: @Sendable (DownloadSessionEvent) -> Void
    private let lock = NSLock()
    /// Keyed by task identifier.
    private var lastUptime: [Int: TimeInterval] = [:]
    private var lastBytes: [Int: Int64] = [:]
    private var lastTotal: [Int: Int64] = [:]

    init(onEvent: @escaping @Sendable (DownloadSessionEvent) -> Void) {
        self.onEvent = onEvent
        super.init()
    }

    nonisolated func urlSession(
        _ session: URLSession,
        downloadTask: URLSessionDownloadTask,
        didWriteData bytesWritten: Int64,
        totalBytesWritten: Int64,
        totalBytesExpectedToWrite: Int64
    ) {
        guard let serverID = Int(downloadTask.taskDescription ?? "") else { return }
        let total: Int64? = totalBytesExpectedToWrite > 0 ? totalBytesExpectedToWrite : nil
        guard shouldReportProgress(taskID: downloadTask.taskIdentifier, written: totalBytesWritten, total: total) else {
            return
        }
        onEvent(.progress(serverID: serverID, taskID: downloadTask.taskIdentifier, written: totalBytesWritten, expected: total))
    }

    nonisolated func urlSession(
        _ session: URLSession,
        downloadTask: URLSessionDownloadTask,
        didFinishDownloadingTo location: URL
    ) {
        guard let serverID = Int(downloadTask.taskDescription ?? "") else { return }
        let response = downloadTask.response as? HTTPURLResponse
        let status = response?.statusCode ?? 200
        guard (200..<300).contains(status) else {
            var code: String?
            var message: String?
            if let data = try? Data(contentsOf: location), data.count <= 65_536,
               let envelope = try? JSONDecoder().decode(APIErrorEnvelope.self, from: data) {
                code = envelope.error?.code
                message = envelope.error?.message
            }
            onEvent(.httpFailure(serverID: serverID, taskID: downloadTask.taskIdentifier, status: status, errorCode: code, message: message))
            return
        }
        let mimeType = response?.mimeType ?? response?.value(forHTTPHeaderField: "Content-Type")
        let filename = AudioStorage.makeFilename(
            serverID: serverID,
            fileExtension: AudioStorage.fileExtension(forMimeType: mimeType)
        )
        let destination = AudioStorage.fileURL(for: filename)
        do {
            try FileManager.default.moveItem(at: location, to: destination)
        } catch {
            onEvent(.failed(
                serverID: serverID,
                taskID: downloadTask.taskIdentifier,
                errorCode: -1,
                message: "Couldn't move the downloaded file: \(error.localizedDescription)",
                resumeData: nil,
                cancelled: false
            ))
            return
        }
        let size = AudioStorage.fileSize(named: filename) ?? 0
        let sha256 = response?.value(forHTTPHeaderField: "ETag").flatMap(Self.sha256(fromETag:))
        onEvent(.finished(serverID: serverID, taskID: downloadTask.taskIdentifier, filename: filename, size: size, sha256: sha256))
    }

    nonisolated func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didCompleteWithError error: Error?
    ) {
        forgetProgress(taskID: task.taskIdentifier)
        guard let serverID = Int(task.taskDescription ?? "") else { return }
        if let error {
            let ns = error as NSError
            let resumeData = ns.userInfo[NSURLSessionDownloadTaskResumeData] as? Data
            let cancelled = ns.domain == NSURLErrorDomain && ns.code == NSURLErrorCancelled
            onEvent(.failed(
                serverID: serverID,
                taskID: task.taskIdentifier,
                errorCode: ns.code,
                message: error.localizedDescription,
                resumeData: resumeData,
                cancelled: cancelled
            ))
        } else {
            onEvent(.completedCleanly(serverID: serverID, taskID: task.taskIdentifier))
        }
    }

    nonisolated func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        onEvent(.backgroundEventsFinished)
    }

    /// The server's audio `ETag` is the SHA-256 of the bytes, possibly weak
    /// (`W/`) and quoted.
    nonisolated static func sha256(fromETag etag: String) -> String? {
        var value = etag.trimmingCharacters(in: .whitespaces)
        if value.hasPrefix("W/") {
            value.removeFirst(2)
        }
        value = value.trimmingCharacters(in: CharacterSet(charactersIn: "\""))
        return value.isEmpty ? nil : value.lowercased()
    }

    // MARK: - Throttle (0.5 s / 512 KB / 1 %)

    private func shouldReportProgress(taskID: Int, written: Int64, total: Int64?) -> Bool {
        let now = ProcessInfo.processInfo.systemUptime
        lock.lock()
        defer { lock.unlock() }
        guard let previousUptime = lastUptime[taskID] else {
            record(taskID: taskID, uptime: now, written: written, total: total)
            return true
        }
        let previousBytes = lastBytes[taskID] ?? 0
        let isComplete = total.map { written >= $0 } ?? false
        let totalBecameKnown = lastTotal[taskID] == nil && total != nil
        let elapsed = now - previousUptime
        let byteDelta = written - previousBytes
        var fractionDelta = 0.0
        if let total, total > 0 {
            fractionDelta = Double(byteDelta) / Double(total)
        }
        let report = isComplete
            || totalBecameKnown
            || (elapsed >= Self.progressThrottleInterval
                && (byteDelta >= Self.progressThrottleBytes || fractionDelta >= Self.progressThrottleFraction))
        if report {
            record(taskID: taskID, uptime: now, written: written, total: total)
        }
        return report
    }

    /// Caller holds `lock`.
    private func record(taskID: Int, uptime: TimeInterval, written: Int64, total: Int64?) {
        lastUptime[taskID] = uptime
        lastBytes[taskID] = written
        if let total {
            lastTotal[taskID] = total
        }
    }

    private func forgetProgress(taskID: Int) {
        lock.lock()
        lastUptime.removeValue(forKey: taskID)
        lastBytes.removeValue(forKey: taskID)
        lastTotal.removeValue(forKey: taskID)
        lock.unlock()
    }
}
