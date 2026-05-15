import Foundation
import FoundationModels
import os

nonisolated struct DetectedAd: Sendable {
    let startSeconds: Double
    let endSeconds: Double
    let summary: String
    var duration: Double { endSeconds - startSeconds }
}

/// User-tunable post-processing applied to the raw LLM output before the
/// markers are written to SwiftData.
nonisolated struct AdMergePolicy: Sendable {
    /// Two ads with a gap less than or equal to this are merged into one.
    let mergeGapSeconds: Double
    /// Ads shorter than this (after merging) are discarded.
    let minDurationSeconds: Double

    static let `default` = AdMergePolicy(mergeGapSeconds: 15, minDurationSeconds: 10)
}

enum AdDetectionError: LocalizedError {
    case modelUnavailable(String)
    case generationFailure(Error)

    var errorDescription: String? {
        switch self {
        case .modelUnavailable(let reason): "Ad detection unavailable: \(reason)"
        case .generationFailure(let err): "Ad detection failed: \(err.localizedDescription)"
        }
    }
}

@Generable
struct LLMAdSegment: Sendable {
    @Guide(description: "Start time in seconds of the ad, taken from the transcript timestamps.")
    let startSeconds: Double
    @Guide(description: "End time in seconds of the ad, taken from the transcript timestamps.")
    let endSeconds: Double
    @Guide(description: "Brief description of what is being advertised, 1 short sentence.")
    let summary: String
}

@Generable
struct LLMAdSegments: Sendable {
    @Guide(description: "All advertisement segments found in this transcript chunk. Empty if none.")
    let segments: [LLMAdSegment]
}

