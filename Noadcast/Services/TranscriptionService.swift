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

/// Tiny ref-type sentinel so `AVAudioConverterInputBlock`'s `@Sendable`
/// closure can read/write a single-use flag without capturing a mutable
/// `var`. The block is invoked from `AVAudioConverter.convert`, which calls
/// it synchronously on the caller's thread — the threading is safe by
/// construction even though we're marked `@unchecked Sendable`.
nonisolated private final class ConverterConsumed: @unchecked Sendable {
    var value: Bool = false
}

/// Wraps Apple's iOS 26 `SpeechAnalyzer` + `SpeechTranscriber` to produce
/// timestamped segments from a local audio file.
///
/// Long episodes (≥ ~79 minutes) consistently failed `analyzeSequence(from:)`
/// with `_GenericObjCError.nilError` — an internal accumulation/duration
/// limit somewhere inside `SpeechAnalyzer`. Working around it by splitting
/// the file into chunks well under that limit, running a fresh analyzer per
/// chunk, and concatenating the segments with their timestamps offset to
/// the chunk's start.
actor TranscriptionService {
    static let shared = TranscriptionService()

    /// Per-chunk processing limit. Chosen so we stay comfortably under the
    /// ~79-minute failure point observed in `analyzeSequence(from:)`.
    private static let chunkDurationSeconds: Double = 30 * 60
    /// PCM frames per buffer pushed to the analyzer. ~46 ms at 44.1 kHz —
    /// small enough that memory stays bounded, big enough to avoid
    /// per-buffer overhead dominating.
    private static let bufferFrames: AVAudioFrameCount = 4096

    func transcribe(
        fileURL: URL,
        locale: Locale = .current,
        progress: (@Sendable (Double, Double) -> Void)? = nil
    ) async throws -> [TranscribedSegment] {
        Log.transcription.info("Begin transcription — file=\(fileURL.lastPathComponent, privacy: .public) locale=\(locale.identifier, privacy: .public)")
        let resolvedLocale = await Self.resolveLocale(locale)
        Log.transcription.debug("Resolved locale → \(resolvedLocale.identifier, privacy: .public)")
        try await Self.ensureModelInstalled(for: resolvedLocale)

        let audioFile: AVAudioFile
        do {
            audioFile = try AVAudioFile(forReading: fileURL)
        } catch {
            throw TranscriptionError.audioFileUnreadable(error)
        }
        let sampleRate = audioFile.fileFormat.sampleRate
        let totalFrames = audioFile.length
        let totalDuration = Double(totalFrames) / sampleRate
        let chunkFrames = AVAudioFramePosition(Self.chunkDurationSeconds * sampleRate)

        var allSegments: [TranscribedSegment] = []
        var chunkStart: AVAudioFramePosition = 0
        var chunkIndex = 0

        while chunkStart < totalFrames {
            try Task.checkCancellation()
            let chunkEnd = min(chunkStart + chunkFrames, totalFrames)
            let chunkOffsetSec = Double(chunkStart) / sampleRate
            let chunkEndSec = Double(chunkEnd) / sampleRate
            Log.transcription.info("Transcribing chunk \(chunkIndex) [\(chunkOffsetSec, format: .fixed(precision: 1))s..\(chunkEndSec, format: .fixed(precision: 1))s]")

            let chunkSegments = try await transcribeChunk(
                fileURL: fileURL,
                startFrame: chunkStart,
                endFrame: chunkEnd,
                locale: resolvedLocale,
                offsetSeconds: chunkOffsetSec,
                totalDuration: totalDuration,
                progress: progress
            )
            allSegments.append(contentsOf: chunkSegments)
            chunkStart = chunkEnd
            chunkIndex += 1
        }

        Log.transcription.info("Transcription complete — file=\(fileURL.lastPathComponent, privacy: .public) chunks=\(chunkIndex) segments=\(allSegments.count)")
        return allSegments
    }

    // MARK: - Per-chunk transcription

    private func transcribeChunk(
        fileURL: URL,
        startFrame: AVAudioFramePosition,
        endFrame: AVAudioFramePosition,
        locale: Locale,
        offsetSeconds: Double,
        totalDuration: Double,
        progress: (@Sendable (Double, Double) -> Void)?
    ) async throws -> [TranscribedSegment] {
        let audioFile: AVAudioFile
        do {
            audioFile = try AVAudioFile(forReading: fileURL)
        } catch {
            throw TranscriptionError.audioFileUnreadable(error)
        }
        audioFile.framePosition = startFrame
        let sourceFormat = audioFile.processingFormat

        // SpeechAnalyzer requires 16-bit signed integer PCM. `AVAudioFile`
        // hands us `Float32` regardless of the underlying file format, so we
        // run every buffer through an `AVAudioConverter` to Int16 mono at
        // 16 kHz (the standard sample rate for speech recognition) before
        // yielding it to the analyzer.
        guard let analyzerFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: 16_000,
            channels: 1,
            interleaved: true
        ) else {
            throw TranscriptionError.analyzerFailure(
                NSError(domain: "Noadcast.Transcription", code: -1,
                        userInfo: [NSLocalizedDescriptionKey: "Couldn't construct analyzer audio format"])
            )
        }
        guard let converter = AVAudioConverter(from: sourceFormat, to: analyzerFormat) else {
            throw TranscriptionError.analyzerFailure(
                NSError(domain: "Noadcast.Transcription", code: -2,
                        userInfo: [NSLocalizedDescriptionKey: "Couldn't create AVAudioConverter \(sourceFormat) → \(analyzerFormat)"])
            )
        }

        let transcriber = SpeechTranscriber(
            locale: locale,
            transcriptionOptions: [],
            reportingOptions: [],
            attributeOptions: [.audioTimeRange]
        )
        let analyzer = SpeechAnalyzer(modules: [transcriber])

        let (inputStream, continuation) = AsyncStream<AnalyzerInput>.makeStream()

        let resultsTask = Task { () throws -> [TranscribedSegment] in
            var collected: [TranscribedSegment] = []
            for try await result in transcriber.results where result.isFinal {
                let text = String(result.text.characters)
                let range = result.range
                let start = range.start.seconds
                let end = range.end.seconds
                let globalStart = (start.isFinite ? start : 0) + offsetSeconds
                let globalEnd = (end.isFinite ? end : 0) + offsetSeconds
                collected.append(TranscribedSegment(
                    startSeconds: globalStart,
                    endSeconds: globalEnd,
                    text: text
                ))
                if let progress {
                    progress(min(globalEnd, totalDuration), totalDuration)
                }
            }
            return collected
        }

        do {
            try await analyzer.start(inputSequence: inputStream)

            // Output buffer is sized to comfortably hold one input buffer
            // after sample-rate conversion. Source rates above the analyzer
            // rate (the common case for 44.1 kHz podcast audio → 16 kHz)
            // produce *fewer* output frames, so this capacity is conservative.
            let resampleRatio = analyzerFormat.sampleRate / sourceFormat.sampleRate
            let outputCapacity = AVAudioFrameCount(
                ceil(Double(Self.bufferFrames) * resampleRatio)
            ) + 1024

            while audioFile.framePosition < endFrame {
                try Task.checkCancellation()
                let remaining = endFrame - audioFile.framePosition
                let inputCapacity = AVAudioFrameCount(min(AVAudioFramePosition(Self.bufferFrames), remaining))
                guard inputCapacity > 0,
                      let inputBuffer = AVAudioPCMBuffer(pcmFormat: sourceFormat, frameCapacity: inputCapacity),
                      let outputBuffer = AVAudioPCMBuffer(pcmFormat: analyzerFormat, frameCapacity: outputCapacity)
                else { break }
                try audioFile.read(into: inputBuffer)
                if inputBuffer.frameLength == 0 { break }

                // `AVAudioConverterInputBlock` is `@Sendable`; can't capture a
                // mutable `var` from it. A tiny ref-typed flag works around
                // the Swift 6 capture rule.
                let consumedFlag = ConverterConsumed()
                var convertError: NSError?
                let inputBlock: AVAudioConverterInputBlock = { _, status in
                    if consumedFlag.value {
                        status.pointee = .noDataNow
                        return nil
                    }
                    consumedFlag.value = true
                    status.pointee = .haveData
                    return inputBuffer
                }
                let result = converter.convert(
                    to: outputBuffer,
                    error: &convertError,
                    withInputFrom: inputBlock
                )
                if result == .error, let convertError {
                    throw convertError
                }
                if outputBuffer.frameLength > 0 {
                    continuation.yield(AnalyzerInput(buffer: outputBuffer))
                }
            }
            continuation.finish()

            try await analyzer.finalizeAndFinishThroughEndOfInput()
            return try await resultsTask.value
        } catch {
            resultsTask.cancel()
            continuation.finish()
            Log.transcription.error("Chunk transcription failed at offset \(offsetSeconds, format: .fixed(precision: 1))s: \(Log.describe(error), privacy: .public)")
            throw TranscriptionError.analyzerFailure(error)
        }
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
