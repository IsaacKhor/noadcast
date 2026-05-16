import Foundation
import SwiftData

@Model
final class AppSettings {
    var defaultPlaybackSpeed: Double
    var autoDownloadPolicyRaw: String
    var autoDeleteAfterPlayed: Bool
    var podcastSortModeRaw: String = PodcastSortMode.latestEpisode.rawValue

    /// `Episode.guid` of the episode that was loaded into the player when the
    /// app was last foregrounded. Restored on launch so Now Playing comes back
    /// pre-loaded (paused) and ready to resume.
    var lastPlayedEpisodeGUID: String?

    /// Last time `refreshAll` finished. Surfaced on the Podcasts list.
    var lastGlobalRefreshAt: Date?

    /// Lifetime cumulative seconds skipped because an `AdMarker` was hit
    /// during playback. Updated by `PlayerService.maybeSkipAd()`.
    var lifetimeAdSkipSeconds: Double = 0
    /// Lifetime cumulative audio-seconds actually played back (counts both
    /// content and the parts of ads that played before being skipped).
    /// Sum with `lifetimeAdSkipSeconds` to get the total audio "consumed".
    var lifetimePlayedSeconds: Double = 0

    /// Which cloud model performs ad detection. See `AdDetectionProvider`.
    var adDetectionProviderRaw: String = AdDetectionProvider.geminiFlashLite.rawValue
    /// API key for Google AI Studio (Gemini providers). Stored unencrypted in
    /// the app's SwiftData store — fine for a personal-use app; move to
    /// Keychain if this ever ships to multiple users.
    var googleAPIKey: String?
    /// API key for OpenAI. Same caveat re: storage as `googleAPIKey`.
    var openAIAPIKey: String?

    var adDetectionProvider: AdDetectionProvider {
        get { AdDetectionProvider(rawValue: adDetectionProviderRaw) ?? .geminiFlashLite }
        set { adDetectionProviderRaw = newValue.rawValue }
    }

    init(
        defaultPlaybackSpeed: Double = 1.0,
        autoDownloadPolicy: AutoDownloadPolicy = .wifiOnly,
        autoDeleteAfterPlayed: Bool = true
    ) {
        self.defaultPlaybackSpeed = defaultPlaybackSpeed
        self.autoDownloadPolicyRaw = autoDownloadPolicy.rawValue
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
