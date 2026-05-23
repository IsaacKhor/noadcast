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

    /// Filename (under `ArtworkService.artworkDirectory`) of the locally
    /// cached artwork. Populated by `ArtworkService.cache(for:)` on subscribe
    /// and refresh; `nil` means no cache yet or download failed.
    var cachedArtworkFilename: String?

    /// The remote URL the cached file was downloaded from. Used by the cache
    /// to decide whether the artwork has changed on the server and needs a
    /// re-download.
    var cachedArtworkSourceURL: URL?

    /// Denormalized: the most recent `Episode.publishedAt` for this podcast.
    /// Maintained by `SubscriptionService.importEpisodes` and the launch-time
    /// backfill, so views never have to fault the entire episode relationship
    /// just to sort the podcast list. `nil` means "not yet computed".
    var latestEpisodeAt: Date?

    /// Denormalized episode count for podcast rows. Reading
    /// `episodes.count` in a row body faults the relationship while
    /// scrolling, so services keep this scalar fresh instead.
    var episodeCount: Int = 0

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

    /// The URL views should pass to `AsyncImage`. Prefers the locally
    /// cached file (no network hit, even on cold launch) and falls back to
    /// the remote URL if no cache exists yet (e.g. between adding a podcast
    /// and the first refresh finishing).
    var artworkDisplayURL: URL? {
        if let filename = cachedArtworkFilename {
            let local = ArtworkService.localURL(filename: filename)
            if FileManager.default.fileExists(atPath: local.path) {
                return local
            }
        }
        return artworkURL
    }

    /// UI-only artwork URL. Avoids filesystem checks from row bodies; service
    /// code is responsible for keeping cached artwork filenames valid.
    var cachedArtworkDisplayURL: URL? {
        if let filename = cachedArtworkFilename {
            return ArtworkService.localURL(filename: filename)
        }
        return artworkURL
    }

    func syncEpisodeSnapshots() {
        episodeCount = episodes.count
        for episode in episodes {
            episode.syncPodcastSnapshot(from: self)
        }
    }
}
