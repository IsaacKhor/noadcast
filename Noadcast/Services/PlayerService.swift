import Foundation
import AVFoundation
import MediaPlayer
import Observation
import SwiftData
import UIKit
import os

/// Lightweight snapshot of an episode's skip-segment metadata, captured at
/// the time playback starts so the audio loop doesn't need to talk to
/// SwiftData. `kind` is consulted by `maybeSkipAd` to decide whether the
/// user's per-kind toggle says to skip this one.
struct AdRegion: Sendable, Equatable {
    let startSeconds: Double
    let endSeconds: Double
    let kind: SegmentKind
}

@MainActor
@Observable
final class PlayerService {
    static let shared = PlayerService()

    private(set) var currentEpisodeID: PersistentIdentifier?
    private(set) var currentEpisodeTitle: String = ""
    private(set) var currentPodcastTitle: String = ""
    private(set) var artworkURL: URL?
    private(set) var currentTime: Double = 0
    private(set) var duration: Double = 0
    private(set) var isPlaying: Bool = false
    private(set) var playbackRate: Double = 1.0
    private(set) var volume: Float = 1.0
    private(set) var skippedAds: Int = 0
    private(set) var adRegions: [AdRegion] = []
    private(set) var playAdsForCurrentEpisode: Bool = false

    private let player: AVPlayer = AVPlayer()
    private var timeObserverToken: Any?
    private var endObserver: NSObjectProtocol?

    private weak var modelContainer: ModelContainer?
    /// Last time `persistPosition(force:)` actually wrote, in
    /// `ProcessInfo.systemUptime` seconds. Used to throttle saves.
    private var lastPersistTime: TimeInterval = 0
    /// Lifetime stats are accumulated in memory and flushed occasionally.
    /// Writing `AppSettings` on every 0.25s playback tick invalidates every
    /// live `@Query<AppSettings>` and makes list scrolling stutter.
    private var pendingLifetimePlayedSeconds: Double = 0
    private var pendingLifetimeAdSkipSeconds: Double = 0
    private var lastLifetimeStatsFlushTime: TimeInterval = 0
    private var willResignObserver: NSObjectProtocol?

    /// Cached artwork keyed by URL so we don't re-fetch every load.
    private var artworkCache: [URL: UIImage] = [:]
    /// In-flight artwork fetch — cancelled when the player loads a new episode.
    private var artworkFetchTask: Task<Void, Never>?

    /// Last playback position seen by the periodic time observer. Used to
    /// compute audio-time deltas for the speedup savings counter, and to
    /// ignore deltas that look like seeks rather than natural progress.
    private var lastObservedTime: Double = 0

    /// Skip-policy snapshot captured at `load(episode:settings:)` time. The
    /// ad toggle can also be updated live from Settings / Now Playing.
    private var skipAdsEnabled: Bool = true
    private var skipIntrosAndOutrosEnabled: Bool = true
    /// Gap in seconds: when the player skips a segment it then peeks ahead
    /// for another segment whose start is within this window and chains
    /// through it too.
    private var chainSkipGapSeconds: Double = 5

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

    // MARK: - Public controls

