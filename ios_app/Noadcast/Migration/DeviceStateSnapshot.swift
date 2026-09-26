import Foundation

/// Device-local state keyed by `feedURL + guid`, the only identity that
/// survives a mirror wipe (server ids are reissued when the server database
/// is rebuilt, and did not exist at all in the previous on-device store).
///
/// Written in three situations, and re-applied by `DeviceStateRestoreService`
/// after the next full sync:
/// * `legacyStore` — exported once from the previous build's SwiftData store
///   (`Application Support/legacy-export.json`).
/// * `instanceChange` — the server's `instanceId` changed.
/// * `localReset` — the user reset the local cache.
///
/// All types spell out `nonisolated` and their `CodingKeys` so the Codable
/// conformances are usable from the sync actor under default MainActor
/// isolation.
nonisolated struct DeviceStateSnapshot: Codable, Sendable, Equatable {
    nonisolated struct PodcastState: Codable, Sendable, Equatable {
        var feedURL: String
        var title: String?
        var autoDownloadEnabled: Bool
        var customPlaybackSpeed: Double?
        /// The previous build's per-podcast "Detect & skip ads" (only set by
        /// the legacy export; restored to the server when it was off).
        var adAnalysisEnabled: Bool?

        nonisolated enum CodingKeys: String, CodingKey {
            case feedURL, title, autoDownloadEnabled, customPlaybackSpeed, adAnalysisEnabled
        }
    }

    nonisolated struct EpisodeState: Codable, Sendable, Equatable {
        var feedURL: String
        var guid: String
        var title: String?
        /// Filename inside `AudioStorage.episodesDirectory`, if the audio was
        /// on disk when the snapshot was taken.
        var localFilename: String?
        var fileSizeBytes: Int64?
        var audioMimeType: String?
        var playbackPosition: Double
        var isPlayed: Bool
        var datePlayed: Date?

        var key: EpisodeKey { EpisodeKey(feedURL: feedURL, guid: guid) }

        nonisolated enum CodingKeys: String, CodingKey {
            case feedURL, guid, title, localFilename, fileSizeBytes, audioMimeType
            case playbackPosition, isPlayed, datePlayed
        }
    }

    nonisolated struct EpisodeKey: Codable, Sendable, Hashable {
        var feedURL: String
        var guid: String

        /// Normalised identity used for matching (see `FeedURLKey`).
        var matchKey: String { FeedURLKey.normalize(feedURL) + "\u{1F}" + guid }

        nonisolated enum CodingKeys: String, CodingKey {
            case feedURL, guid
        }
    }

    nonisolated struct Stats: Codable, Sendable, Equatable {
        var lifetimePlayedSeconds: Double
        var lifetimeAdSkipSeconds: Double

        nonisolated enum CodingKeys: String, CodingKey {
            case lifetimePlayedSeconds, lifetimeAdSkipSeconds
        }
    }

    nonisolated struct UsageDay: Codable, Sendable, Equatable {
        var dayStart: Date
        var playbackSeconds: Double
        var adSkippedSeconds: Double

        nonisolated enum CodingKeys: String, CodingKey {
            case dayStart, playbackSeconds, adSkippedSeconds
        }
    }

    nonisolated struct Preferences: Codable, Sendable, Equatable {
        var defaultPlaybackSpeed: Double
        var autoDownloadPolicy: String
        var autoDeleteAfterPlayed: Bool
        var podcastSortMode: String
        var skipAds: Bool
        var skipIntrosAndOutros: Bool
        var chainSkipGapSeconds: Int
        /// The previous build's global "Analyze downloaded episodes".
        var adAnalysisEnabled: Bool

        nonisolated enum CodingKeys: String, CodingKey {
            case defaultPlaybackSpeed, autoDownloadPolicy, autoDeleteAfterPlayed, podcastSortMode
            case skipAds, skipIntrosAndOutros, chainSkipGapSeconds, adAnalysisEnabled
        }
    }

    nonisolated enum Source: String, Codable, Sendable {
        case legacyStore
        case instanceChange
        case localReset
    }

    var version: Int
    var source: Source
    var createdAt: Date
    var podcasts: [PodcastState]
    var episodes: [EpisodeState]
    /// Queue order, first = next up.
    var queue: [EpisodeKey]
    var lastPlayed: EpisodeKey?
    /// Legacy export only: restored at first launch, no server needed.
    var stats: Stats?
    var usageDays: [UsageDay]?
    var preferences: Preferences?

    nonisolated enum CodingKeys: String, CodingKey {
        case version, source, createdAt, podcasts, episodes, queue, lastPlayed
        case stats, usageDays, preferences
    }

    init(
        source: Source,
        createdAt: Date = Date(),
        podcasts: [PodcastState] = [],
        episodes: [EpisodeState] = [],
        queue: [EpisodeKey] = [],
        lastPlayed: EpisodeKey? = nil,
        stats: Stats? = nil,
        usageDays: [UsageDay]? = nil,
        preferences: Preferences? = nil
    ) {
        self.version = 1
        self.source = source
        self.createdAt = createdAt
        self.podcasts = podcasts
        self.episodes = episodes
        self.queue = queue
        self.lastPlayed = lastPlayed
        self.stats = stats
        self.usageDays = usageDays
        self.preferences = preferences
    }

    var isEmpty: Bool {
        podcasts.isEmpty && episodes.isEmpty && queue.isEmpty && lastPlayed == nil
    }

    /// Plain JSON (not the API coder): dates as reference-date seconds.
    static func encode(_ snapshot: DeviceStateSnapshot) throws -> Data {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        return try encoder.encode(snapshot)
    }

    static func decode(_ data: Data) throws -> DeviceStateSnapshot {
        try JSONDecoder().decode(DeviceStateSnapshot.self, from: data)
    }
}

