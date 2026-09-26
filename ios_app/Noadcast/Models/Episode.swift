import Foundation
import SwiftData

/// Offline mirror of a server episode plus the device-local state that is
/// never synced (download, playback position, played flag).
///
/// Ownership rule: `SyncEngine` writes only the "server mirror" fields and the
/// denormalized snapshots; everything under "Device-local" is written only by
/// the app (player, download manager, queue). Sync must never clobber it.
@Model
final class Episode {
    /// Server-issued id — the sync reconciliation key. Meaningful only within
    /// one server `instanceId`.
    @Attribute(.unique) var serverID: Int
    /// Denormalized `Podcast.serverID` so per-podcast lists can use a scalar
    /// predicate instead of a relationship join.
    var podcastServerID: Int
    /// RSS `<guid>` (falls back to the enclosure URL). Unique per feed only —
    /// two podcasts may legitimately share a GUID.
    var guid: String

    // MARK: Server mirror

    var title: String
    var episodeDescription: String?
    var publishedAt: Date?
    /// Measured duration once the server has decoded the audio
    /// (`durationIsMeasured`), else the feed's declared duration.
    var duration: Double?
    var durationIsMeasured: Bool = false
    var enclosureURL: URL?
    /// Enclosure MIME type from the feed.
    var audioMimeType: String?

    /// Backs `serverState`. Stored as `String` so `#Predicate` can filter on it.
    var serverStateRaw: String = ServerEpisodeState.discovered.rawValue
    /// Backs `audioState`.
    var audioStateRaw: String = ServerAudioState.absent.rawValue
    var transcriptStateRaw: String = ServerTranscriptState.notStarted.rawValue
    var classifyStateRaw: String = ServerClassifyState.notStarted.rawValue
    var serverError: String?
    /// Size and hash of the exact bytes the server's markers describe.
    var audioBytes: Int64?
    var audioSha256: String?
    var audioContentType: String?
    var markerRevision: Int = 0

    // MARK: Device-local: download to this device

    /// Backs `downloadState`.
    var downloadStateRaw: String = DownloadState.idle.rawValue
    /// User-initiated downloads bypass the auto-download network policy.
    var downloadIsUserInitiated: Bool = false
    var downloadRequestedAt: Date?
    var downloadError: String?
    /// `[0, 1]`, written on a background context by `DownloadManager`.
    /// Only `DownloadsView` rows read it (see `EpisodeRow.showProgress`).
    var downloadProgress: Double = 0
    var downloadedBytes: Int64?
    var downloadTotalBytes: Int64?

    /// Denormalized "server job active OR device download active". A plain
    /// `Bool` keeps the Downloads `#Predicate` translatable to SQL.
    /// Maintained by `applyServerState(_:)` / `setDownloadState(_:)`.
    var isBusy: Bool = false

    // MARK: Device-local: file and playback

    /// Filename inside `AudioStorage.episodesDirectory`, once downloaded.
    /// `nil` means not on disk.
    var localFilename: String?
    var fileSizeBytes: Int64?
    /// SHA-256 of the local file as reported by the server when it was
    /// downloaded (the audio `ETag`), or the server's hash when a legacy
    /// file was adopted after a size match.
    var localAudioSha256: String?

    /// Last playback position in seconds.
    var playbackPosition: Double
    var isPlayed: Bool
    var datePlayed: Date?

    var podcast: Podcast?

    /// Denormalized podcast fields used by episode rows in cross-podcast
    /// lists. SwiftUI creates/destroys rows while scrolling; reading these
    /// scalars avoids faulting the `podcast` relationship just to draw title
    /// text or artwork.
    var podcastTitle: String?
    var podcastArtworkURL: URL?
    var podcastCachedArtworkFilename: String?

    /// Denormalized count of active ad/intro/outro markers. Rows can show
    /// the badge without faulting the `adMarkers` relationship.
    var activeAdMarkerCount: Int = 0

    @Relationship(deleteRule: .cascade, inverse: \AdMarker.episode)
    var adMarkers: [AdMarker] = []

    init(
        serverID: Int,
        podcastServerID: Int,
        guid: String,
        title: String,
        episodeDescription: String? = nil,
        publishedAt: Date? = nil,
        duration: Double? = nil,
        enclosureURL: URL? = nil,
        audioMimeType: String? = nil,
        podcast: Podcast? = nil
    ) {
        self.serverID = serverID
        self.podcastServerID = podcastServerID
        self.guid = guid
        self.title = title
        self.episodeDescription = episodeDescription
        self.publishedAt = publishedAt
        self.duration = duration
        self.enclosureURL = enclosureURL
        self.audioMimeType = audioMimeType
        self.podcast = podcast
        self.podcastTitle = podcast?.title
        self.podcastArtworkURL = podcast?.artworkURL
        self.podcastCachedArtworkFilename = podcast?.cachedArtworkFilename
        self.playbackPosition = 0
        self.isPlayed = false
    }

    // MARK: - Typed accessors

    var serverState: ServerEpisodeState { ServerEpisodeState(raw: serverStateRaw) }
    var audioState: ServerAudioState { ServerAudioState(raw: audioStateRaw) }
    var downloadState: DownloadState { DownloadState(rawValue: downloadStateRaw) ?? .idle }
    var classifyState: ServerClassifyState {
        ServerClassifyState(rawValue: classifyStateRaw) ?? .unknown
    }

    /// Write-if-changed: SwiftData dirties a model on *any* assignment, and a
    /// dirty row invalidates every `@Query` that contains it.
    func applyServerState(_ raw: String) {
        if serverStateRaw != raw {
            serverStateRaw = raw
        }
        refreshBusyFlag()
    }

    func setDownloadState(_ state: DownloadState) {
        if downloadStateRaw != state.rawValue {
            downloadStateRaw = state.rawValue
        }
        refreshBusyFlag()
    }

    func refreshBusyFlag() {
        let busy = serverState.isActive || downloadState.isActive
        if isBusy != busy {
            isBusy = busy
        }
    }

    // MARK: - Files

    var localFileURL: URL? {
        guard let localFilename else { return nil }
        return AudioStorage.fileURL(for: localFilename)
    }

    var hasLocalFile: Bool {
        guard let url = localFileURL else { return false }
        return FileManager.default.fileExists(atPath: url.path)
    }

    /// UI-only download flag. Unlike `hasLocalFile`, this does not touch the
    /// filesystem from a row body.
    var isMarkedDownloaded: Bool {
        localFilename != nil
    }

    /// `false` when the local file is known to be a different render of the
    /// episode than the one the server's markers were computed against
    /// (dynamic ad insertion). Markers must not be applied to such a file.
    var localFileMatchesServerAudio: Bool {
        if let local = localAudioSha256, let server = audioSha256 {
            return local.caseInsensitiveCompare(server) == .orderedSame
        }
        if let size = fileSizeBytes, let serverSize = audioBytes {
            return size == serverSize
        }
        return true
    }

    var podcastArtworkDisplayURL: URL? {
        if let filename = podcastCachedArtworkFilename {
            return ArtworkService.localURL(filename: filename)
        }
        return podcastArtworkURL
    }

    func syncPodcastSnapshot(from podcast: Podcast) {
        if podcastTitle != podcast.title {
            podcastTitle = podcast.title
        }
        if podcastArtworkURL != podcast.artworkURL {
            podcastArtworkURL = podcast.artworkURL
        }
        if podcastCachedArtworkFilename != podcast.cachedArtworkFilename {
            podcastCachedArtworkFilename = podcast.cachedArtworkFilename
        }
    }

    static var episodesDirectory: URL {
        AudioStorage.episodesDirectory
    }
}
