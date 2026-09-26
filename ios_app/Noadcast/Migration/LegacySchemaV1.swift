import Foundation
import SwiftData

/// Frozen copy of the previous build's SwiftData schema (git `HEAD` before
/// the server rewrite: `ios_app/Noadcast/Models/*.swift`), used exactly once
/// to read the old `default.store` and export device-local state before it
/// is moved aside.
///
/// Only what defines the Core Data model matters here: entity (class) names,
/// stored property names, types, optionality, default values, `.unique`
/// attributes, and relationships with their delete rules and inverses. Every
/// one of those is copied verbatim; computed properties and methods are
/// dropped. Enum-backed defaults are spelled as the literal raw values the
/// old enums produced. **Do not edit** — a mismatch makes SwiftData treat the
/// store as a different model and attempt a migration on the only copy of
/// the user's legacy data.
nonisolated enum LegacySchemaV1: VersionedSchema {
    static var versionIdentifier: Schema.Version { Schema.Version(1, 0, 0) }

    static var models: [any PersistentModel.Type] {
        [
            Podcast.self,
            Episode.self,
            AdMarker.self,
            QueueItem.self,
            AppSettings.self,
            UsageHistoryDay.self,
            TokenUsageRecord.self
        ]
    }

    @Model
    final class Podcast {
        @Attribute(.unique) var feedURL: URL
        var title: String
        var author: String?
        var summary: String?
        var artworkURL: URL?
        var dateAdded: Date
        var lastFetched: Date?
        var customPlaybackSpeed: Double?
        var autoDownloadEnabled: Bool
        var aiProcessingEnabled: Bool = true
        var cachedArtworkFilename: String?
        var cachedArtworkSourceURL: URL?
        var latestEpisodeAt: Date?
        var episodeCount: Int = 0

        @Relationship(deleteRule: .cascade, inverse: \LegacySchemaV1.Episode.podcast)
        var episodes: [LegacySchemaV1.Episode] = []

        init(feedURL: URL, title: String, dateAdded: Date = .now, autoDownloadEnabled: Bool = true) {
            self.feedURL = feedURL
            self.title = title
            self.dateAdded = dateAdded
            self.autoDownloadEnabled = autoDownloadEnabled
        }
    }

    @Model
    final class Episode {
        @Attribute(.unique) var guid: String
        var title: String
        var episodeDescription: String?
        var publishedAt: Date?
        var duration: Double?
        var audioURL: URL
        var audioMimeType: String?
        var localFilename: String?
        var fileSizeBytes: Int64?
        var processingStateRaw: String = "new"
        var isInProgress: Bool = false
        var processingError: String?
        var processingProgress: Double
        var processingCurrent: Double?
        var processingTotal: Double?
        var processingStatusText: String?
        var playbackPosition: Double
        var isPlayed: Bool
        var datePlayed: Date?
        var podcast: LegacySchemaV1.Podcast?
        var podcastTitle: String?
        var podcastArtworkURL: URL?
        var podcastCachedArtworkFilename: String?
        var activeAdMarkerCount: Int = 0

        @Relationship(deleteRule: .cascade, inverse: \LegacySchemaV1.AdMarker.episode)
        var adMarkers: [LegacySchemaV1.AdMarker] = []

        init(guid: String, title: String, audioURL: URL) {
            self.guid = guid
            self.title = title
            self.audioURL = audioURL
            self.processingProgress = 0
            self.playbackPosition = 0
            self.isPlayed = false
        }
    }

    @Model
    final class AdMarker {
        var startSeconds: Double
        var endSeconds: Double
        var summary: String
        var manuallyEdited: Bool
        var isDeleted: Bool
        var episode: LegacySchemaV1.Episode?
        var kindRaw: String = "ad"

        init(startSeconds: Double, endSeconds: Double, summary: String) {
            self.startSeconds = startSeconds
            self.endSeconds = endSeconds
            self.summary = summary
            self.manuallyEdited = false
            self.isDeleted = false
        }
    }

    @Model
    final class QueueItem {
        var position: Int
        var addedAt: Date
        var episode: LegacySchemaV1.Episode?

        init(position: Int, addedAt: Date = .now) {
            self.position = position
            self.addedAt = addedAt
        }
    }

    @Model
    final class AppSettings {
        var defaultPlaybackSpeed: Double
        var autoDownloadPolicyRaw: String
        var adAnalysisEnabled: Bool = false
        var autoDeleteAfterPlayed: Bool
        var podcastSortModeRaw: String = "latestEpisode"
        var lastPlayedEpisodeGUID: String?
        var lastGlobalRefreshAt: Date?
        var lifetimeAdSkipSeconds: Double = 0
        var lifetimePlayedSeconds: Double = 0
        var skipAds: Bool = true
        var skipIntrosAndOutros: Bool = true
        var chainSkipGapSeconds: Int = 5
        var lifetimeAdDetectionInputTokens: Int = 0
        var lifetimeAdDetectionThoughtTokens: Int = 0
        var lifetimeAdDetectionOutputTokens: Int = 0
        var lifetimeAdDetectionInputCostUSD: Double = 0
        var lifetimeAdDetectionThoughtCostUSD: Double = 0
        var lifetimeAdDetectionOutputCostUSD: Double = 0
        var lifetimeAdDetectionCostUSD: Double = 0
        var adDetectionBackendRaw: String = "geminiFiles"
        var adDetectionProviderRaw: String = "gemini35Flash"
        var adDetectionThinkingLevelRaw: String = "automatic"
        var downsampleAudioBeforeUpload: Bool = false
        var googleAPIKey: String?
        var openRouterAPIKey: String?
        var adDetectionServerHost: String = "http://127.0.0.1"
        var adDetectionServerPort: Int = 8765

        init() {
            self.defaultPlaybackSpeed = 1.0
            self.autoDownloadPolicyRaw = "wifiOnly"
            self.autoDeleteAfterPlayed = true
        }
    }

    @Model
    final class UsageHistoryDay {
        var dayStart: Date
        var playbackSeconds: Double = 0
        var adSkippedSeconds: Double = 0

        init(dayStart: Date) {
            self.dayStart = dayStart
        }
    }

    @Model
    final class TokenUsageRecord {
        var createdAt: Date
        var providerRaw: String
        var providerLabel: String
        var episodeGUID: String?
        var episodeTitle: String?
        var inputTokens: Int
        var thoughtTokens: Int
        var outputTokens: Int
        var inputCostUSD: Double
        var thoughtCostUSD: Double
        var outputCostUSD: Double

        init(createdAt: Date = .now) {
            self.createdAt = createdAt
            self.providerRaw = ""
            self.providerLabel = ""
            self.inputTokens = 0
            self.thoughtTokens = 0
            self.outputTokens = 0
            self.inputCostUSD = 0
            self.thoughtCostUSD = 0
            self.outputCostUSD = 0
        }
    }
}
