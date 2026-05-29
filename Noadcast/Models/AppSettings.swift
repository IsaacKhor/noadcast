import Foundation
import SwiftData

nonisolated enum AdDetectionMode: String, Codable, CaseIterable, Sendable {
    case apiAdTimestampsOnly

    var label: String {
        switch self {
        case .apiAdTimestampsOnly:
            "API ad timestamps only"
        }
    }

    var requiresAudioUpload: Bool {
        true
    }

    var storesTranscript: Bool {
        false
    }

    func isSupported(by provider: AdDetectionProvider) -> Bool {
        !requiresAudioUpload || provider.supportsCloudTranscription
    }
}

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

    /// Skip detected mid-episode ads during playback. Defaults on (it's the
    /// whole point of the app). Off lets you hear ads if you want — markers
    /// are still rendered on the timeline and in the skip-segments sheet.
    var skipAds: Bool = true
    /// Skip detected intros and outros during playback. Defaults on.
    var skipIntrosAndOutros: Bool = true
    /// When the player skips a segment, it then peeks ahead by this many
    /// seconds for another segment to chain-skip. Set to 0 to disable.
    var chainSkipGapSeconds: Int = 5

    /// Lifetime cumulative input/thought/output tokens billed to the user's API
    /// key(s) for ad-detection (and, when enabled, cloud transcription)
    /// calls. Summed across models so changing providers doesn't reset the
    /// counter. Updated by `ProcessingPipeline` after each successful call.
    var lifetimeAdDetectionInputTokens: Int = 0
    var lifetimeAdDetectionThoughtTokens: Int = 0
    var lifetimeAdDetectionOutputTokens: Int = 0

    /// Running cost estimates in USD, accumulated as each call completes
    /// using whatever per-token rates the provider was on at the time. These
    /// are historical records, not re-computations — so changes to the
    /// pricing constants in `AdDetectionProvider` only affect new calls.
    var lifetimeAdDetectionInputCostUSD: Double = 0
    var lifetimeAdDetectionThoughtCostUSD: Double = 0
    var lifetimeAdDetectionOutputCostUSD: Double = 0
    /// Legacy combined total retained for older local stores and any future
    /// migration/debugging needs.
    var lifetimeAdDetectionCostUSD: Double = 0

    /// Legacy setting retained for backwards compatibility with older local
    /// stores. The app now always uses file upload plus segments-only output.
    var useCloudTranscription: Bool = false
    /// Legacy setting retained for backwards compatibility with older local
    /// stores. The app now always uses file upload plus segments-only output.
    var adDetectionModeRaw: String = AdDetectionMode.apiAdTimestampsOnly.rawValue

    /// Which cloud model performs ad detection. See `AdDetectionProvider`.
    var adDetectionProviderRaw: String = AdDetectionProvider.gemini35Flash.rawValue
    /// Optional thinking level for Gemini models whose API accepts
    /// `thinkingConfig.thinkingLevel`. Unsupported models ignore this.
    var adDetectionThinkingLevelRaw: String = AdDetectionThinkingLevel.automatic.rawValue
    /// If enabled, uploads a temporary low-bitrate copy to Gemini instead of
    /// the local playback file.
    var downsampleAudioBeforeUpload: Bool = false
    /// API key for Google AI Studio (Gemini providers). Stored unencrypted in
    /// the app's SwiftData store — fine for a personal-use app; move to
    /// Keychain if this ever ships to multiple users.
    var googleAPIKey: String?

    var adDetectionProvider: AdDetectionProvider {
        get { AdDetectionProvider(rawValue: adDetectionProviderRaw) ?? .gemini35Flash }
        set { adDetectionProviderRaw = newValue.rawValue }
    }

    var adDetectionThinkingLevel: AdDetectionThinkingLevel {
        get { AdDetectionThinkingLevel(rawValue: adDetectionThinkingLevelRaw) ?? .automatic }
        set { adDetectionThinkingLevelRaw = newValue.rawValue }
    }

    var adDetectionMode: AdDetectionMode {
        get { .apiAdTimestampsOnly }
        set {
            adDetectionModeRaw = newValue.rawValue
            useCloudTranscription = newValue.requiresAudioUpload
        }
    }

    func resolvedAdDetectionMode(for provider: AdDetectionProvider? = nil) -> AdDetectionMode {
        let provider = provider ?? adDetectionProvider
        let selected = AdDetectionMode.apiAdTimestampsOnly
        if selected.isSupported(by: provider) {
            return selected
        }
        return .apiAdTimestampsOnly
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

    func resetAdDetectionUsageStatistics() {
        lifetimeAdDetectionInputTokens = 0
        lifetimeAdDetectionThoughtTokens = 0
        lifetimeAdDetectionOutputTokens = 0
        lifetimeAdDetectionInputCostUSD = 0
        lifetimeAdDetectionThoughtCostUSD = 0
        lifetimeAdDetectionOutputCostUSD = 0
        lifetimeAdDetectionCostUSD = 0
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
