import Foundation
import AVFoundation
import MediaPlayer
import Observation
import SwiftData
import UIKit
import os

/// Where the loaded item's audio comes from.
nonisolated enum PlaybackSourceKind: Equatable, Sendable {
    case none
    case local
    case stream
}

/// What the player is doing beyond play/pause, for the Now Playing UI.
nonisolated enum PlayerStatus: Equatable, Sendable {
    case idle
    /// Metadata loaded (the item may be built lazily on first play/seek).
    case ready
    /// Minting a stream URL, or waiting for the server to fetch the audio.
    case preparing(String)
    /// AVPlayer is waiting for data.
    case buffering
    /// Cannot play right now. Retry re-resolves the source; Download queues
    /// the episode for the device.
    case unavailable(message: String, canDownload: Bool)
}

@MainActor
@Observable
final class PlayerService {
    static let shared = PlayerService()

    /// Buffer ahead, in content seconds at 1×. Scaled by the playback rate:
    /// the speed ladder goes to 4.2×, which drains a buffer 4.2× faster.
    nonisolated static let baseForwardBufferSeconds: TimeInterval = 30
    /// Network-drop / signed-URL-expiry recovery: 3 attempts, 1 / 2 / 4 s.
    static let recoveryDelays: [Double] = [1, 2, 4]
    /// A stream stalled this long while online is rebuilt.
    static let stallRecoverySeconds: TimeInterval = 20
    /// Give up waiting for the server to fetch audio after this long.
    static let prepareTimeoutSeconds: TimeInterval = 15 * 60

    private(set) var currentEpisodeID: PersistentIdentifier?
    private(set) var currentEpisodeServerID: Int?
    private(set) var currentPodcastServerID: Int?
    private(set) var currentEpisodeTitle: String = ""
    private(set) var currentPodcastTitle: String = ""
    private(set) var artworkURL: URL?
    private(set) var currentTime: Double = 0
    private(set) var duration: Double = 0
    private(set) var isPlaying: Bool = false
    private(set) var playbackRate: Double = 1.0
    private(set) var skippedAds: Int = 0
    private(set) var adRegions: [AdRegion] = []
    private(set) var chapters: [EpisodeChapter] = []
    private(set) var isLoadingChapters = false
    private(set) var playAdsForCurrentEpisode: Bool = false
    private(set) var status: PlayerStatus = .idle
    private(set) var sourceKind: PlaybackSourceKind = .none
    /// Loaded time ranges of a stream, throttled to 1 Hz.
    private(set) var bufferedRanges: [ClosedRange<Double>] = []
    /// The local file is a different render than the one the server's
    /// markers describe, so skipping is off for it.
    private(set) var markersSuppressedForLocalFile = false

    private let player: AVPlayer = AVPlayer()
    @ObservationIgnored private var timeObserverToken: Any?
    @ObservationIgnored private var endObserver: NSObjectProtocol?
    @ObservationIgnored private var failureObserver: NSObjectProtocol?

    @ObservationIgnored private weak var modelContainer: ModelContainer?
    /// Last time `persistPosition(force:)` actually wrote, in
    /// `ProcessInfo.systemUptime` seconds. Used to throttle saves.
    @ObservationIgnored private var lastPersistTime: TimeInterval = 0
    /// Lifetime stats are accumulated in memory and flushed occasionally.
    /// Writing `AppSettings` on every 0.25s playback tick invalidates every
    /// live `@Query<AppSettings>` and makes list scrolling stutter.
    @ObservationIgnored private var pendingLifetimePlayedSeconds: Double = 0
    @ObservationIgnored private var pendingLifetimeAdSkipSeconds: Double = 0
    @ObservationIgnored private var lastLifetimeStatsFlushTime: TimeInterval = 0
    @ObservationIgnored private var willResignObserver: NSObjectProtocol?

    /// Cached artwork keyed by URL so we don't re-fetch every load.
    @ObservationIgnored private var artworkCache: [URL: UIImage] = [:]
    /// In-flight artwork fetch — cancelled when the player loads a new episode.
    @ObservationIgnored private var artworkFetchTask: Task<Void, Never>?

    /// Last playback position seen by the periodic time observer. Used to
    /// compute audio-time deltas for the speedup savings counter, and to
    /// ignore deltas that look like seeks rather than natural progress.
    @ObservationIgnored private var lastObservedTime: Double = 0

    /// Skip-policy snapshot captured at load time. The ad toggle can also be
    /// updated live from Settings / Now Playing.
    @ObservationIgnored private var skipAdsEnabled: Bool = true
    @ObservationIgnored private var skipIntrosAndOutrosEnabled: Bool = true
    /// Gap in seconds: when the player skips a segment it then peeks ahead
    /// for another segment whose start is within this window and chains
    /// through it too.
    @ObservationIgnored private var chainSkipGapSeconds: Double = 5
    @ObservationIgnored private var analysisEnabled: Bool = true
    @ObservationIgnored private var skipPlanner = AdSkipPlanner()

    // Two-source plumbing.
    @ObservationIgnored private var loadGeneration = 0
    /// Metadata is loaded but no `AVPlayerItem` exists yet (lazy restore, or
    /// after an unavailable/failed attempt). Built on the next play/seek.
    @ObservationIgnored private var needsItemBuild = false
    @ObservationIgnored private var wantsToPlay = false
    /// A download finished while streaming; switch at the next pause/seek.
    @ObservationIgnored private var pendingLocalSwap = false
    @ObservationIgnored private var recoveryAttempts = 0
    @ObservationIgnored private var isRecovering = false
    @ObservationIgnored private var stallStartedAt: TimeInterval?
    @ObservationIgnored private var smoothPlaybackTicks = 0
    @ObservationIgnored private var prepareTask: Task<Void, Never>?
    @ObservationIgnored private var monitorTask: Task<Void, Never>?
    @ObservationIgnored private var chapterTask: Task<Void, Never>?

