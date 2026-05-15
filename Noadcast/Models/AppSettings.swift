import Foundation
import SwiftData

@Model
final class AppSettings {
    var defaultPlaybackSpeed: Double
    var autoDownloadPolicyRaw: String
    var autoDeleteAfterPlayed: Bool
    var podcastSortModeRaw: String = PodcastSortMode.latestEpisode.rawValue
    var adDetectionPrompt: String

    /// `Episode.guid` of the episode that was loaded into the player when the
    /// app was last foregrounded. Restored on launch so Now Playing comes back
    /// pre-loaded (paused) and ready to resume.
    var lastPlayedEpisodeGUID: String?

    /// Last time `refreshAll` finished. Surfaced on the Podcasts list.
    var lastGlobalRefreshAt: Date?

    /// Lifetime cumulative seconds skipped because an `AdMarker` was hit
    /// during playback. Updated by `PlayerService.maybeSkipAd()`.
    var lifetimeAdSkipSeconds: Double = 0
    /// Lifetime cumulative seconds saved by playing audio above 1×.
    /// Updated by `PlayerService`'s periodic time observer.
    var lifetimeSpeedupSeconds: Double = 0

    /// Two detected ads with this many seconds or fewer between them are
    /// coalesced into a single marker. Helps stitch back-to-back spots in a
    /// single ad break together.
    var adMergeGapSeconds: Int = 5
    /// Ads shorter than this (after merging) are discarded as likely false
    /// positives.
    var adMinDurationSeconds: Int = 10

    init(
        defaultPlaybackSpeed: Double = 1.0,
        autoDownloadPolicy: AutoDownloadPolicy = .wifiOnly,
        autoDeleteAfterPlayed: Bool = true,
        adDetectionPrompt: String = AppSettings.defaultAdDetectionPrompt
    ) {
        self.defaultPlaybackSpeed = defaultPlaybackSpeed
        self.autoDownloadPolicyRaw = autoDownloadPolicy.rawValue
        self.autoDeleteAfterPlayed = autoDeleteAfterPlayed
        self.adDetectionPrompt = adDetectionPrompt
    }

    var autoDownloadPolicy: AutoDownloadPolicy {
        get { AutoDownloadPolicy(rawValue: autoDownloadPolicyRaw) ?? .wifiOnly }
        set { autoDownloadPolicyRaw = newValue.rawValue }
    }

    var podcastSortMode: PodcastSortMode {
        get { PodcastSortMode(rawValue: podcastSortModeRaw) ?? .latestEpisode }
        set { podcastSortModeRaw = newValue.rawValue }
    }

    static let defaultAdDetectionPrompt: String = """
    You are analyzing a podcast transcript to identify advertisement segments. \
    Advertisements include: sponsored messages, host-read ads, promo codes, \
    paid endorsements, and cross-promotion of other podcasts. They do NOT include: \
    editorial mentions, listener mail, the host's own products discussed editorially, \
    or interview segments.

    Given a list of timestamped transcript lines, return the start and end seconds \
    of each contiguous ad segment. Use the timestamps provided. Be conservative — \
    only flag a segment if you are confident it is a paid advertisement. Return an \
    empty list if no ads are present.
    """

    /// Fetches the singleton settings row, creating it if missing.
    static func current(in context: ModelContext) -> AppSettings {
        let descriptor = FetchDescriptor<AppSettings>()
        if let existing = try? context.fetch(descriptor).first {
            return existing
        }
        let new = AppSettings()
        context.insert(new)
        try? context.save()
        return new
    }
}
