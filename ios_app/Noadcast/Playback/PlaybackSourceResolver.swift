import Foundation

/// Why an episode cannot be played right now.
nonisolated enum PlaybackUnavailableReason: Equatable, Sendable {
    /// No server address configured and nothing on disk.
    case notConfigured
    /// Not downloaded and the device is offline.
    case offline
    /// Streaming policy is "Never".
    case streamingDisabled
    /// Streaming policy is "Wi-Fi only" and the device is on cellular.
    case streamingRequiresWiFi
    /// Reachable and allowed to stream, but the server does not hold the
    /// audio (`absent` / `partial` / `evicted`). The player asks the server
    /// to fetch it (`POST /process`) and shows "Preparing…".
    case audioNotOnServer

    var message: String {
        switch self {
        case .notConfigured: "Connect to your Noadcast server in Settings, or download the episode."
        case .offline: "You're offline and this episode isn't downloaded."
        case .streamingDisabled: "Streaming is turned off. Download the episode to play it."
        case .streamingRequiresWiFi: "Streaming is limited to Wi-Fi. Download the episode or join Wi-Fi."
        case .audioNotOnServer: "Preparing the episode on the server…"
        }
    }

    /// A download could fix it (it will start when the network allows).
    var suggestsDownload: Bool {
        switch self {
        case .offline, .streamingDisabled, .streamingRequiresWiFi: true
        case .notConfigured, .audioNotOnServer: false
        }
    }
}

nonisolated enum PlaybackSource: Equatable, Sendable {
    case localFile(URL)
    case stream
    case unavailable(PlaybackUnavailableReason)

    /// Can start immediately.
    var isPlayableNow: Bool {
        switch self {
        case .localFile, .stream: true
        case .unavailable: false
        }
    }

    /// Can start now, or after the server prepares the audio.
    var canStartPlayback: Bool {
        switch self {
        case .localFile, .stream: true
        case .unavailable(let reason): reason == .audioNotOnServer
        }
    }
}

/// Everything the source decision depends on, captured as plain values so
/// the decision table is a pure function (and unit-testable).
nonisolated struct PlaybackSourceInputs: Equatable, Sendable {
    /// Non-nil only when the file is actually on disk.
    var localFileURL: URL?
    var isServerConfigured: Bool
    var isOnline: Bool
    var isWiFi: Bool
    var audioState: ServerAudioState
    var streamingPolicy: StreamingPolicy

    init(
        localFileURL: URL?,
        isServerConfigured: Bool,
        isOnline: Bool,
        isWiFi: Bool,
        audioState: ServerAudioState,
        streamingPolicy: StreamingPolicy
    ) {
        self.localFileURL = localFileURL
        self.isServerConfigured = isServerConfigured
        self.isOnline = isOnline
        self.isWiFi = isWiFi
        self.audioState = audioState
        self.streamingPolicy = streamingPolicy
    }
}

/// Local file wins → stream when configured + online + policy allows +
/// the server has the audio → otherwise unavailable (with the reason).
/// Nothing about the player depends on reachability when a file exists.
nonisolated enum PlaybackSourceResolver {
    static func resolve(_ inputs: PlaybackSourceInputs) -> PlaybackSource {
        if let url = inputs.localFileURL {
            return .localFile(url)
        }
        guard inputs.isServerConfigured else {
            return .unavailable(.notConfigured)
        }
        guard inputs.isOnline else {
            return .unavailable(.offline)
        }
        switch inputs.streamingPolicy {
        case .never:
            return .unavailable(.streamingDisabled)
        case .wifiOnly where !inputs.isWiFi:
            return .unavailable(.streamingRequiresWiFi)
        case .wifiOnly, .anyNetwork:
            break
        }
        guard inputs.audioState.isPresent else {
            return .unavailable(.audioNotOnServer)
        }
        return .stream
    }

    /// Auto-advance target: the first item, in queue order, that can play
    /// right now. Queue order wins over "is downloaded"; nothing is
    /// reordered — unplayable items are simply passed over.
    static func firstPlayableIndex(_ sources: [PlaybackSource]) -> Int? {
        sources.firstIndex { $0.isPlayableNow }
    }

    /// Row decision without touching the filesystem (`isDownloaded` is the
    /// row's `localFilename != nil`, not a file check).
    static func rowAction(
        isDownloaded: Bool,
        downloadState: DownloadState,
        serverState: ServerEpisodeState,
        audioState: ServerAudioState,
        isServerConfigured: Bool,
        isOnline: Bool,
        isWiFi: Bool,
        streamingPolicy: StreamingPolicy
    ) -> EpisodeRowAction {
        if isDownloaded {
            return .play
        }
        let source = resolve(PlaybackSourceInputs(
            localFileURL: nil,
            isServerConfigured: isServerConfigured,
            isOnline: isOnline,
            isWiFi: isWiFi,
            audioState: audioState,
            streamingPolicy: streamingPolicy
        ))
        switch source {
        case .localFile, .stream:
            return .play
        case .unavailable(let reason):
            switch reason {
            case .audioNotOnServer:
                if serverState == .failed {
                    return .retry
                }
                if downloadState.isActive || serverState.isActive {
                    return .inProgress
                }
                return .play
            case .streamingDisabled, .streamingRequiresWiFi:
                if downloadState.isActive {
                    return .inProgress
                }
                return downloadState == .failed ? .retry : .download
            case .offline, .notConfigured:
                return downloadState.isActive ? .inProgress : .unavailable
            }
        }
    }
}

/// The trailing affordance an episode row shows.
nonisolated enum EpisodeRowAction: Equatable, Sendable {
    /// Plays now (local file or stream), or starts "Preparing…" on the server.
    case play
    /// Something is in flight and nothing is playable yet.
    case inProgress
    /// A failure the user can retry.
    case retry
    /// Streaming isn't allowed on this network; downloading is the way.
    case download
    /// Offline or not connected to a server, and nothing on the device.
    case unavailable
}