    func load(episode: Episode, settings: AppSettings) {
        guard let fileURL = episode.localFileURL, episode.hasLocalFile else { return }
        let loadState = Log.signposter.beginInterval("PlayerService.load")
        defer { Log.signposter.endInterval("PlayerService.load", loadState) }

        flushLifetimeStats(force: true)
        teardownObservers()

        let itemState = Log.signposter.beginInterval("AVPlayerItem.init")
        let item = AVPlayerItem(url: fileURL)
        Log.signposter.endInterval("AVPlayerItem.init", itemState)
        player.replaceCurrentItem(with: item)

        let podcast = episode.podcast
        let displayArtworkURL = episode.podcastArtworkDisplayURL ?? podcast?.artworkDisplayURL
        currentEpisodeID = episode.persistentModelID
        currentEpisodeTitle = episode.title
        currentPodcastTitle = episode.podcastTitle ?? podcast?.title ?? ""
        artworkURL = displayArtworkURL
        let analysisEnabled = podcast?.aiProcessingEnabled ?? true
        adRegions = analysisEnabled
            ? episode.adMarkers
                .filter { !$0.isDeleted }
                .map { AdRegion(startSeconds: $0.startSeconds, endSeconds: $0.endSeconds, kind: $0.kind) }
                .sorted { $0.startSeconds < $1.startSeconds }
            : []
        skipAdsEnabled = settings.skipAds
        skipIntrosAndOutrosEnabled = settings.skipIntrosAndOutros
        chainSkipGapSeconds = Double(settings.chainSkipGapSeconds)
        playAdsForCurrentEpisode = false
        skippedAds = 0

        let resume = episode.playbackPosition
        if resume > 0 {
            player.seek(to: CMTime(seconds: resume, preferredTimescale: 600))
            currentTime = resume
        } else {
            currentTime = 0
        }
        lastObservedTime = resume
        lastPersistTime = 0  // allow the first throttled persist to write immediately
        if let dur = episode.duration { duration = dur }

        let speed = podcast?.customPlaybackSpeed ?? settings.defaultPlaybackSpeed
        playbackRate = speed
        installPeriodicObserver()
        installEndObserver()
        updateNowPlayingInfo()
        loadArtworkForNowPlaying(url: displayArtworkURL)

        settings.lastPlayedEpisodeGUID = episode.guid
        if let container = modelContainer {
            try? container.mainContext.save()
        }
    }

    /// Stops playback and clears all current-episode UI state if the given
    /// episode is the one currently loaded. Used when the user deletes the
    /// playing episode from Queue or Downloads.
    func unloadIfCurrent(episodeID: PersistentIdentifier) {
        guard currentEpisodeID == episodeID else { return }
        flushLifetimeStats(force: true)
        teardownObservers()
        player.pause()
        player.replaceCurrentItem(with: nil)
        isPlaying = false
        currentEpisodeID = nil
        currentEpisodeTitle = ""
        currentPodcastTitle = ""
        artworkURL = nil
        currentTime = 0
        duration = 0
        adRegions = []
        playAdsForCurrentEpisode = false
        skippedAds = 0
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil

        if let container = modelContainer {
            let settings = AppSettings.current(in: container.mainContext)
            settings.lastPlayedEpisodeGUID = nil
            try? container.mainContext.save()
        }
    }

