import Foundation

/// Server pipeline state (`Episode.state` in docs/API.md). The client never
/// drives this; it is a mirror written only by `SyncEngine`. Unknown strings
/// from a newer server decode to `.unknown` rather than failing the page.
nonisolated enum ServerEpisodeState: String, CaseIterable, Sendable, Decodable {
    case discovered
    case downloadPending = "download_pending"
    case downloading
    case downloaded
    case transcribePending = "transcribe_pending"
    case transcribing
    case transcribed
    case classifyPending = "classify_pending"
    case classifying
    case ready
    case failed
    case unknown

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = ServerEpisodeState(rawValue: raw) ?? .unknown
    }

    init(raw: String?) {
        self = raw.flatMap { ServerEpisodeState(rawValue: $0) } ?? .unknown
    }

    /// Queued or running on the server. `downloaded` / `transcribed` are
    /// resting points between stages and are deliberately not "active": a
    /// server that stops there (analysis disabled) must not leave the row
    /// spinning forever.
    var isActive: Bool {
        switch self {
        case .downloadPending, .downloading,
             .transcribePending, .transcribing,
             .classifyPending, .classifying:
            true
        case .discovered, .downloaded, .transcribed, .ready, .failed, .unknown:
            false
        }
    }

    var label: String {
        switch self {
        case .discovered: "Not processed"
        case .downloadPending: "Waiting to download on server"
        case .downloading: "Downloading on server"
        case .downloaded: "On server"
        case .transcribePending: "Waiting to transcribe"
        case .transcribing: "Transcribing"
        case .transcribed: "Transcribed"
        case .classifyPending: "Waiting to analyze"
        case .classifying: "Analyzing"
        case .ready: "Ready"
        case .failed: "Failed"
        case .unknown: "Processing"
        }
    }
}

/// Whether the server currently holds the episode's audio (`Episode.audioState`).
/// Orthogonal to `ServerEpisodeState`: an episode stays `ready` after its audio
/// is evicted, and keeps its markers.
nonisolated enum ServerAudioState: String, CaseIterable, Sendable, Decodable {
    case absent
    case partial
    case present
    case evicted
    case unknown

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = ServerAudioState(rawValue: raw) ?? .unknown
    }

    init(raw: String?) {
        self = raw.flatMap { ServerAudioState(rawValue: $0) } ?? .unknown
    }

    /// Streamable / downloadable from the server right now.
    var isPresent: Bool { self == .present }
}

/// `Episode.transcriptState`.
nonisolated enum ServerTranscriptState: String, CaseIterable, Sendable, Decodable {
    case notStarted = "none"
    case ready
    case stale
    case failed
    case unknown

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = ServerTranscriptState(rawValue: raw) ?? .unknown
    }
}

/// `Episode.classifyState`.
nonisolated enum ServerClassifyState: String, CaseIterable, Sendable, Decodable {
    case notStarted = "none"
    case ready
    case stale
    case failed
    case skipped
    case unknown

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = ServerClassifyState(rawValue: raw) ?? .unknown
    }
}

/// Stage reported by `GET /jobs/active`. Units: `download` in bytes,
/// `transcribe` in audio seconds, `classify` indeterminate.
nonisolated enum JobStage: String, CaseIterable, Sendable, Decodable {
    case download
    case transcribe
    case classify
    case unknown

    init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = JobStage(rawValue: raw) ?? .unknown
    }
}

/// Client-side download of the audio to this device. Independent of the
/// server axis: both can be active at once (e.g. the server is still
/// classifying while the phone pulls the audio).
nonisolated enum DownloadState: String, CaseIterable, Sendable {
    /// Not on the device and not wanted.
    case idle
    /// Wanted on the device; waiting for the network policy, a free slot, or
    /// the server to have the audio.
    case queued
    case downloading
    case downloaded
    case failed

    var isActive: Bool {
        switch self {
        case .queued, .downloading: true
        case .idle, .downloaded, .failed: false
        }
    }
}

nonisolated enum AutoDownloadPolicy: String, Codable, CaseIterable, Sendable {
    case wifiOnly
    case anyNetwork
    case manualOnly

    var label: String {
        switch self {
        case .wifiOnly: "Wi-Fi only"
        case .anyNetwork: "Any network"
        case .manualOnly: "Manual only"
        }
    }
}

/// When the player may stream an episode that is not downloaded. Device-local
/// preference kept in `UserDefaults` (rows read it through `@AppStorage`, so
/// it must not live in `AppSettings`, whose writes invalidate every query).
nonisolated enum StreamingPolicy: String, Codable, CaseIterable, Sendable {
    case never
    case wifiOnly
    case anyNetwork

    static let storageKey = "StreamingPolicy"
    static let defaultValue: StreamingPolicy = .anyNetwork

    var label: String {
        switch self {
        case .never: "Never"
        case .wifiOnly: "Wi-Fi only"
        case .anyNetwork: "Any network"
        }
    }

    func allowsStreaming(isOnline: Bool, isWiFi: Bool) -> Bool {
        guard isOnline else { return false }
        switch self {
        case .never: return false
        case .wifiOnly: return isWiFi
        case .anyNetwork: return true
        }
    }

    static var current: StreamingPolicy {
        UserDefaults.standard.string(forKey: storageKey)
            .flatMap { StreamingPolicy(rawValue: $0) } ?? defaultValue
    }
}

nonisolated enum PodcastSortMode: String, Codable, CaseIterable, Sendable {
    case latestEpisode
    case alphabetical

    var label: String {
        switch self {
        case .latestEpisode: "Latest episode"
        case .alphabetical: "Alphabetical"
        }
    }
}
