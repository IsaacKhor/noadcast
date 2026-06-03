import Foundation

nonisolated struct DetectedAd: Sendable {
    let startSeconds: Double
    let endSeconds: Double
    let summary: String
    let kind: SegmentKind
    var duration: Double { endSeconds - startSeconds }

    func sanitized(episodeDuration: Double?) -> DetectedAd? {
        guard let range = AdTimestampSanitizer.sanitizedRange(
            startSeconds: startSeconds,
            endSeconds: endSeconds,
            episodeDuration: episodeDuration
        ) else {
            return nil
        }
        return DetectedAd(
            startSeconds: range.startSeconds,
            endSeconds: range.endSeconds,
            summary: summary,
            kind: kind
        )
    }
}

nonisolated enum AdTimestampSanitizer {
    static func sanitizedRange(
        startSeconds: Double,
        endSeconds: Double,
        episodeDuration: Double?
    ) -> (startSeconds: Double, endSeconds: Double)? {
        guard startSeconds.isFinite, endSeconds.isFinite, endSeconds > startSeconds else {
            return nil
        }

        let knownDuration = episodeDuration.flatMap { duration -> Double? in
            guard duration.isFinite, duration > 0 else { return nil }
            return duration
        }

        if let knownDuration {
            guard endSeconds > 0, startSeconds < knownDuration else { return nil }
            let start = min(max(0, startSeconds), knownDuration)
            let end = min(max(0, endSeconds), knownDuration)
            guard end > start else { return nil }
            return (start, end)
        }

        let start = max(0, startSeconds)
        let end = max(0, endSeconds)
        guard end > start else { return nil }
        return (start, end)
    }
}

/// Token usage reported by the provider for one audio-analysis call.
nonisolated struct TokenUsage: Sendable {
    var inputTokens: Int
    var thoughtTokens: Int
    var outputTokens: Int

    init(inputTokens: Int, thoughtTokens: Int = 0, outputTokens: Int) {
        self.inputTokens = inputTokens
        self.thoughtTokens = thoughtTokens
        self.outputTokens = outputTokens
    }
}