/// Uses on-device FoundationModels to identify ad segments in a transcript.
actor AdDetectionService {
    static let shared = AdDetectionService()

    /// Chunked transcript size, in transcript-segments, sent to the model per
    /// LLM call. Tuned to stay comfortably within the on-device context window
    /// while preserving enough context for the model to recognise multi-line
    /// host-read ads.
    private static let chunkSize = 60
    private static let chunkOverlap = 6

    func detectAds(
        in transcript: [TranscribedSegment],
        instructions: String,
        mergePolicy: AdMergePolicy = .default,
        progress: (@Sendable (Int, Int) -> Void)? = nil
    ) async throws -> [DetectedAd] {
        guard !transcript.isEmpty else { return [] }

        let availability = SystemLanguageModel.default.availability
        guard case .available = availability else {
            let reason = Self.describe(availability)
            Log.adDetection.error("SystemLanguageModel unavailable: \(reason, privacy: .public)")
            throw AdDetectionError.modelUnavailable(reason)
        }

        let stride = Self.chunkSize - Self.chunkOverlap
        let totalChunks = max(1, Int(ceil(Double(max(0, transcript.count - Self.chunkOverlap)) / Double(stride))))
        Log.adDetection.info("Begin ad detection — segments=\(transcript.count) chunks=\(totalChunks)")

        var collected: [DetectedAd] = []
        var skippedChunks = 0
        var index = 0
        var chunkIndex = 0
        progress?(0, totalChunks)
        while index < transcript.count {
            try Task.checkCancellation()
            let end = min(index + Self.chunkSize, transcript.count)
            let chunk = Array(transcript[index..<end])
            do {
                let ads = try await detectAdsInChunk(
                    chunk,
                    chunkIndex: chunkIndex,
                    instructions: instructions
                )
                collected.append(contentsOf: ads)
            } catch is CancellationError {
                throw CancellationError()
            } catch {
                // Per-chunk failures (guardrail violations, decoding errors,
                // rate limits, etc.) are treated as "no ads in this chunk"
                // and we keep going. The error itself was already logged in
                // detail by `detectAdsInChunk`.
                skippedChunks += 1
            }
            chunkIndex += 1
            progress?(chunkIndex, totalChunks)
            if end == transcript.count { break }
            index = end - Self.chunkOverlap
        }

        let merged = Self.mergeOverlapping(collected, gapSeconds: mergePolicy.mergeGapSeconds)
        let filtered = merged.filter { $0.duration >= mergePolicy.minDurationSeconds }
        Log.adDetection.info("""
            Ad detection complete — \
            raw_ads=\(collected.count) merged=\(merged.count) \
            after_min_duration=\(filtered.count) \
            skipped_chunks=\(skippedChunks) \
            policy_gap=\(mergePolicy.mergeGapSeconds)s policy_min=\(mergePolicy.minDurationSeconds)s
            """)
        return filtered
    }

    private func detectAdsInChunk(
        _ chunk: [TranscribedSegment],
        chunkIndex: Int,
        instructions: String
    ) async throws -> [DetectedAd] {
        let session = LanguageModelSession(instructions: instructions)

        let lines = chunk.map { seg in
            String(format: "[%.1f-%.1f] %@", seg.startSeconds, seg.endSeconds, seg.text)
        }.joined(separator: "\n")

        let prompt = """
        Transcript chunk (start-seconds-end-seconds in brackets):

        \(lines)

        Identify advertisement segments. Return start/end seconds using only the \
        timestamps that appear in this chunk. Return an empty list if none.
        """

        let timeRange = String(format: "%.1fs–%.1fs",
                               chunk.first?.startSeconds ?? 0,
                               chunk.last?.endSeconds ?? 0)
        Log.adDetection.debug("Chunk \(chunkIndex) → LLM (\(chunk.count) segs, \(timeRange, privacy: .public))")
        logTranscript(chunk: chunk, chunkIndex: chunkIndex)

        do {
            let response = try await session.respond(to: prompt, generating: LLMAdSegments.self)
            let detected: [DetectedAd] = response.content.segments.compactMap { seg in
                guard seg.endSeconds > seg.startSeconds else { return nil }
                return DetectedAd(
                    startSeconds: seg.startSeconds,
                    endSeconds: seg.endSeconds,
                    summary: seg.summary
                )
            }
            Log.adDetection.debug("Chunk \(chunkIndex) returned \(detected.count) ad(s)")
            logDetectedAds(detected, chunk: chunk, chunkIndex: chunkIndex)
            return detected
        } catch is CancellationError {
            // Don't wrap cancellation — the caller relies on `is CancellationError`
            // to distinguish "user cancelled the pipeline" from "LLM rejected
            // this chunk".
            throw CancellationError()
        } catch {
            // Log everything we can about the failure so the user can see
            // exactly why FoundationModels rejected this chunk — especially
            // useful for the "detected content likely to be unsafe"
            // (guardrailViolation) case.
            let preview = String(lines.prefix(800))
            Log.adDetection.error("""
                LLM rejection in chunk \(chunkIndex) (\(timeRange, privacy: .public)) — chunk will be treated as ad-free
                \(Log.describe(error), privacy: .public)
                chunk_preview=\(preview, privacy: .public)
                """)
            throw AdDetectionError.generationFailure(error)
        }
    }

    /// Lowest-level trace: every transcript segment going into the model for
    /// this chunk. Visible at `.debug` (Console.app: enable Debug messages or
    /// run from Xcode).
    private func logTranscript(chunk: [TranscribedSegment], chunkIndex: Int) {
        for (offset, seg) in chunk.enumerated() {
            Log.adDetection.debug("""
                seg chunk=\(chunkIndex) i=\(offset) \
                t=\(String(format: "%.2f", seg.startSeconds), privacy: .public)s–\(String(format: "%.2f", seg.endSeconds), privacy: .public)s \
                text=\(seg.text, privacy: .public)
                """)
        }
    }

    /// For each detected ad, log the ad itself plus the transcript segments
    /// whose timestamps fall inside it, so the audit trail in Console shows
    /// exactly which words were classified as advertisement.
    private func logDetectedAds(
        _ ads: [DetectedAd],
        chunk: [TranscribedSegment],
        chunkIndex: Int
    ) {
        for (adIndex, ad) in ads.enumerated() {
            Log.adDetection.debug("""
                ad chunk=\(chunkIndex) i=\(adIndex) \
                t=\(String(format: "%.2f", ad.startSeconds), privacy: .public)s–\(String(format: "%.2f", ad.endSeconds), privacy: .public)s \
                summary=\(ad.summary, privacy: .public)
                """)
            for seg in chunk where seg.startSeconds < ad.endSeconds && seg.endSeconds > ad.startSeconds {
                Log.adDetection.debug("""
                      ad-seg chunk=\(chunkIndex) ad=\(adIndex) \
                    t=\(String(format: "%.2f", seg.startSeconds), privacy: .public)s–\(String(format: "%.2f", seg.endSeconds), privacy: .public)s \
                    text=\(seg.text, privacy: .public)
                    """)
            }
        }
    }

    /// Merges overlapping or near-touching detected ads. Two ads are
    /// considered the same break when the gap between them is no greater than
    /// `gapSeconds` — this stitches back-to-back spots together and absorbs
    /// duplicates produced by the chunk overlap.
    private static func mergeOverlapping(
        _ ads: [DetectedAd],
        gapSeconds: Double
    ) -> [DetectedAd] {
        guard !ads.isEmpty else { return [] }
        let sorted = ads.sorted { $0.startSeconds < $1.startSeconds }
        var merged: [DetectedAd] = [sorted[0]]
        for ad in sorted.dropFirst() {
            let last = merged[merged.count - 1]
            if ad.startSeconds <= last.endSeconds + gapSeconds {
                merged[merged.count - 1] = DetectedAd(
                    startSeconds: last.startSeconds,
                    endSeconds: max(last.endSeconds, ad.endSeconds),
                    summary: last.summary
                )
            } else {
                merged.append(ad)
            }
        }
        return merged
    }

    private static func describe(_ availability: SystemLanguageModel.Availability) -> String {
        switch availability {
        case .available: "available"
        case .unavailable(let reason):
            switch reason {
            case .deviceNotEligible: "device not Apple Intelligence-eligible"
            case .appleIntelligenceNotEnabled: "Apple Intelligence not enabled in Settings"
            case .modelNotReady: "model still downloading — try again later"
            @unknown default: "unavailable"
            }
        @unknown default: "unavailable"
        }
    }
}
