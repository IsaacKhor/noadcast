import Foundation
import SwiftData

/// Singleton row of device preferences, lifetime listening stats, and a
/// small mirror of the server's global settings.
///
/// Every write to this row invalidates every live `@Query<AppSettings>`, so
/// sync writes to the mirrored fields are write-if-changed, and the server
/// address/token live outside SwiftData (`APIConfiguration`).
@Model
final class AppSettings {
    var defaultPlaybackSpeed: Double
    var autoDownloadPolicyRaw: String
    /// Server mirror of the global ad-analysis switch (`GET /settings`).
    /// Changed through an optimistic `PATCH /api/v1/settings`. When off, the
    /// server skips analysis even if an individual podcast's setting is on.
    var adAnalysisEnabled: Bool = true
    /// Retained for store compatibility. Played audio is always removed now.
    var autoDeleteAfterPlayed: Bool
    var podcastSortModeRaw: String = PodcastSortMode.latestEpisode.rawValue

    /// `Episode.serverID` of the episode that was loaded into the player when
    /// the app was last foregrounded. Restored on launch so Now Playing comes
    /// back pre-loaded (paused) and ready to resume.
    var lastPlayedEpisodeServerID: Int?

    /// Last time a pull-to-refresh of every feed finished. Surfaced on the
    /// Podcasts list.
    var lastGlobalRefreshAt: Date?

    /// Lifetime cumulative seconds skipped because an `AdMarker` was hit
    /// during playback. Updated by `PlayerService`.
    var lifetimeAdSkipSeconds: Double = 0
    /// Lifetime cumulative audio-seconds actually played back (counts both
    /// content and the parts of ads that played before being skipped).
    var lifetimePlayedSeconds: Double = 0

    /// Skip detected mid-episode ads during playback. Off lets you hear ads
    /// if you want — markers are still rendered on the timeline and in the
    /// skip-segments sheet.
    var skipAds: Bool = true
    /// Skip detected intros and outros during playback.
    var skipIntrosAndOutros: Bool = true
    /// When the player skips a segment, it then peeks ahead by this many
    /// seconds for another segment to chain-skip. Set to 0 to disable.
    var chainSkipGapSeconds: Int = 5

    /// Server mirror, changed through the settings API.
    var serverAutoProcessEnabled: Bool = true
    var serverClassifier: String?
    var serverClassifierModel: String?
    /// Unknown until a server reports its OpenRouter key availability.
    var serverOpenRouterAvailable: Bool?

    init(
        defaultPlaybackSpeed: Double = 1.0,
        autoDownloadPolicy: AutoDownloadPolicy = .wifiOnly,
        adAnalysisEnabled: Bool = true,
        autoDeleteAfterPlayed: Bool = true
    ) {
        self.defaultPlaybackSpeed = defaultPlaybackSpeed
        self.autoDownloadPolicyRaw = autoDownloadPolicy.rawValue
        self.adAnalysisEnabled = adAnalysisEnabled
        self.autoDeleteAfterPlayed = autoDeleteAfterPlayed
    }

    var autoDownloadPolicy: AutoDownloadPolicy {
        get { AutoDownloadPolicy(rawValue: autoDownloadPolicyRaw) ?? .wifiOnly }
        set { autoDownloadPolicyRaw = newValue.rawValue }
    }

    var podcastSortMode: PodcastSortMode {
        get { PodcastSortMode(rawValue: podcastSortModeRaw) ?? .latestEpisode }
        set { podcastSortModeRaw = newValue.rawValue }
    }

    func resetPlaybackHistoryStatistics() {
        lifetimePlayedSeconds = 0
        lifetimeAdSkipSeconds = 0
    }

    /// Fetches the singleton settings row, creating it if missing. Also
    /// collapses accidental duplicates (two contexts racing the insert).
    static func current(in context: ModelContext) -> AppSettings {
        let existing = (try? context.fetch(FetchDescriptor<AppSettings>())) ?? []
        if let first = existing.first {
            if existing.count > 1 {
                for duplicate in existing.dropFirst() {
                    context.delete(duplicate)
                }
                try? context.save()
            }
            return first
        }
        let new = AppSettings()
        context.insert(new)
        try? context.save()
        return new
    }
}
