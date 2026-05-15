import Foundation
import SwiftData

@Model
final class Podcast {
    @Attribute(.unique) var feedURL: URL
    var title: String
    var author: String?
    var summary: String?
    var artworkURL: URL?
    var dateAdded: Date
    var lastFetched: Date?

    /// Per-podcast playback speed override. `nil` means use the global default.
    var customPlaybackSpeed: Double?

    /// If `true`, new episodes are eligible for auto-download (subject to the
    /// global `AppSettings.autoDownloadPolicy`).
    var autoDownloadEnabled: Bool

    /// If `true`, downloaded episodes are run through transcription + ad
    /// detection and detected ads are skipped during playback. Off lets you
    /// keep a podcast's episodes for normal listening without spending time
    /// or battery on the AI pipeline (e.g. ad-free or short shows).
    var aiProcessingEnabled: Bool = true

    /// Denormalized: the most recent `Episode.publishedAt` for this podcast.
    /// Maintained by `SubscriptionService.importEpisodes` and the launch-time
    /// backfill, so views never have to fault the entire episode relationship
    /// just to sort the podcast list. `nil` means "not yet computed".
    var latestEpisodeAt: Date?

    @Relationship(deleteRule: .cascade, inverse: \Episode.podcast)
    var episodes: [Episode] = []

    init(
        feedURL: URL,
        title: String,
        author: String? = nil,
        summary: String? = nil,
        artworkURL: URL? = nil,
        dateAdded: Date = .now,
        customPlaybackSpeed: Double? = nil,
        autoDownloadEnabled: Bool = true,
        aiProcessingEnabled: Bool = true
    ) {
        self.feedURL = feedURL
        self.title = title
        self.author = author
        self.summary = summary
        self.artworkURL = artworkURL
        self.dateAdded = dateAdded
        self.customPlaybackSpeed = customPlaybackSpeed
        self.autoDownloadEnabled = autoDownloadEnabled
        self.aiProcessingEnabled = aiProcessingEnabled
    }
}
