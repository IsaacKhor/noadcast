import Foundation
import AVFAudio
import CoreMedia
import Speech
import os

enum AppleSpeechTranscriptionError: LocalizedError {
    case transcriberUnavailable
    case unsupportedLocale(String)
    case assetInstallationUnavailable(String)
    case emptyTranscript
    case failed(String)

    var errorDescription: String? {
        switch self {
        case .transcriberUnavailable:
            "Apple local transcription is unavailable on this device."
        case .unsupportedLocale(let locale):
            "Apple local transcription is unavailable for \(locale)."
        case .assetInstallationUnavailable(let locale):
            "Apple local transcription assets couldn't be installed for \(locale)."
        case .emptyTranscript:
            "Apple local transcription returned an empty transcript."
        case .failed(let message):
            "Apple local transcription failed: \(message)"
        }
    }
}

nonisolated final class AppleSpeechTranscriptionService: @unchecked Sendable {
    static let shared = AppleSpeechTranscriptionService()

    typealias ProgressHandler = @Sendable (_ status: String, _ currentSeconds: Double?, _ totalSeconds: Double?) -> Void

    private static let progressReportMinimumFraction = 0.01
    private static let progressReportMinimumSeconds: Double = 15

    private init() {}

    func transcribe(
        fileURL: URL,
        onProgress: ProgressHandler? = nil
    ) async throws -> [TimestampedTranscriptSegment] {
        guard SpeechTranscriber.isAvailable else {
            throw AppleSpeechTranscriptionError.transcriberUnavailable
        }

        Self.report(
            "Preparing Apple local transcription…",
            to: onProgress
        )
        let locale = try await Self.preferredTranscriptionLocale()
        let transcriber = SpeechTranscriber(
            locale: locale,
            preset: .timeIndexedProgressiveTranscription
        )
        try await ensureAssetsInstalled(
            for: transcriber,
            locale: locale,
            onProgress: onProgress
        )

        let audioFile: AVAudioFile
        do {
            audioFile = try AVAudioFile(forReading: fileURL)
        } catch {
            throw AppleSpeechTranscriptionError.failed(error.localizedDescription)
        }

        let totalSeconds = Self.audioDurationSeconds(audioFile)
        Self.report(
            "Preparing Apple transcription model…",
            currentSeconds: totalSeconds.map { _ in 0.0 },
            totalSeconds: totalSeconds,
            to: onProgress
        )
        let analyzer = SpeechAnalyzer(
            modules: [transcriber],
            options: .init(priority: .utility, modelRetention: .whileInUse)
        )

        Log.adDetection.info("Apple local transcription begin - file=\(fileURL.lastPathComponent, privacy: .public) locale=\(locale.identifier, privacy: .public)")
        let resultTask = Task<[TimestampedTranscriptSegment], Error> {
            var segments: [TimestampedTranscriptSegment] = []
            var lastReportedSeconds: Double = 0
            var lastReportedFraction: Double = 0

            func reportIfNeeded(_ segmentEndSeconds: Double, force: Bool = false) {
                guard let totalSeconds, totalSeconds > 0 else { return }
                let currentSeconds = max(0, min(segmentEndSeconds, totalSeconds))
                let fraction = currentSeconds / totalSeconds
                let passedFractionThreshold = fraction - lastReportedFraction >= Self.progressReportMinimumFraction
                let passedSecondsThreshold = currentSeconds - lastReportedSeconds >= Self.progressReportMinimumSeconds
                guard force || passedFractionThreshold || passedSecondsThreshold else { return }
                lastReportedSeconds = currentSeconds
                lastReportedFraction = fraction
                Self.report(
                    "Transcribing locally with Apple…",
                    currentSeconds: currentSeconds,
                    totalSeconds: totalSeconds,
                    to: onProgress
                )
            }

            for try await result in transcriber.results {
                guard result.isFinal,
                      let segment = Self.segment(from: result)
                else {
                    continue
                }
                segments.append(segment)
                reportIfNeeded(segment.endSeconds)
            }
            reportIfNeeded(totalSeconds ?? lastReportedSeconds, force: true)
            return Self.coalescedSegments(from: segments)
        }

        do {
            try await analyzer.prepareToAnalyze(in: audioFile.processingFormat)
            Self.report(
                "Transcribing locally with Apple…",
                currentSeconds: totalSeconds.map { _ in 0.0 },
                totalSeconds: totalSeconds,
                to: onProgress
            )
            try await analyzer.start(inputAudioFile: audioFile, finishAfterFile: true)
            let segments = try await resultTask.value
            guard !segments.isEmpty else {
                throw AppleSpeechTranscriptionError.emptyTranscript
            }
            Log.adDetection.info("Apple local transcription complete - chunks=\(segments.count)")
            return segments
        } catch {
            resultTask.cancel()
            await analyzer.cancelAndFinishNow()
            if error is CancellationError {
                throw error
            }
            if let transcriptionError = error as? AppleSpeechTranscriptionError {
                throw transcriptionError
            }
            throw AppleSpeechTranscriptionError.failed(error.localizedDescription)
        }
    }

    private static func preferredTranscriptionLocale() async throws -> Locale {
        if let current = await SpeechTranscriber.supportedLocale(equivalentTo: .current) {
            return current
        }
        if let english = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: "en_US")) {
            return english
        }
        let supportedLocales = await SpeechTranscriber.supportedLocales
        guard let first = supportedLocales.first else {
            throw AppleSpeechTranscriptionError.transcriberUnavailable
        }
        return first
    }

    private func ensureAssetsInstalled(
        for transcriber: SpeechTranscriber,
        locale: Locale,
        onProgress: ProgressHandler?
    ) async throws {
        let modules: [any SpeechModule] = [transcriber]
        switch await AssetInventory.status(forModules: modules) {
        case .installed:
            return
        case .supported, .downloading:
            Self.report("Downloading Apple transcription assets…", to: onProgress)
            guard let request = try await AssetInventory.assetInstallationRequest(supporting: modules) else {
                throw AppleSpeechTranscriptionError.assetInstallationUnavailable(locale.identifier)
            }
            try await request.downloadAndInstall()
            guard await AssetInventory.status(forModules: modules) == .installed else {
                throw AppleSpeechTranscriptionError.assetInstallationUnavailable(locale.identifier)
            }
        case .unsupported:
            throw AppleSpeechTranscriptionError.unsupportedLocale(locale.identifier)
        @unknown default:
            throw AppleSpeechTranscriptionError.assetInstallationUnavailable(locale.identifier)
        }
    }

    private static func audioDurationSeconds(_ audioFile: AVAudioFile) -> Double? {
        let sampleRate = audioFile.processingFormat.sampleRate
        guard sampleRate > 0, audioFile.length > 0 else { return nil }
        let duration = Double(audioFile.length) / sampleRate
        guard duration.isFinite, duration > 0 else { return nil }
        return duration
    }

    private static func report(
        _ status: String,
        currentSeconds: Double? = nil,
        totalSeconds: Double? = nil,
        to handler: ProgressHandler?
    ) {
        handler?(status, currentSeconds, totalSeconds)
    }

    private static func coalescedSegments(
        from segments: [TimestampedTranscriptSegment],
        maxChunkDuration: TimeInterval = 20,
        maxGap: TimeInterval = 1.5
    ) -> [TimestampedTranscriptSegment] {
        var chunks: [TimestampedTranscriptSegment] = []
        var start: Double?
        var end: Double?
        var words: [String] = []

        func flush() {
            guard let chunkStart = start,
                  let chunkEnd = end,
                  chunkEnd > chunkStart
            else {
                start = nil
                end = nil
                words.removeAll()
                return
            }
            let text = words.joined(separator: " ")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty {
                chunks.append(TimestampedTranscriptSegment(
                    startSeconds: chunkStart,
                    endSeconds: chunkEnd,
                    text: text
                ))
            }
            start = nil
            end = nil
            words.removeAll()
        }

        for segment in segments.sorted(by: { $0.startSeconds < $1.startSeconds }) {
            let text = segment.text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }

            let segmentStart = segment.startSeconds
            let segmentEnd = max(segment.endSeconds, segmentStart + 0.05)
            if let chunkStart = start, let chunkEnd = end {
                let wouldExceedDuration = segmentEnd - chunkStart > maxChunkDuration
                let hasLargeGap = segmentStart - chunkEnd > maxGap
                let endedSentence = words.last.map(Self.endsSentence) ?? false
                if hasLargeGap || wouldExceedDuration || endedSentence {
                    flush()
                }
            }

            if start == nil {
                start = segmentStart
            }
            end = max(end ?? segmentEnd, segmentEnd)
            words.append(text)
        }
        flush()
        return chunks
    }

    private static func segment(
        from result: SpeechTranscriber.Result
    ) -> TimestampedTranscriptSegment? {
        let text = String(result.text.characters)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }

        let startSeconds = CMTimeGetSeconds(result.range.start)
        let endSeconds = CMTimeGetSeconds(CMTimeRangeGetEnd(result.range))
        guard startSeconds.isFinite,
              endSeconds.isFinite,
              endSeconds > startSeconds
        else {
            return nil
        }

        return TimestampedTranscriptSegment(
            startSeconds: startSeconds,
            endSeconds: endSeconds,
            text: text
        )
    }

    private static func endsSentence(_ text: String) -> Bool {
        guard let last = text.trimmingCharacters(in: .whitespacesAndNewlines).last else {
            return false
        }
        return ".!?".contains(last)
    }
}