    /// Loads the last-played episode (if any) into the player without auto-
    /// playing. Call once at app launch from the root view.
    func restoreLastPlayedEpisode(context: ModelContext) {
        let state = Log.signposter.beginInterval("PlayerService.restoreLastPlayedEpisode")
        defer { Log.signposter.endInterval("PlayerService.restoreLastPlayedEpisode", state) }
        guard currentEpisodeID == nil else { return }  // already loaded
        let settings = AppSettings.current(in: context)
        guard let guid = settings.lastPlayedEpisodeGUID else { return }
        let descriptor = FetchDescriptor<Episode>(predicate: #Predicate { $0.guid == guid })
        guard let episode = try? context.fetch(descriptor).first else { return }
        guard episode.hasLocalFile else { return }
        load(episode: episode, settings: settings)
    }

    func play() {
        activateAudioSessionIfNeeded()
        player.rate = Float(playbackRate)
        isPlaying = true
        updateNowPlayingInfo()
    }

    func pause() {
        player.pause()
        isPlaying = false
        updateNowPlayingInfo()
        flushLifetimeStats(force: true)
        persistPosition(force: true)
    }

    func togglePlayPause() {
        isPlaying ? pause() : play()
    }

    func seek(to seconds: Double) {
        let clamped = max(0, min(duration, seconds))
        player.seek(to: CMTime(seconds: clamped, preferredTimescale: 600))
        currentTime = clamped
        // Suppress the next time-observer delta so this seek doesn't count
        // toward the speedup-savings tally.
        lastObservedTime = clamped
        updateNowPlayingInfo()
    }

    func skipForward(_ seconds: Double = 30) { seek(to: currentTime + seconds) }
    func skipBackward(_ seconds: Double = 15) { seek(to: currentTime - seconds) }

    func setPlaybackRate(_ rate: Double) {
        playbackRate = rate
        if isPlaying { player.rate = Float(rate) }
        updateNowPlayingInfo()
    }

    func setVolume(_ v: Float) {
        volume = max(0, min(1, v))
        player.volume = volume
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
                }
                self.accumulatePlayedTime()
                self.maybeSkipAd()
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

    private func teardownObservers() {
        if let token = timeObserverToken {
            player.removeTimeObserver(token)
            timeObserverToken = nil
        }
        if let obs = endObserver {
            NotificationCenter.default.removeObserver(obs)
            endObserver = nil
        }
    }

    // MARK: - Ad skipping

    private func maybeSkipAd() {
        // Find the region we're currently inside, and only act if the user
        // wants this kind skipped.
        guard let initial = adRegions.first(where: { $0.startSeconds <= currentTime && currentTime < $0.endSeconds }),
              shouldSkip(kind: initial.kind)
        else { return }

        // Chain-skip: walk forward through additional regions whose start
        // is within `chainSkipGapSeconds` of the previous region's end and
        // that the user also wants skipped. The seek jumps past all of
        // them in one go.
        var targetEnd = initial.endSeconds
        var skipped = 1
        while let next = adRegions.first(where: { region in
            region.startSeconds > targetEnd
                && region.startSeconds - targetEnd <= chainSkipGapSeconds
                && shouldSkip(kind: region.kind)
        }) {
            targetEnd = max(targetEnd, next.endSeconds)
            skipped += 1
        }

        skippedAds += skipped
        let saved = max(0, targetEnd - currentTime)
        addPendingLifetimeAdSkipSeconds(saved)
        seek(to: targetEnd + 0.05)
    }

    private func shouldSkip(kind: SegmentKind) -> Bool {
        switch kind {
        case .ad: skipAdsEnabled && !playAdsForCurrentEpisode
        case .intro, .outro: skipIntrosAndOutrosEnabled
        }
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
        // playback-finished) write immediately. The previous implementation
        // used a *debounce* — a 3-second timer reset on every periodic
        // observer tick — which meant the timer never fired during normal
        // playback and the position only got saved when playback paused.
        let now = ProcessInfo.processInfo.systemUptime
        if !force, now - lastPersistTime < Self.persistInterval { return }
        lastPersistTime = now

        let position = currentTime
        let context = ModelContext(container)
        if let episode = context.model(for: id) as? Episode {
            episode.playbackPosition = position
            try? context.save()
        }
    }

    private func handlePlaybackFinished() {
        isPlaying = false
        playAdsForCurrentEpisode = false
        guard let id = currentEpisodeID, let container = modelContainer else { return }
        let context = container.mainContext
        guard let episode = context.model(for: id) as? Episode else { return }
        flushLifetimeStats(force: true)
        episode.isPlayed = true
        episode.datePlayed = .now
        episode.playbackPosition = episode.duration ?? 0

        let settings = AppSettings.current(in: context)
        if settings.autoDeleteAfterPlayed, let localURL = episode.localFileURL {
            try? FileManager.default.removeItem(at: localURL)
            episode.localFilename = nil
            episode.fileSizeBytes = nil
        }

        // Remove the just-finished episode from the queue (if present).
        let allQueue = (try? context.fetch(FetchDescriptor<QueueItem>())) ?? []
        for item in allQueue where item.episode == episode {
            context.delete(item)
        }
        try? context.save()

        // Auto-advance: pick the lowest-position QueueItem that's ready to
        // play and start it. If nothing's ready, we just leave the player on
        // the finished episode (mini-bar stays visible, user can pick).
        let remaining = (try? context.fetch(
            FetchDescriptor<QueueItem>(sortBy: [SortDescriptor(\QueueItem.position)])
        )) ?? []
        for item in remaining {
            guard let next = item.episode else { continue }
            if next.processingState == .ready, next.hasLocalFile {
                load(episode: next, settings: settings)
                play()
                return
            }
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