    init() {
        Log.signposter.withIntervalSignpost("PlayerService.init") {
            Log.signposter.withIntervalSignpost("AVAudioSession.setup") {
                configureAudioSession()
            }
            Log.signposter.withIntervalSignpost("RemoteCommands.setup") {
                configureRemoteCommands()
            }
            player.allowsExternalPlayback = false
            // Flush the current playback position whenever the app moves to
            // background — catches clean exits and graceful suspensions.
            // During a sudden crash, the in-flight 3-second throttled save
            // is the safety net (worst case: lose up to 3 s of progress).
            willResignObserver = NotificationCenter.default.addObserver(
                forName: UIApplication.willResignActiveNotification,
                object: nil,
                queue: .main
            ) { _ in
                Task { @MainActor [weak self] in
                    self?.flushLifetimeStats(force: true)
                    self?.persistPosition(force: true)
                }
            }
        }
    }

    func setModelContainer(_ container: ModelContainer) {
        self.modelContainer = container
    }

    // MARK: - Status helpers (UI)

    var isPreparing: Bool {
        if case .preparing = status { return true }
        return false
    }

    var isBuffering: Bool {
        status == .buffering
    }

    var isUnavailable: Bool {
        if case .unavailable = status { return true }
        return false
    }

    /// Preparing or buffering: show a spinner instead of the play glyph.
    var isWaitingForAudio: Bool {
        isPreparing || isBuffering
    }

    var statusMessage: String? {
        switch status {
        case .idle, .ready: nil
        case .preparing(let message): message
        case .buffering: "Buffering…"
        case .unavailable(let message, _): message
        }
    }

    var offersDownload: Bool {
        if case .unavailable(_, let canDownload) = status { return canDownload }
        return false
    }

    // MARK: - Public controls

    /// Loads an episode and, if `autoPlay`, starts it. Async because a
    /// remote source needs a freshly minted signed URL (or the server to
    /// fetch the audio first).
    func load(episode: Episode, settings: AppSettings, autoPlay: Bool = false) async {
        let loadState = Log.signposter.beginInterval("PlayerService.load")
        defer { Log.signposter.endInterval("PlayerService.load", loadState) }
        if currentEpisodeID == episode.persistentModelID, player.currentItem != nil {
            // Already loaded (e.g. its row's play button): don't restart it.
            if autoPlay {
                play()
            }
            return
        }
        prepareMetadata(for: episode, settings: settings)
        await buildItem(autoPlay: autoPlay, generation: loadGeneration)
    }

    /// Stops playback and clears all current-episode UI state if the given
    /// episode is the one currently loaded. Used when the user deletes the
    /// playing episode from Queue or Downloads.
    func unloadIfCurrent(episodeID: PersistentIdentifier) {
        guard currentEpisodeID == episodeID else { return }
        unload()
    }

    func unloadIfCurrent(episodeServerID: Int) {
        guard currentEpisodeServerID == episodeServerID else { return }
        unload()
    }

    /// Unconditional unload (server deletion, instance change, local reset).
    func unload() {
        guard currentEpisodeID != nil || currentEpisodeServerID != nil else { return }
        flushLifetimeStats(force: true)
        teardownObservers()
        prepareTask?.cancel()
        prepareTask = nil
        chapterTask?.cancel()
        chapterTask = nil
        loadGeneration += 1
        player.pause()
        player.replaceCurrentItem(with: nil)
        isPlaying = false
        wantsToPlay = false
        needsItemBuild = false
        pendingLocalSwap = false
        isRecovering = false
        recoveryAttempts = 0
        currentEpisodeID = nil
        currentEpisodeServerID = nil
        currentPodcastServerID = nil
        currentEpisodeTitle = ""
        currentPodcastTitle = ""
        artworkURL = nil
        currentTime = 0
        duration = 0
        adRegions = []
        chapters = []
        isLoadingChapters = false
        playAdsForCurrentEpisode = false
        skippedAds = 0
        status = .idle
        sourceKind = .none
        bufferedRanges = []
        markersSuppressedForLocalFile = false
        skipPlanner.reset()
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil

        if let container = modelContainer {
            let settings = AppSettings.current(in: container.mainContext)
            if settings.lastPlayedEpisodeServerID != nil {
                settings.lastPlayedEpisodeServerID = nil
            }
            try? container.mainContext.save()
        }
    }

