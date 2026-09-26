import Foundation
import SwiftData

/// Offline mirror of a server podcast plus device-local preferences.
///
/// Server mirror (written only by `SyncEngine`): feed metadata, fetch status,
/// `adAnalysisEnabled`, `autoProcessEnabled`, `latestEpisodeAt`,
/// `episodeCount`. Device-local (never synced): `autoDownloadEnabled`,
/// `customPlaybackSpeed`, the artwork cache.
@Model
final class Podcast {
    /// Server-issued id — the sync reconciliation key.
    @Attribute(.unique) var serverID: Int
    /// Not unique locally: uniqueness is the server's job, and re-attaching
    /// device-local state after a server rebuild matches on it.
    var feedURL: URL
    var title: String
    var author: String?
    var summary: String?
    var artworkURL: URL?
    /// Server `createdAt` (when it was subscribed).
    var dateAdded: Date
    /// Server `lastFetchAt`.
    var lastFetched: Date?
    var lastFetchError: String?

    /// Per-podcast playback speed override. `nil` means use the global default.
    var customPlaybackSpeed: Double?

    /// Device-local: if `true`, newly published episodes join the Queue (and
    /// are then downloaded under `AppSettings.autoDownloadPolicy`).
    var autoDownloadEnabled: Bool

    /// Server mirror: processed episodes are transcribed and classified. The
    /// global `AppSettings.adAnalysisEnabled` switch can still disable
    /// analysis for every podcast at once. Changed through
    /// `PATCH /api/v1/podcasts/{id}` (optimistically).
    var adAnalysisEnabled: Bool = true

    /// Server mirror: new episodes are downloaded and processed on the server
    /// automatically.
    var autoProcessEnabled: Bool = true

    /// Filename (under `ArtworkService.artworkDirectory`) of the locally
    /// cached artwork. `nil` means no cache yet or download failed.
    var cachedArtworkFilename: String?

    /// The remote URL the cached file was downloaded from. Used by the cache
    /// to decide whether the artwork has changed and needs a re-download.
    var cachedArtworkSourceURL: URL?

    /// Server mirror: the most recent episode `publishedAt`, so views never
    /// fault the episode relationship just to sort the podcast list.
    var latestEpisodeAt: Date?

    /// Server mirror: episode count for podcast rows.
    var episodeCount: Int = 0

    /// Device-local bookkeeping: when this row was first mirrored. Only
    /// episodes published after it are auto-queued, so subscribing (or
    /// rebuilding the mirror) never floods the Queue with the archive.
    var firstSyncedAt: Date = Date.distantPast

    @Relationship(deleteRule: .cascade, inverse: \Episode.podcast)
    var episodes: [Episode] = []

    init(
        serverID: Int,
        feedURL: URL,
        title: String,
        author: String? = nil,
        summary: String? = nil,
        artworkURL: URL? = nil,
        dateAdded: Date = .now,
        customPlaybackSpeed: Double? = nil,
        autoDownloadEnabled: Bool = true,
        adAnalysisEnabled: Bool = true,
        autoProcessEnabled: Bool = true,
        firstSyncedAt: Date = .now
    ) {
        self.serverID = serverID
        self.feedURL = feedURL
        self.title = title
        self.author = author
        self.summary = summary
        self.artworkURL = artworkURL
        self.dateAdded = dateAdded
        self.customPlaybackSpeed = customPlaybackSpeed
        self.autoDownloadEnabled = autoDownloadEnabled
        self.adAnalysisEnabled = adAnalysisEnabled
        self.autoProcessEnabled = autoProcessEnabled
        self.firstSyncedAt = firstSyncedAt
    }

    /// The URL views should pass to `AsyncImage`. Prefers the locally
    /// cached file (no network hit, even on cold launch) and falls back to
    /// the remote URL if no cache exists yet.
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

    /// Pushes title/artwork into every episode's denormalized snapshot.
    /// Write-if-changed per field (see `Episode.syncPodcastSnapshot`).
    func syncEpisodeSnapshots() {
        for episode in episodes {
            episode.syncPodcastSnapshot(from: self)
        }
    }
}
