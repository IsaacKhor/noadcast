import Foundation
import AVFoundation
import Speech
import os

struct TranscribedSegment: Sendable {
    let startSeconds: Double
    let endSeconds: Double
    let text: String
}

enum TranscriptionError: LocalizedError {
    case unsupportedLocale
    case assetUnavailable
    case audioFileUnreadable(Error)
    case analyzerFailure(Error)

    var errorDescription: String? {
        switch self {
        case .unsupportedLocale:
            "Transcription isn't supported for this device's locale."
        case .assetUnavailable:
            "The on-device speech model is unavailable."
        case .audioFileUnreadable(let err):
            "Couldn't read the audio file: \(err.localizedDescription)"
        case .analyzerFailure(let err):
            "Speech analyzer failed: \(err.localizedDescription)"
        }
    }
}

/// Wraps Apple's iOS 26 `SpeechAnalyzer` + `SpeechTranscriber` to produce
/// timestamped segments from a local audio file.
actor TranscriptionService {
    static let shared = TranscriptionService()

    /// Transcribes a local audio file end-to-end and returns the final
    /// segments with start/end seconds. The `progress` callback receives
    /// (`currentSeconds`, `totalSeconds`) — i.e. how far through the audio
    /// the analyzer has reported a final segment, and the file's total
    /// duration.
    func transcribe(
        fileURL: URL,
        locale: Locale = .current,
        progress: (@Sendable (Double, Double) -> Void)? = nil
    ) async throws -> [TranscribedSegment] {
        Log.transcription.info("Begin transcription — file=\(fileURL.lastPathComponent, privacy: .public) locale=\(locale.identifier, privacy: .public)")
        let resolvedLocale = await Self.resolveLocale(locale)
        Log.transcription.debug("Resolved locale → \(resolvedLocale.identifier, privacy: .public)")
        try await Self.ensureModelInstalled(for: resolvedLocale)

        let transcriber = SpeechTranscriber(
            locale: resolvedLocale,
            transcriptionOptions: [],
            reportingOptions: [],
            attributeOptions: [.audioTimeRange]
        )

        let analyzer = SpeechAnalyzer(modules: [transcriber])

        let audioFile: AVAudioFile
        do {
            audioFile = try AVAudioFile(forReading: fileURL)
        } catch {
            throw TranscriptionError.audioFileUnreadable(error)
        }
        let totalDuration = Double(audioFile.length) / audioFile.fileFormat.sampleRate

        var segments: [TranscribedSegment] = []

        let resultsTask = Task { () throws -> [TranscribedSegment] in
            var collected: [TranscribedSegment] = []
            for try await result in transcriber.results where result.isFinal {
                let text = String(result.text.characters)
                let range = result.range
                let start = range.start.seconds
                let end = range.end.seconds
                collected.append(TranscribedSegment(
                    startSeconds: start.isFinite ? start : 0,
                    endSeconds: end.isFinite ? end : 0,
                    text: text
                ))
                if let progress, end.isFinite {
                    progress(min(end, totalDuration), totalDuration)
                }
            }
            return collected
        }

        do {
            _ = try await analyzer.analyzeSequence(from: audioFile)
            try await analyzer.finalizeAndFinishThroughEndOfInput()
            segments = try await resultsTask.value
        } catch {
            resultsTask.cancel()
            Log.transcription.error("SpeechAnalyzer failed — file=\(fileURL.lastPathComponent, privacy: .public) \(Log.describe(error), privacy: .public)")
            throw TranscriptionError.analyzerFailure(error)
        }

        Log.transcription.info("Transcription complete — file=\(fileURL.lastPathComponent, privacy: .public) segments=\(segments.count)")
        return segments
    }

    // MARK: - Model management

    private static func resolveLocale(_ requested: Locale) async -> Locale {
        if let supported = await SpeechTranscriber.supportedLocale(equivalentTo: requested) {
            return supported
        }
        return Locale(identifier: "en-US")
    }

    private static func ensureModelInstalled(for locale: Locale) async throws {
        let bcp47 = locale.identifier(.bcp47)
        let installed = await SpeechTranscriber.installedLocales
        let alreadyInstalled = installed.contains { $0.identifier(.bcp47) == bcp47 }
        guard !alreadyInstalled else { return }

        Log.transcription.info("Installing speech model for \(bcp47, privacy: .public)…")
        let probe = SpeechTranscriber(
            locale: locale,
            transcriptionOptions: [],
            reportingOptions: [],
            attributeOptions: []
        )
        do {
            if let request = try await AssetInventory.assetInstallationRequest(supporting: [probe]) {
                try await request.downloadAndInstall()
            }
            Log.transcription.info("Speech model installed for \(bcp47, privacy: .public)")
        } catch {
            Log.transcription.error("Failed to install speech model for \(bcp47, privacy: .public): \(Log.describe(error), privacy: .public)")
            throw TranscriptionError.assetUnavailable
        }
    }
}