    /// Loads the last-played episode (if any) without auto-playing. Call at
    /// app launch from the root view. Metadata only: building a remote item
    /// here would start buffering on every cold launch, so the item is built
    /// on the first play/seek.
    func restoreLastPlayedEpisode(context: ModelContext) {
        let state = Log.signposter.beginInterval("PlayerService.restoreLastPlayedEpisode")
        defer { Log.signposter.endInterval("PlayerService.restoreLastPlayedEpisode", state) }
        guard currentEpisodeID == nil else { return }  // already loaded
        let settings = AppSettings.current(in: context)
        guard let serverID = settings.lastPlayedEpisodeServerID else { return }
        let descriptor = FetchDescriptor<Episode>(predicate: #Predicate { $0.serverID == serverID })
        guard let episode = try? context.fetch(descriptor).first else { return }
        prepareMetadata(for: episode, settings: settings)
    }

    func play() {
        activateAudioSessionIfNeeded()
        wantsToPlay = true
        guard player.currentItem != nil else {
            // No item yet (lazy restore) or the last attempt failed: build
            // it; it starts playing once ready.
            if needsItemBuild, !isPreparing {
                let generation = loadGeneration
                if isUnavailable {
                    status = .ready
                }
                Task { await self.buildItem(autoPlay: true, generation: generation) }
            }
            updateNowPlayingInfo()
            return
        }
        player.rate = Float(playbackRate)
        isPlaying = true
        updateNowPlayingInfo()
    }

    func pause() {
        player.pause()
        isPlaying = false
        wantsToPlay = false
        updateNowPlayingInfo()
        flushLifetimeStats(force: true)
        persistPosition(force: true)
        if pendingLocalSwap {
            swapToLocalFile()
        }
    }

    func togglePlayPause() {
        (isPlaying || wantsToPlay) ? pause() : play()
    }

    /// Retry after an unavailable / failed state (Now Playing's Retry).
    func retry() {
        recoveryAttempts = 0
        play()
    }

    /// Queue the current episode for download to the device (Now Playing's
    /// Download action when streaming is not possible).
    func downloadCurrentEpisode() {
        guard let episode = currentEpisodeModel(), let context = modelContainer?.mainContext else { return }
        SubscriptionService.shared.download(episode, in: context)
    }

    func seek(to seconds: Double) {
        let clamped = Self.clampedPlaybackTime(seconds, duration: duration)
        // A user seek may deliberately land inside a segment: let it skip
        // again from scratch.
        skipPlanner.reset()
        currentTime = clamped
        // Suppress the next time-observer delta so this seek doesn't count
        // toward the speedup-savings tally.
        lastObservedTime = clamped
        if player.currentItem != nil {
            player.seek(to: CMTime(seconds: clamped, preferredTimescale: 600))
        } else if needsItemBuild, !isPreparing, !isUnavailable {
            // Lazy restore: build the item at the new position (paused).
            let generation = loadGeneration
            Task { await self.buildItem(autoPlay: false, generation: generation) }
        }
        updateNowPlayingInfo()
        if pendingLocalSwap {
            swapToLocalFile()
        }
    }

    func skipForward(_ seconds: Double = 30) { seek(to: currentTime + seconds) }
    func skipBackward(_ seconds: Double = 15) { seek(to: currentTime - seconds) }

    func setPlaybackRate(_ rate: Double) {
        playbackRate = rate
        if isPlaying { player.rate = Float(rate) }
        if sourceKind == .stream {
            player.currentItem?.preferredForwardBufferDuration = Self.forwardBufferDuration(forRate: rate)
        }
        updateNowPlayingInfo()
    }

    func setSkipAdsEnabled(_ enabled: Bool) {
        skipAdsEnabled = enabled
        if !enabled {
            playAdsForCurrentEpisode = false
        }
    }

    func setPlayAdsForCurrentEpisode(_ enabled: Bool) {
        playAdsForCurrentEpisode = enabled && skipAdsEnabled
    }

    func discardPendingPlaybackHistory() {
        pendingLifetimePlayedSeconds = 0
        pendingLifetimeAdSkipSeconds = 0
        lastLifetimeStatsFlushTime = ProcessInfo.processInfo.systemUptime
    }

    nonisolated static func forwardBufferDuration(forRate rate: Double) -> TimeInterval {
        guard rate.isFinite, rate > 1 else { return baseForwardBufferSeconds }
        return baseForwardBufferSeconds * rate
    }

    // MARK: - Sync / download notifications

    /// Markers can arrive mid-playback (streaming a just-published episode
    /// while the server is still classifying). Re-snapshot and re-sanitise;
    /// skipping only ever seeks forward, so a region already passed is never
    /// revisited.
    func adMarkersDidChange(episodeServerID: Int) {
        guard episodeServerID == currentEpisodeServerID, let episode = currentEpisodeModel() else { return }
        let refreshed = regions(for: episode, kind: sourceKind)
        if refreshed != adRegions {
            adRegions = refreshed
            skipPlanner.reset()
            updateNowPlayingInfo()
        }
    }

    /// A sync saw the server's audio become present for the current episode.
    func serverAudioBecameAvailable(episodeServerID: Int) {
        guard episodeServerID == currentEpisodeServerID, isPreparing, sourceKind == .none else { return }
        prepareTask?.cancel()
        let generation = loadGeneration
        Task { await self.buildItem(autoPlay: self.wantsToPlay, generation: generation) }
    }

    /// The device download of the current episode finished: switch to the
    /// local file immediately if paused or stalled, otherwise at the next
    /// pause / seek (or not at all if the episode finishes first).
    func localFileBecameAvailable(episodeServerID: Int) {
        guard episodeServerID == currentEpisodeServerID else { return }
        switch sourceKind {
        case .local:
            return
        case .none:
            if isPreparing || isUnavailable {
                prepareTask?.cancel()
                let generation = loadGeneration
                Task { await self.buildItem(autoPlay: self.wantsToPlay, generation: generation) }
            }
        case .stream:
            let stalled = player.timeControlStatus == .waitingToPlayAtSpecifiedRate
            if !isPlaying || stalled {
                swapToLocalFile()
            } else {
                pendingLocalSwap = true
            }
        }
    }

    // MARK: - Source resolution and item construction

    private func currentEpisodeModel() -> Episode? {
        guard let id = currentEpisodeID, let container = modelContainer else { return nil }
        return container.mainContext.model(for: id) as? Episode
    }

    /// Local file wins → stream (configured + online + policy + server has
    /// the audio) → unavailable. See `PlaybackSourceResolver`.
    func resolveSource(for episode: Episode) -> PlaybackSource {
        let localURL = episode.hasLocalFile ? episode.localFileURL : nil
        let network = NetworkMonitor.shared
        return PlaybackSourceResolver.resolve(PlaybackSourceInputs(
            localFileURL: localURL,
            isServerConfigured: APIConfiguration.isConfigured,
            isOnline: network.isOnline,
            isWiFi: network.isWiFi,
            audioState: episode.audioState,
            streamingPolicy: StreamingPolicy.current
        ))
    }

    /// Title, artwork, markers, resume point and speed — no AVFoundation.
    private func prepareMetadata(for episode: Episode, settings: AppSettings) {
        // Reloading the loaded episode (lazy item, retry) keeps the live
        // position: the row's persisted value may not have merged back yet.
        let sameEpisode = currentEpisodeID == episode.persistentModelID
        let livePosition = currentTime
        flushLifetimeStats(force: true)
        persistPosition(force: true)
        teardownObservers()
        prepareTask?.cancel()
        prepareTask = nil
        chapterTask?.cancel()
        chapterTask = nil
        loadGeneration += 1
        player.pause()
        player.replaceCurrentItem(with: nil)
        isPlaying = false
        wantsToPlay = false
        pendingLocalSwap = false
        recoveryAttempts = 0
        isRecovering = false
        stallStartedAt = nil
        smoothPlaybackTicks = 0
        bufferedRanges = []
        chapters = []
        isLoadingChapters = false
        sourceKind = .none
        status = .ready
        markersSuppressedForLocalFile = false
        skipPlanner.reset()

        let podcast = episode.podcast
        let displayArtworkURL = episode.podcastArtworkDisplayURL ?? podcast?.artworkDisplayURL
        currentEpisodeID = episode.persistentModelID
        currentEpisodeServerID = episode.serverID
        currentPodcastServerID = episode.podcastServerID
        currentEpisodeTitle = episode.title
        currentPodcastTitle = episode.podcastTitle ?? podcast?.title ?? ""
        artworkURL = displayArtworkURL
        duration = Self.positiveFiniteDuration(episode.duration) ?? 0
        analysisEnabled = settings.adAnalysisEnabled && (podcast?.adAnalysisEnabled ?? true)
        adRegions = regions(for: episode, kind: episode.hasLocalFile ? .local : .stream)
        skipAdsEnabled = settings.skipAds
        skipIntrosAndOutrosEnabled = settings.skipIntrosAndOutros
        chainSkipGapSeconds = Double(settings.chainSkipGapSeconds)
        playAdsForCurrentEpisode = false
        skippedAds = 0

        let resume = Self.clampedPlaybackTime(sameEpisode ? livePosition : episode.playbackPosition, duration: duration)
        currentTime = resume
        lastObservedTime = resume
        lastPersistTime = 0  // allow the first throttled persist to write immediately

        let speed = podcast?.customPlaybackSpeed ?? settings.defaultPlaybackSpeed
        playbackRate = speed
        needsItemBuild = true
        updateNowPlayingInfo()
        loadArtworkForNowPlaying(url: displayArtworkURL)

        if settings.lastPlayedEpisodeServerID != episode.serverID {
            settings.lastPlayedEpisodeServerID = episode.serverID
        }
        if let container = modelContainer {
            try? container.mainContext.save()
        }
    }

    /// Sanitised skip regions for the episode, or none when analysis is off
    /// or when a local file is a different render than the markers describe.
    private func regions(for episode: Episode, kind: PlaybackSourceKind) -> [AdRegion] {
        guard analysisEnabled else {
            markersSuppressedForLocalFile = false
            return []
        }
        if kind == .local, !episode.localFileMatchesServerAudio {
            markersSuppressedForLocalFile = !episode.adMarkers.isEmpty
            if markersSuppressedForLocalFile {
                Log.player.notice("Local file for \"\(episode.title, privacy: .public)\" differs from the server's audio; not applying its markers")
            }
            return []
        }
        markersSuppressedForLocalFile = false
        return episode.adMarkers
            .filter { !$0.isDeleted }
            .compactMap {
                AdRegion.sanitized(
                    startSeconds: $0.startSeconds,
                    endSeconds: $0.endSeconds,
                    kind: $0.kind,
                    episodeDuration: duration
                )
            }
            .sorted { $0.startSeconds < $1.startSeconds }
    }

    private func buildItem(autoPlay: Bool, generation: Int) async {
        guard generation == loadGeneration, let episode = currentEpisodeModel() else { return }
        needsItemBuild = false
        if autoPlay {
            wantsToPlay = true
        }
        switch resolveSource(for: episode) {
        case .localFile(let url):
            installItem(url: url, kind: .local, episode: episode)
        case .stream:
            let serverID = episode.serverID
            await runPreparation(generation: generation) { service in
                await service.startStreaming(serverID: serverID, generation: generation)
            }
        case .unavailable(let reason):
            if reason == .audioNotOnServer {
                let serverID = episode.serverID
                await runPreparation(generation: generation) { service in
                    await service.prepareOnServer(serverID: serverID, generation: generation, alreadyRequested: false)
                }
            } else {
                showUnavailable(message: reason.message, canDownload: reason.suggestsDownload)
            }
        }
    }

    /// Runs a cancellable preparation step (so a new load, a finished
    /// download, or `serverAudioBecameAvailable` can cut it short).
    private func runPreparation(generation: Int, _ body: @escaping @MainActor (PlayerService) async -> Void) async {
        prepareTask?.cancel()
        let task = Task { @MainActor [weak self] in
            guard let self, generation == self.loadGeneration else { return }
            await body(self)
        }
        prepareTask = task
        await task.value
    }

    /// Mints a fresh signed URL (`POST /audio-url`) and installs a stream
    /// item. A 409 means the server lacks the audio: prepare on the server.
    private func startStreaming(serverID: Int, generation: Int) async {
        status = .preparing("Connecting to your server…")
        do {
            let signed = try await NoadcastAPIClient.shared.audioURL(episodeID: serverID)
            guard generation == loadGeneration, !Task.isCancelled else { return }
            installItem(url: signed.url, kind: .stream, episode: currentEpisodeModel())
        } catch let error as APIError where error.isAudioUnavailable {
            guard generation == loadGeneration, !Task.isCancelled else { return }
            // The 409 already enqueued a priority download on the server.
            await prepareOnServer(serverID: serverID, generation: generation, alreadyRequested: true)
        } catch {
            guard generation == loadGeneration, !Task.isCancelled else { return }
            let apiError = APIError.from(transportError: error)
            guard apiError != .cancelled else { return }
            if apiError.isOffline || !NetworkMonitor.shared.isOnline {
                showUnavailable(message: PlaybackUnavailableReason.offline.message, canDownload: true)
            } else {
                showUnavailable(message: apiError.localizedDescription, canDownload: true)
            }
        }
    }

    /// The server does not hold the audio: ask it to fetch it (priority,
    /// `POST /process`) and poll the episode until the audio is present,
    /// showing "Preparing…" (with the job's status text when available).
    private func prepareOnServer(serverID: Int, generation: Int, alreadyRequested: Bool) async {
        status = .preparing("Preparing the episode on your server…")
        if !alreadyRequested {
            do {
                try await NoadcastAPIClient.shared.process(episodeID: serverID)
            } catch {
                Log.player.notice("process request failed: \(error.localizedDescription, privacy: .public)")
            }
        }
        let deadline = Date().addingTimeInterval(Self.prepareTimeoutSeconds)
        var delay: Double = 3
        while Date() < deadline {
            try? await Task.sleep(for: .seconds(delay))
            guard generation == loadGeneration, !Task.isCancelled else { return }
            if let job = SyncService.shared.activeJobs[serverID] {
                let detail = TimeFormatting.progressDetail(for: job)
                let text = job.statusText ?? job.state.label
                status = .preparing(detail.map { "\(text) · \($0)" } ?? text)
            }
            do {
                let dto = try await NoadcastAPIClient.shared.episode(id: serverID)
                guard generation == loadGeneration, !Task.isCancelled else { return }
                await SyncService.shared.applyEpisode(dto)
                guard generation == loadGeneration, !Task.isCancelled else { return }
                if dto.audioState.isPresent {
                    await startStreaming(serverID: serverID, generation: generation)
                    return
                }
                if dto.state == .failed {
                    showUnavailable(message: dto.error ?? "Your server couldn't fetch this episode.", canDownload: false)
                    return
                }
            } catch let error as APIError where error == .unauthorized {
                showUnavailable(message: error.localizedDescription, canDownload: false)
                return
            } catch {
                // Transient (offline, server restarting): keep waiting.
            }
            delay = min(delay * 1.5, 10)
        }
        guard generation == loadGeneration else { return }
        showUnavailable(message: "Your server is taking too long to prepare this episode.", canDownload: false)
    }

    private func installItem(url: URL, kind: PlaybackSourceKind, episode: Episode?) {
        teardownObservers()
        let itemState = Log.signposter.beginInterval("AVPlayerItem.init")
        // Precise timing: VBR MP3 without a Xing header otherwise seeks by
        // estimation, landing tens of seconds off — fatal for ad skipping.
        let asset = AVURLAsset(url: url, options: [AVURLAssetPreferPreciseDurationAndTimingKey: true])
        let item = AVPlayerItem(asset: asset)
        Log.signposter.endInterval("AVPlayerItem.init", itemState)
        if kind == .stream {
            item.preferredForwardBufferDuration = Self.forwardBufferDuration(forRate: playbackRate)
        }
        player.replaceCurrentItem(with: item)
        loadChapters(from: url, item: item)
        sourceKind = kind
        needsItemBuild = false
        isRecovering = false
        stallStartedAt = nil
        bufferedRanges = []
        status = kind == .stream ? .buffering : .ready
        if let episode {
            adRegions = regions(for: episode, kind: kind)
        }
        skipPlanner.reset()

        let resume = currentTime
        if resume > 0 {
            player.seek(
                to: CMTime(seconds: resume, preferredTimescale: 600),
                toleranceBefore: .zero,
                toleranceAfter: .zero
            )
        }
        installPeriodicObserver()
        installEndObserver()
        installFailureObserver(for: item)
        startStateMonitor()
        updateNowPlayingInfo()
        if wantsToPlay {
            play()
        }
    }

    private func loadChapters(from url: URL, item: AVPlayerItem) {
        chapterTask?.cancel()
        chapters = []
        isLoadingChapters = true
        chapterTask = Task { @MainActor [weak self] in
            let result = await EpisodeChapterReader.read(from: url)
            guard let self, !Task.isCancelled, self.player.currentItem === item else { return }
            self.chapters = result
            self.isLoadingChapters = false
            self.chapterTask = nil
        }
    }

    private func showUnavailable(message: String, canDownload: Bool) {
        teardownObservers()
        prepareTask = nil
        chapterTask?.cancel()
        chapterTask = nil
        chapters = []
        isLoadingChapters = false
        player.pause()
        player.replaceCurrentItem(with: nil)
        sourceKind = .none
        isPlaying = false
        wantsToPlay = false
        needsItemBuild = true
        isRecovering = false
        let downloaded = currentEpisodeModel()?.isMarkedDownloaded ?? false
        status = .unavailable(message: message, canDownload: canDownload && !downloaded)
        updateNowPlayingInfo()
        persistPosition(force: true)
    }

    private func swapToLocalFile() {
        pendingLocalSwap = false
        guard let episode = currentEpisodeModel(), episode.hasLocalFile, let url = episode.localFileURL else { return }
        prepareTask?.cancel()
        prepareTask = nil
        recoveryAttempts = 0
        Log.player.info("Switching \"\(episode.title, privacy: .public)\" to the downloaded file")
        installItem(url: url, kind: .local, episode: episode)
    }

    // MARK: - Failure recovery

    /// Network drop or signed-URL expiry: capture the position, mint a
    /// fresh URL, rebuild, seek back precisely, resume — 3 attempts with
    /// 1/2/4 s backoff. Offline and not downloaded: pause with Retry and
    /// Download.
    private func handleItemFailure(message: String?) {
        guard !isRecovering, player.currentItem != nil else { return }
        Log.player.error("Playback failed: \(message ?? "unknown", privacy: .public)")
        guard let episode = currentEpisodeModel() else { return }
        if sourceKind == .stream, episode.hasLocalFile {
            swapToLocalFile()
            return
        }
        guard sourceKind == .stream else {
            showUnavailable(message: "This download couldn't be played. Delete it and download it again.", canDownload: false)
            return
        }
        guard NetworkMonitor.shared.isOnline else {
            showUnavailable(message: PlaybackUnavailableReason.offline.message, canDownload: true)
            return
        }
        guard recoveryAttempts < Self.recoveryDelays.count else {
            showUnavailable(message: "Streaming failed. Check the connection to your server.", canDownload: true)
            return
        }
        let delay = Self.recoveryDelays[recoveryAttempts]
        recoveryAttempts += 1
        let resume = isPlaying || wantsToPlay
        let serverID = episode.serverID
        let generation = loadGeneration
        isRecovering = true
        teardownObservers()
        player.replaceCurrentItem(with: nil)
        sourceKind = .none
        isPlaying = false
        wantsToPlay = resume
        status = .buffering
        prepareTask?.cancel()
        prepareTask = Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            guard let self, generation == self.loadGeneration, !Task.isCancelled else { return }
            self.isRecovering = false
            await self.startStreaming(serverID: serverID, generation: generation)
        }
    }