/// Feed-URL identity tolerant of the rewrites a server might apply on
/// subscribe: scheme (`http`↔`https`), host case, default ports, `www.`,
/// and trailing slashes. Queries are kept (they can select a different
/// feed).
nonisolated enum FeedURLKey {
    static func normalize(_ string: String) -> String {
        let trimmed = string.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var components = URLComponents(string: trimmed), let host = components.host else {
            return trimmed.lowercased()
        }
        var normalizedHost = host.lowercased()
        if normalizedHost.hasPrefix("www.") {
            normalizedHost.removeFirst(4)
        }
        if components.port == 80 || components.port == 443 {
            components.port = nil
        }
        var path = components.path
        while path.hasSuffix("/") {
            path.removeLast()
        }
        let port = components.port.map { ":\($0)" } ?? ""
        let query = components.percentEncodedQuery.map { "?\($0)" } ?? ""
        return normalizedHost + port + path + query
    }
}

/// Minimal OPML writer for re-subscribing legacy feeds on the server.
nonisolated enum OPMLWriter {
    static func document(feeds: [(url: String, title: String?)]) -> Data {
        var lines: [String] = [
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>",
            "<opml version=\"2.0\">",
            "<head><title>Noadcast subscriptions</title></head>",
            "<body>"
        ]
        for feed in feeds {
            let title = escape(feed.title ?? feed.url)
            lines.append("<outline type=\"rss\" text=\"\(title)\" title=\"\(title)\" xmlUrl=\"\(escape(feed.url))\"/>")
        }
        lines.append("</body>")
        lines.append("</opml>")
        return Data(lines.joined(separator: "\n").utf8)
    }

    static func escape(_ text: String) -> String {
        var result = ""
        result.reserveCapacity(text.count)
        for character in text {
            switch character {
            case "&": result += "&amp;"
            case "<": result += "&lt;"
            case ">": result += "&gt;"
            case "\"": result += "&quot;"
            case "'": result += "&apos;"
            default: result.append(character)
            }
        }
        return result
    }
}
