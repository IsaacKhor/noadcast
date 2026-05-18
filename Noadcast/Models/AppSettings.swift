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

    /// Skip detected mid-episode ads during playback. Defaults on (it's the
    /// whole point of the app). Off lets you hear ads if you want — markers
    /// are still rendered on the timeline / transcript regardless.
    var skipAds: Bool = true
    /// Skip detected intros and outros during playback. Defaults on.
    var skipIntrosAndOutros: Bool = true
    /// When the player skips a segment, it then peeks ahead by this many
    /// seconds for another segment to chain-skip. Set to 0 to disable.
    var chainSkipGapSeconds: Int = 5

    /// Lifetime cumulative input + output tokens billed to the user's API
    /// key(s) for ad-detection (and, when enabled, cloud transcription)
    /// calls. Summed across providers — switching from Gemini to OpenAI
    /// doesn't reset the counter. Updated by `ProcessingPipeline` after
    /// each successful call.
    var lifetimeAdDetectionInputTokens: Int = 0
    var lifetimeAdDetectionOutputTokens: Int = 0

    /// Running cost estimate in USD, accumulated as each call completes
    /// using whatever per-token rate the provider was on at the time. A
    /// historical record, not a re-computation — so changes to the pricing
    /// constants in `AdDetectionProvider` only affect new calls.
    var lifetimeAdDetectionCostUSD: Double = 0

    /// Upload the audio file directly to a cloud model that returns both a
    /// transcript and labelled ad/intro/outro segments in a single response,
    /// bypassing the on-device `SpeechAnalyzer`. Off by default — flip in
    /// Settings to A/B. Only the Gemini providers currently honor this;
    /// OpenAI providers stay on the local-transcription path even when the
    /// toggle is on, because their audio-input pricing/availability is
    /// less favourable.
    var useCloudTranscription: Bool = false

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
