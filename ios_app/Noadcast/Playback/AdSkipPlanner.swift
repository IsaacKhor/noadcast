import Foundation

/// Lightweight snapshot of an episode's skip-segment metadata, captured at
/// the time playback starts so the audio loop doesn't need to talk to
/// SwiftData. `kind` is consulted when deciding whether the user's per-kind
/// toggle says to skip this one.
nonisolated struct AdRegion: Sendable, Equatable {
    let startSeconds: Double
    let endSeconds: Double
    let kind: SegmentKind

    static func sanitized(
        startSeconds: Double,
        endSeconds: Double,
        kind: SegmentKind,
        episodeDuration: Double?
    ) -> AdRegion? {
        guard let range = AdTimestampSanitizer.sanitizedRange(
            startSeconds: startSeconds,
            endSeconds: endSeconds,
            episodeDuration: episodeDuration
        ) else {
            return nil
        }
        return AdRegion(
            startSeconds: range.startSeconds,
            endSeconds: range.endSeconds,
            kind: kind
        )
    }
}

/// The ad-skip decision, extracted from `PlayerService.maybeSkipAd()` so it
/// can be tested without AVFoundation. The chain-skip walk and the
/// end-of-episode branch are the previous build's logic unchanged; what is
/// new is the loop guard:
///
/// * `pendingSkipTarget` — while a skip seek is in flight the next 0.25 s
///   tick must not re-enter the same region (it is still "inside" it until
///   the seek lands). Cleared by `skipSeekCompleted()`.
/// * a per-region attempt cap — on a remote VBR stream a seek can land
///   *before* the target; the region is retried at most
///   `maxAttemptsPerRegion` times, then left to play, instead of looping
///   forever (and inflating the skipped-seconds counters).
///
/// Seeks themselves use `toleranceBefore: .zero` (see `PlayerService`).
nonisolated struct AdSkipPlanner: Sendable {
    static let maxAttemptsPerRegion = 3
    /// Seek this far past a region's end so the landing is outside it.
    static let landingOffset: Double = 0.05
    /// A seek whose completion never arrives (item replaced mid-seek) must
    /// not disable skipping forever.
    static let pendingSkipTimeout: TimeInterval = 8

    nonisolated enum Decision: Equatable, Sendable {
        case none
        case seek(target: Double, regionsSkipped: Int, savedSeconds: Double)
        /// The skip would land at or past the end: finish the episode.
        case finish(regionsSkipped: Int, savedSeconds: Double)
    }

    private(set) var pendingSkipTarget: Double?
    private var pendingSince: TimeInterval?
    private var attempts: [String: Int] = [:]

    init() {}

    /// Region identity for the attempt cap (millisecond bounds + kind).
    static func key(for region: AdRegion) -> String {
        let start = Int64((region.startSeconds * 1_000).rounded())
        let end = Int64((region.endSeconds * 1_000).rounded())
        return "\(start)-\(end)-\(region.kind.rawValue)"
    }

    func attemptCount(for region: AdRegion) -> Int {
        attempts[Self.key(for: region)] ?? 0
    }

    mutating func decide(
        currentTime: Double,
        duration: Double,
        regions: [AdRegion],
        chainSkipGapSeconds: Double,
        skipsAds: Bool,
        skipsIntrosAndOutros: Bool,
        now: TimeInterval
    ) -> Decision {
        if pendingSkipTarget != nil {
            if let pendingSince, now - pendingSince > Self.pendingSkipTimeout {
                pendingSkipTarget = nil
                self.pendingSince = nil
            } else {
                return .none
            }
        }

        func shouldSkip(_ kind: SegmentKind) -> Bool {
            switch kind {
            case .ad: skipsAds
            case .intro, .outro: skipsIntrosAndOutros
            }
        }

        // Find the region we're currently inside, and only act if the user
        // wants this kind skipped.
        guard let initial = regions.first(where: {
            $0.startSeconds <= currentTime && currentTime < $0.endSeconds && shouldSkip($0.kind)
        })
        else { return .none }

        let key = Self.key(for: initial)
        let previousAttempts = attempts[key] ?? 0
        guard previousAttempts < Self.maxAttemptsPerRegion else { return .none }
        attempts[key] = previousAttempts + 1

        // Chain-skip: walk forward through additional regions whose start
        // is within `chainSkipGapSeconds` of the previous region's end and
        // that the user also wants skipped. The seek jumps past all of
        // them in one go.
        var targetEnd = initial.endSeconds
        var skipped = 1
        while let next = regions.first(where: { region in
            region.endSeconds > targetEnd
                && region.startSeconds - targetEnd <= max(0, chainSkipGapSeconds)
                && shouldSkip(region.kind)
        }) {
            targetEnd = max(targetEnd, next.endSeconds)
            skipped += 1
        }

        let effectiveTargetEnd = duration > 0 ? min(targetEnd, duration) : targetEnd
        let saved = max(0, effectiveTargetEnd - currentTime)
        let skipTarget = targetEnd + Self.landingOffset
        if duration > 0, skipTarget >= duration {
            return .finish(regionsSkipped: skipped, savedSeconds: saved)
        }
        pendingSkipTarget = skipTarget
        pendingSince = now
        return .seek(target: skipTarget, regionsSkipped: skipped, savedSeconds: saved)
    }

    /// Call from the skip seek's completion handler (finished or not).
    mutating func skipSeekCompleted() {
        pendingSkipTarget = nil
        pendingSince = nil
    }

    /// Call on a new item, a user seek, or when the region set changes: the
    /// user may deliberately seek back into a segment, and it should skip
    /// again.
    mutating func reset() {
        pendingSkipTarget = nil
        pendingSince = nil
        attempts.removeAll()
    }
}