    // MARK: - Observers

    private func installPeriodicObserver() {
        let interval = CMTime(seconds: 0.25, preferredTimescale: 600)
        timeObserverToken = player.addPeriodicTimeObserver(
            forInterval: interval,
            queue: .main
        ) { [weak self] time in
            let t = time.seconds
            Task { @MainActor [weak self] in
                guard let self else { return }
                if t.isFinite { self.currentTime = t }
                if let dur = self.player.currentItem?.duration.seconds, dur.isFinite, dur > 0 {
                    self.duration = dur
                    self.sanitizeAdRegionsForCurrentDuration()
                }
                self.accumulatePlayedTime()
                if self.maybeSkipAd() { return }
                self.persistPosition(force: false)
                self.lastObservedTime = self.currentTime
            }
        }
    }

    /// Add to `AppSettings.lifetimePlayedSeconds` based on how far the audio
    /// advanced since the last tick. Filter out anything that doesn't look
    /// like natural forward progress (seeks, ad skips — those are accounted
    /// for separately in `maybeSkipAd`).
    private func accumulatePlayedTime() {
        let delta = currentTime - lastObservedTime
        // A single observer tick advances by ~rate × interval, capped at ~2s
        // even at 3.6×. Anything outside (0, 5] is a seek or a glitch.
        guard delta > 0, delta <= 5 else { return }
        addPendingLifetimePlayedSeconds(delta)
    }

    private func installEndObserver() {
        endObserver = NotificationCenter.default.addObserver(
            forName: .AVPlayerItemDidPlayToEndTime,
            object: player.currentItem,
            queue: .main
        ) { _ in
            Task { @MainActor [weak self] in self?.handlePlaybackFinished() }
        }
    }

    private func installFailureObserver(for item: AVPlayerItem) {
        failureObserver = NotificationCenter.default.addObserver(
            forName: AVPlayerItem.failedToPlayToEndTimeNotification,
            object: item,
            queue: .main
        ) { note in
            let message = (note.userInfo?[AVPlayerItemFailedToPlayToEndTimeErrorKey] as? Error)?.localizedDescription
            Task { @MainActor [weak self] in self?.handleItemFailure(message: message) }
        }
    }

    private func teardownObservers() {
        if let token = timeObserverToken {
            player.removeTimeObserver(token)
            timeObserverToken = nil
        }
        if let obs = endObserver {
            NotificationCenter.default.removeObserver(obs)
            endObserver = nil
        }
        if let obs = failureObserver {
            NotificationCenter.default.removeObserver(obs)
            failureObserver = nil
        }
        monitorTask?.cancel()
        monitorTask = nil
    }

    /// 1 Hz: buffering/stall state from `timeControlStatus` /
    /// `reasonForWaitingToPlay` / `isPlaybackLikelyToKeepUp`, the loaded
    /// ranges of a stream, item failures, and stall recovery. (The periodic
    /// time observer does not fire while stalled, so this is a separate loop.)
    private func startStateMonitor() {
        monitorTask?.cancel()
        monitorTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                guard let self, !Task.isCancelled else { return }
                self.pollPlayerState()
            }
        }
    }

    private func pollPlayerState() {
        guard let item = player.currentItem else { return }
        if item.status == .failed {
            handleItemFailure(message: item.error?.localizedDescription)
            return
        }
        let now = ProcessInfo.processInfo.systemUptime
        let waiting = player.timeControlStatus == .waitingToPlayAtSpecifiedRate
        if waiting, wantsToPlay || isPlaying {
            let reason = player.reasonForWaitingToPlay
            let starved = reason == .toMinimizeStalls || reason == .evaluatingBufferingRate || !item.isPlaybackLikelyToKeepUp
            if starved, status != .buffering {
                status = .buffering
            }
            if stallStartedAt == nil {
                stallStartedAt = now
            }
            smoothPlaybackTicks = 0
            if sourceKind == .stream, let started = stallStartedAt {
                if !NetworkMonitor.shared.isOnline, !item.isPlaybackLikelyToKeepUp {
                    showUnavailable(message: PlaybackUnavailableReason.offline.message, canDownload: true)
                    return
                }
                if now - started > Self.stallRecoverySeconds {
                    stallStartedAt = nil
                    handleItemFailure(message: "Stalled for \(Int(Self.stallRecoverySeconds)) s")
                    return
                }
            }
        } else {
            stallStartedAt = nil
            if status == .buffering {
                status = .ready
            }
            if player.timeControlStatus == .playing {
                smoothPlaybackTicks += 1
                // 30 s of healthy playback restores the recovery budget.
                if smoothPlaybackTicks >= 30, recoveryAttempts > 0 {
                    recoveryAttempts = 0
                }
            }
        }
        if sourceKind == .stream {
            let ranges: [ClosedRange<Double>] = item.loadedTimeRanges.compactMap { value in
                let range = value.timeRangeValue
                let start = range.start.seconds
                let end = CMTimeRangeGetEnd(range).seconds
                guard start.isFinite, end.isFinite, end > start else { return nil }
                return start...end
            }
            if ranges != bufferedRanges {
                bufferedRanges = ranges
            }
        }
    }

    // MARK: - Ad skipping

    /// Decision logic lives in `AdSkipPlanner` (chain-skip walk and
    /// end-of-episode branch unchanged, plus the loop guard). Returns `true`
    /// when playback finished.
    @discardableResult
    private func maybeSkipAd() -> Bool {
        let decision = skipPlanner.decide(
            currentTime: currentTime,
            duration: duration,
            regions: adRegions,
            chainSkipGapSeconds: chainSkipGapSeconds,
            skipsAds: skipAdsEnabled && !playAdsForCurrentEpisode,
            skipsIntrosAndOutros: skipIntrosAndOutrosEnabled,
            now: ProcessInfo.processInfo.systemUptime
        )
        switch decision {
        case .none:
            return false
        case .finish(let skipped, let saved):
            skippedAds += skipped
            addPendingLifetimeAdSkipSeconds(saved)
            player.pause()
            player.seek(to: CMTime(seconds: duration, preferredTimescale: 600))
            currentTime = duration
            lastObservedTime = duration
            updateNowPlayingInfo()
            handlePlaybackFinished()
            return true
        case .seek(let target, let skipped, let saved):
            skippedAds += skipped
            addPendingLifetimeAdSkipSeconds(saved)
            performSkipSeek(to: target)
            return false
        }
    }

    /// Skip seeks never land before the target (`toleranceBefore: .zero`);
    /// the planner's pending guard is cleared when the seek completes.
    private func performSkipSeek(to seconds: Double) {
        let clamped = Self.clampedPlaybackTime(seconds, duration: duration)
        currentTime = clamped
        lastObservedTime = clamped
        let generation = loadGeneration
        player.seek(
            to: CMTime(seconds: clamped, preferredTimescale: 600),
            toleranceBefore: .zero,
            toleranceAfter: .zero
        ) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.loadGeneration == generation else { return }
                self.skipPlanner.skipSeekCompleted()
            }
        }
        updateNowPlayingInfo()
    }

    private func sanitizeAdRegionsForCurrentDuration() {
        guard duration > 0 else { return }
        let sanitized = adRegions.compactMap {
            AdRegion.sanitized(
                startSeconds: $0.startSeconds,
                endSeconds: $0.endSeconds,
                kind: $0.kind,
                episodeDuration: duration
            )
        }
        if sanitized != adRegions {
            adRegions = sanitized.sorted { $0.startSeconds < $1.startSeconds }
        }
    }

    private nonisolated static func positiveFiniteDuration(_ seconds: Double?) -> Double? {
        guard let seconds, seconds.isFinite, seconds > 0 else { return nil }
        return seconds
    }

    nonisolated static func clampedPlaybackTime(_ seconds: Double, duration: Double) -> Double {
        guard seconds.isFinite else { return 0 }
        if duration.isFinite, duration > 0 {
            return max(0, min(duration, seconds))
        }
        return max(0, seconds)
    }

    private func addPendingLifetimePlayedSeconds(_ amount: Double) {
        guard amount > 0 else { return }
        pendingLifetimePlayedSeconds += amount
        flushLifetimeStats(force: false)
    }

    private func addPendingLifetimeAdSkipSeconds(_ amount: Double) {
        guard amount > 0 else { return }
        pendingLifetimeAdSkipSeconds += amount
        flushLifetimeStats(force: false)
    }

    private static let lifetimeStatsFlushInterval: TimeInterval = 60.0

    private func flushLifetimeStats(force: Bool) {
        guard let container = modelContainer else { return }
        guard pendingLifetimePlayedSeconds > 0 || pendingLifetimeAdSkipSeconds > 0 else { return }
        let now = ProcessInfo.processInfo.systemUptime
        if !force, now - lastLifetimeStatsFlushTime < Self.lifetimeStatsFlushInterval {
            return
        }

        let playedSeconds = pendingLifetimePlayedSeconds
        let adSkipSeconds = pendingLifetimeAdSkipSeconds
        let context = container.mainContext
        let settings = AppSettings.current(in: context)
        settings.lifetimePlayedSeconds += pendingLifetimePlayedSeconds
        settings.lifetimeAdSkipSeconds += pendingLifetimeAdSkipSeconds
        UsageHistoryDay.recordPlayback(
            playedSeconds: playedSeconds,
            adSkippedSeconds: adSkipSeconds,
            in: context
        )
        pendingLifetimePlayedSeconds = 0
        pendingLifetimeAdSkipSeconds = 0
        lastLifetimeStatsFlushTime = now
        try? context.save()
    }

    // MARK: - Persistence

    private static let persistInterval: TimeInterval = 3.0

    private func persistPosition(force: Bool) {
        guard let id = currentEpisodeID, let container = modelContainer else { return }
        // Throttle: persist at most once every `persistInterval` seconds
        // during continuous playback. Forced calls (pause, background,
        // playback-finished) write immediately.
        let now = ProcessInfo.processInfo.systemUptime
        if !force, now - lastPersistTime < Self.persistInterval { return }
        lastPersistTime = now

        let position = currentTime
        let context = ModelContext(container)
        if let episode = context.model(for: id) as? Episode, episode.playbackPosition != position {
            episode.playbackPosition = position
            try? context.save()
        }
    }

    private func handlePlaybackFinished() {
        isPlaying = false
        wantsToPlay = false
        playAdsForCurrentEpisode = false
        pendingLocalSwap = false
        guard let id = currentEpisodeID, let container = modelContainer else { return }
        let context = container.mainContext
        guard let episode = context.model(for: id) as? Episode else { return }
        flushLifetimeStats(force: true)
        episode.isPlayed = true
        episode.datePlayed = .now
        episode.playbackPosition = episode.duration ?? duration

        let settings = AppSettings.current(in: context)
        if settings.autoDeleteAfterPlayed {
            if episode.downloadState.isActive {
                DownloadManager.shared.cancelTransfer(serverID: episode.serverID, discardResumeData: true)
            }
            if let localURL = episode.localFileURL {
                try? FileManager.default.removeItem(at: localURL)
            }
            episode.localFilename = nil
            episode.fileSizeBytes = nil
            episode.localAudioSha256 = nil
            episode.setDownloadState(.idle)
        }

        // Remove the just-finished episode from the queue (if present).
        let allQueue = (try? context.fetch(FetchDescriptor<QueueItem>())) ?? []
        for item in allQueue where item.episode == episode {
            context.delete(item)
        }
        try? context.save()

        // Retention release: the server may delete its copy now.
        SyncService.shared.releaseAudio(episodeServerID: episode.serverID)

        // Auto-advance to the first item that can play right now, in queue
        // order (never reordering). If nothing can play, the finished
        // episode stays loaded (mini-bar visible, user can pick).
        let remaining = (try? context.fetch(
            FetchDescriptor<QueueItem>(sortBy: [SortDescriptor(\QueueItem.position)])
        )) ?? []
        let candidates = remaining.compactMap(\.episode)
        let sources = candidates.map { resolveSource(for: $0) }
        if let index = PlaybackSourceResolver.firstPlayableIndex(sources) {
            let next = candidates[index]
            Task { await self.load(episode: next, settings: settings, autoPlay: true) }
        }
    }

    // MARK: - Audio session + remote commands

    /// Set up the shared audio session's category, but DON'T activate it yet.
    ///
    /// Calling `setActive(true)` with the `.playback` category interrupts
    /// whatever else is producing audio (Music, Spotify, podcasts in other
    /// apps), which is jarring if the user just opened Noadcast to browse
    /// — they didn't ask us to take over. We defer activation until the
    /// user actually presses play.
    private func configureAudioSession() {
        let session = AVAudioSession.sharedInstance()
        Log.signposter.withIntervalSignpost("AVAudioSession.setCategory") {
            try? session.setCategory(.playback, mode: .spokenAudio, policy: .longFormAudio)
        }
    }

    /// Activate the shared audio session right before starting playback.
    /// Idempotent: AVAudioSession ignores `setActive(true)` when already
    /// active. Called from `play()` only.
    private func activateAudioSessionIfNeeded() {
        try? AVAudioSession.sharedInstance().setActive(true)
    }

    private func configureRemoteCommands() {
        let cc = MPRemoteCommandCenter.shared()
        cc.playCommand.addTarget { [weak self] _ in
            Task { @MainActor in self?.play() }
            return .success
        }
        cc.pauseCommand.addTarget { [weak self] _ in
            Task { @MainActor in self?.pause() }
            return .success
        }
        cc.togglePlayPauseCommand.addTarget { [weak self] _ in
            Task { @MainActor in self?.togglePlayPause() }
            return .success
        }
        cc.skipForwardCommand.preferredIntervals = [30]
        cc.skipForwardCommand.addTarget { [weak self] _ in
            Task { @MainActor in self?.skipForward() }
            return .success
        }
        cc.skipBackwardCommand.preferredIntervals = [15]
        cc.skipBackwardCommand.addTarget { [weak self] _ in
            Task { @MainActor in self?.skipBackward() }
            return .success
        }
        cc.changePlaybackPositionCommand.addTarget { [weak self] event in
            guard let event = event as? MPChangePlaybackPositionCommandEvent else { return .commandFailed }
            Task { @MainActor in self?.seek(to: event.positionTime) }
            return .success
        }
    }

    private func updateNowPlayingInfo() {
        let center = MPNowPlayingInfoCenter.default()
        var info: [String: Any] = center.nowPlayingInfo ?? [:]
        info[MPMediaItemPropertyTitle] = currentEpisodeTitle
        info[MPMediaItemPropertyArtist] = currentPodcastTitle
        info[MPMediaItemPropertyAlbumTitle] = currentPodcastTitle
        info[MPMediaItemPropertyPlaybackDuration] = duration
        info[MPNowPlayingInfoPropertyElapsedPlaybackTime] = currentTime
        info[MPNowPlayingInfoPropertyPlaybackRate] = isPlaying ? playbackRate : 0
        // Preserve any previously-set artwork. `loadArtworkForNowPlaying` is
        // responsible for inserting/replacing it.
        center.nowPlayingInfo = info
    }

    /// Fetches the podcast's artwork (if any) and pushes it into the Now
    /// Playing info dictionary so it appears on the lock screen / Control
    /// Center / Dynamic Island. Cached in-process for the session.
    private func loadArtworkForNowPlaying(url: URL?) {
        artworkFetchTask?.cancel()
        guard let url else {
            clearNowPlayingArtwork()
            return
        }
        if let cached = artworkCache[url] {
            applyNowPlayingArtwork(cached)
            return
        }
        // Drop the previous episode's artwork while we fetch the new one,
        // otherwise the lock screen briefly shows mismatched art + title.
        clearNowPlayingArtwork()
        artworkFetchTask = Task.detached(priority: .utility) { [weak self] in
            do {
                let image: UIImage?
                if url.isFileURL {
                    image = UIImage(contentsOfFile: url.path)
                } else {
                    let (data, _) = try await URLSession.shared.data(from: url)
                    image = UIImage(data: data)
                }
                if Task.isCancelled { return }
                guard let image else { return }
                await MainActor.run { [weak self] in
                    guard let self else { return }
                    self.artworkCache[url] = image
                    // Only apply if we're still on the same episode.
                    if self.artworkURL == url {
                        self.applyNowPlayingArtwork(image)
                    }
                }
            } catch {
                Log.player.notice("Artwork fetch failed: \(error.localizedDescription, privacy: .public)")
            }
        }
    }

    private func applyNowPlayingArtwork(_ image: UIImage) {
        let center = MPNowPlayingInfoCenter.default()
        var info = center.nowPlayingInfo ?? [:]
        let artwork = MPMediaItemArtwork(boundsSize: image.size) { _ in image }
        info[MPMediaItemPropertyArtwork] = artwork
        center.nowPlayingInfo = info
    }

    private func clearNowPlayingArtwork() {
        let center = MPNowPlayingInfoCenter.default()
        var info = center.nowPlayingInfo ?? [:]
        info.removeValue(forKey: MPMediaItemPropertyArtwork)
        center.nowPlayingInfo = info
    }
}
