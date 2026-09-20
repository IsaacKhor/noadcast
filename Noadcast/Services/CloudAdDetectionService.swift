import Foundation
import AVFoundation
import os

/// One-shot file-analysis helper: uploads the episode audio to Gemini and
/// receives skip segments in one structured-JSON response.
nonisolated struct CloudAdDetectionResult: Sendable {
    let ads: [DetectedAd]
    let usage: TokenUsage?
}

/// Per-stage signal the pipeline subscribes to so it can flip
/// `Episode.processingState` and surface backend-specific progress in the UI.
nonisolated enum CloudAdDetectionStage: Sendable {
    /// Bytes have started moving. `totalBytes` is the request body size as
    /// reported by `URLSession` during the Gemini Files upload.
    case uploading(bytesSent: Int64, totalBytes: Int64, status: String)
    /// Local or remote transcription is in progress. Transcript-capable
    /// backends can provide audio seconds so the UI can show a determinate
    /// progress bar; server backends may leave them `nil`.
    case transcribing(status: String, currentSeconds: Double?, totalSeconds: Double?)
    /// Upload finished; we're now waiting on the LLM to produce the
    /// structured response.
    case analyzing(String)
}

enum CloudAdDetectionError: LocalizedError {
    case providerUnsupported(String)
    case missingAPIKey(String)
    case unsupportedAudioFormat(String)
    case invalidServerURL(String)
    case downsampleFailed(Error)
    case uploadFailed(Error)
    case serverFailed(Int, String)
    case parseFailure(String)

    var errorDescription: String? {
        switch self {
        case .providerUnsupported(let provider):
            "\(provider) doesn't support file-based analysis. Pick a different Gemini model in Settings → Detection model."
        case .missingAPIKey(let provider):
            "\(provider) API key missing — add one in Settings → Detection model."
        case .unsupportedAudioFormat(let format):
            "OpenRouter can't accept this episode's audio format (\(format)). Enable upload downsampling or use Direct Gemini upload."
        case .invalidServerURL(let value):
            "Invalid ad-detection server URL: \(value)"
        case .downsampleFailed(let err):
            "Couldn't downsample the audio file: \(err.localizedDescription)"
        case .uploadFailed(let err):
            "Couldn't upload the audio file: \(err.localizedDescription)"
        case .serverFailed(let status, let body):
            "Ad-detection server failed with HTTP \(status): \(body)"
        case .parseFailure(let msg):
            "Couldn't parse the provider response: \(msg)"
        }
    }
}

/// Runs the Gemini Files upload + `generateContent` call on a **background**
/// `URLSession`. Both legs use `uploadTask(with:fromFile:)` — the only
/// task flavor a background config supports for outbound requests — so the
/// transfers keep running while the app is suspended. Delegate callbacks
/// land on the session's serial delegate queue and are bridged to the
/// caller's `async` continuation through a per-task entry in `pending`,
/// guarded by `lock`.
nonisolated final class CloudAdDetectionService: NSObject, @unchecked Sendable {
    static let shared = CloudAdDetectionService()

    static let backgroundSessionIdentifier = "com.isaackhor.Noadcast.background-ad-detection"

    private struct PendingUpload {
        var receivedData = Data()
        /// Temp file holding a request body we built; deleted on completion.
        /// `nil` when the upload body is the user's actual audio file.
        var bodyFileURL: URL?
        var progressHandler: (@Sendable (Int64, Int64) -> Void)?
        var completion: @Sendable (Result<(Data, HTTPURLResponse), Error>) -> Void
    }

    private let lock = NSLock()
    private var pending: [Int: PendingUpload] = [:]
    private var uploadProgressSnapshots: [Int: TransferProgressSnapshot] = [:]
    private var pendingBackgroundCompletion: BackgroundCompletion?

    private var sessionStorage: URLSession!
    var session: URLSession { sessionStorage }
    private let decoder = JSONDecoder()

    private struct UploadAudio {
        let fileURL: URL
        let mimeType: String
        let cleanupURL: URL?
    }

    private struct MultipartBody {
        let fileURL: URL
        let boundary: String
        let byteCount: Int64
    }

    private struct OpenRouterJSONBody {
        let fileURL: URL
        let byteCount: Int64
    }

    private struct UploadedGeminiFile {
        let uri: String
        let name: String?
    }

    private struct TransferProgressSnapshot {
        var lastYieldUptime: TimeInterval
        var lastBytesSent: Int64
        var lastTotalBytes: Int64
    }

    private static let progressThrottleInterval: TimeInterval = 0.5
    private static let progressThrottleBytes: Int64 = 512 * 1024
    private static let progressThrottleFraction: Double = 0.01

    private final class DownsamplePump: @unchecked Sendable {
        private let reader: AVAssetReader
        private let readerOutput: AVAssetReaderTrackOutput
        private let writer: AVAssetWriter
        private let writerInput: AVAssetWriterInput
        private let lock = NSLock()
        private var hasCompleted = false

        init(
            reader: AVAssetReader,
            readerOutput: AVAssetReaderTrackOutput,
            writer: AVAssetWriter,
            writerInput: AVAssetWriterInput
        ) {
            self.reader = reader
            self.readerOutput = readerOutput
            self.writer = writer
            self.writerInput = writerInput
        }

        func start(
            on queue: DispatchQueue,
            completion: @escaping @Sendable (Result<Void, Error>) -> Void
        ) {
            writerInput.requestMediaDataWhenReady(on: queue) { [self] in
                while writerInput.isReadyForMoreMediaData {
                    if reader.status == .reading, let sample = readerOutput.copyNextSampleBuffer() {
                        guard writerInput.append(sample) else {
                            reader.cancelReading()
                            writer.cancelWriting()
                            complete(
                                .failure(writer.error ?? CloudAdDetectionService.downsampleError("Couldn't append audio sample.")),
                                completion
                            )
                            return
                        }
                    } else {
                        writerInput.markAsFinished()
                        writer.finishWriting { [self] in
                            complete(finalResult(), completion)
                        }
                        return
                    }
                }
            }
        }

        private func finalResult() -> Result<Void, Error> {
            if reader.status == .failed {
                return .failure(reader.error ?? CloudAdDetectionService.downsampleError("Audio reader failed."))
            }
            if writer.status == .failed || writer.status == .cancelled {
                return .failure(writer.error ?? CloudAdDetectionService.downsampleError("Audio writer failed."))
            }
            guard writer.status == .completed else {
                return .failure(CloudAdDetectionService.downsampleError("Audio writer ended in state \(writer.status.rawValue)."))
            }
            return .success(())
        }

        private func complete(
            _ result: Result<Void, Error>,
            _ completion: @Sendable (Result<Void, Error>) -> Void
        ) {
            lock.lock()
            guard !hasCompleted else {
                lock.unlock()
                return
            }
            hasCompleted = true
            lock.unlock()
            completion(result)
        }
    }

    override private init() {
        super.init()
        let config = URLSessionConfiguration.background(withIdentifier: Self.backgroundSessionIdentifier)
        config.sessionSendsLaunchEvents = true
        config.isDiscretionary = false
        config.allowsCellularAccess = true
        // LLM calls can sit for a while; resource-timeout governs the whole
        // task (upload + response wait), so we give it room.
        config.timeoutIntervalForRequest = 60 * 5
        config.timeoutIntervalForResource = 60 * 30
        sessionStorage = URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }

    /// Stores the completion handler iOS hands us when relaunching the app
    /// to deliver background events for this session. Called from
    /// `AppDelegate`.
    func storePendingBackgroundCompletion(_ handler: @escaping () -> Void) {
        lock.lock()
        pendingBackgroundCompletion = BackgroundCompletion(handler)
        lock.unlock()
    }

    nonisolated static let segmentsOnlyPrompt: String = """
    You are analyzing a podcast episode audio file. Return a single JSON \
    object with one field, `segments`, containing every contiguous portion \
    of the audio the listener would want to skip.

    Each segment has a `kind`:

    - "intro": one contiguous segment near the BEGINNING of the episode \
    covering theme music, branding, and any preroll ads. At most one per episode. Spans from the start of \
    the episode through to where the substantive content begins. Do NOT \
    include introductory content that may be substantive, like host banter,
    guest introductions, or introductory material to the episode's main
    topic — only the "front matter" that would be safe to skip without missing \
    anything important.

    - "outro": one contiguous segment at the very END of the episode \
    covering closing music, credits, next-episode teasers, postroll ads, \
    and farewells. At most one per episode. Spans from where the \
    substantive content finishes through the physical end of the audio file. \
    Fold every farewell, credit, closing theme, next-episode teaser, and \
    postroll ad in that final tail into this single outro rather than returning \
    separate entries. If the user supplies the complete episode duration, an \
    outro's `endSeconds` must equal that endpoint.

    - "ad": a mid-episode advertisement, sponsored message, host-read ad, \
    promo code, paid endorsement, or cross-promotion of another podcast \
    that appears BETWEEN the intro and outro. Editorial mentions, \
    listener mail, the host's own products discussed editorially, and \
    interview segments are NOT ads.

    Before returning, deliberately inspect the final portion of the episode \
    and make an explicit outro decision. Podcasts commonly end with a \
    farewell, credits, closing music, or a postroll ad. Return no outro only \
    when substantive episode content genuinely continues to the physical \
    endpoint and there is no safe-to-skip final tail.

    Use only timestamps that match the audio. Be conservative — flag segments \
    only when you're confident. Return an empty `segments` array if \
    nothing should be skipped. Do not include any fields other than `segments`.
    """

    nonisolated static let transcriptSegmentsPrompt: String = """
    You are analyzing a timestamped podcast transcript. Return a single JSON \
    object with one field, `segments`, containing every contiguous portion \
    of the episode the listener would want to skip.

    Each segment has a `kind`:

    - "intro": one contiguous segment near the BEGINNING of the episode \
    covering theme music, branding, and any preroll ads. At most one per episode. \
    Spans from the start through where substantive content begins. Do not \
    include host banter, guest introductions, or setup for the main topic.

    - "ad": a mid-episode advertisement, sponsored message, host-read ad, \
    promo code, paid endorsement, or cross-promotion of another podcast \
    that appears BETWEEN the intro and outro.

    - "outro": one contiguous segment at the very END of the episode \
    covering closing music, credits, next-episode teasers, postroll ads, \
    and farewells. At most one per episode. Fold all of those elements in the \
    final tail into this single outro. Speech transcription may omit trailing \
    music, silence, or low-confidence postroll audio: ground `startSeconds` in \
    the transcript, but when the user supplies the physical episode endpoint, \
    set the detected outro's `endSeconds` to that endpoint.

    Before returning, deliberately inspect the transcript's final portion and \
    make an explicit outro decision. Podcasts commonly have a skippable final \
    tail. Return no outro only when substantive content genuinely continues \
    through the endpoint and there is no farewell, credit, closing theme, \
    teaser, or postroll ad to skip.

    Segment starts and all non-outro timestamps must be grounded in the \
    transcript. Be conservative about intros and mid-episode ads. Return an \
    empty `segments` array if nothing should be skipped. Do not include any \
    fields other than `segments`.
    """

    nonisolated static func durationPromptContext(_ episodeDuration: Double?) -> String {
        guard let duration = validEpisodeDuration(episodeDuration) else { return "" }
        let endpoint = String(
            format: "%.2f",
            locale: Locale(identifier: "en_US_POSIX"),
            duration
        )
        return """
        The complete episode duration is \(endpoint) seconds. Treat \(endpoint) \
        seconds as the physical audio endpoint and deliberately inspect the \
        final portion. If an outro exists, its endSeconds must be \(endpoint), \
        including any trailing music, silence, or postroll audio.
        """
    }

    nonisolated static func validEpisodeDuration(_ episodeDuration: Double?) -> Double? {
        guard let episodeDuration,
              episodeDuration.isFinite,
              episodeDuration > 0 else { return nil }
        return episodeDuration
    }

    nonisolated static func transcriptEndpointGuidance(
        episodeDuration: Double?,
        transcriptEnd: Double?
    ) -> String {
        let validTranscriptEnd = transcriptEnd.flatMap { value -> Double? in
            guard value.isFinite, value > 0 else { return nil }
            return value
        }
        if let duration = validEpisodeDuration(episodeDuration),
           validTranscriptEnd == nil || duration >= validTranscriptEnd! {
            return """
            \(durationPromptContext(duration))
            A detected outro may end at that supplied physical endpoint because \
            transcription can omit the trailing non-speech tail.
            """
        }
        return """
        The physical episode endpoint is unavailable. Keep every returned \
        timestamp within the transcript ranges and use the final transcript \
        timestamp as a detected outro's endSeconds.
        """
    }

    /// Top-level entry point. Direct Gemini uploads stream audio through
    /// Files API; transcript backends emit transcribe/analyze stages and
    /// only send timestamped text to Gemini. `onStage` carries enough
    /// backend-specific text for the downloads row to describe the current
    /// action.
    ///
    /// `episodeGUID`, when supplied, is set as each task's
    /// `taskDescription` so that `cancelTasks(forEpisodeGUID:)` can find
    /// and cancel orphaned tasks belonging to a given episode after an
    /// app termination + relaunch.
    func analyzeFile(
        fileURL: URL,
        backend: AdDetectionBackend,
        provider: AdDetectionProvider,
        googleAPIKey: String?,
        mimeType: String,
        openRouterAPIKey: String? = nil,
        episodeDuration: Double? = nil,
        thinkingLevel: AdDetectionThinkingLevel = .automatic,
        downsampleBeforeUpload: Bool = false,
        serverHost: String = "",
        serverPort: Int = 0,
        episodeGUID: String? = nil,
        onStage: (@Sendable (CloudAdDetectionStage) -> Void)? = nil
    ) async throws -> CloudAdDetectionResult {
        switch backend {
        case .geminiFiles:
            return try await analyzeWithGeminiFiles(
                fileURL: fileURL,
                provider: provider,
                googleAPIKey: googleAPIKey,
                mimeType: mimeType,
                episodeDuration: episodeDuration,
                thinkingLevel: thinkingLevel,
                downsampleBeforeUpload: downsampleBeforeUpload,
                episodeGUID: episodeGUID,
                onStage: onStage
            )
        case .openRouter:
            return try await analyzeWithOpenRouter(
                fileURL: fileURL,
                provider: provider,
                openRouterAPIKey: openRouterAPIKey,
                mimeType: mimeType,
                episodeDuration: episodeDuration,
                thinkingLevel: thinkingLevel,
                downsampleBeforeUpload: downsampleBeforeUpload,
                episodeGUID: episodeGUID,
                onStage: onStage
            )
        case .whisperServer:
            return try await analyzeWithWhisperServer(
                fileURL: fileURL,
                provider: provider,
                googleAPIKey: googleAPIKey,
                mimeType: mimeType,
                episodeDuration: episodeDuration,
                thinkingLevel: thinkingLevel,
                serverHost: serverHost,
                serverPort: serverPort,
                episodeGUID: episodeGUID,
                onStage: onStage
            )
        case .appleSpeech:
            return try await analyzeWithAppleSpeech(
                fileURL: fileURL,
                provider: provider,
                googleAPIKey: googleAPIKey,
                episodeDuration: episodeDuration,
                thinkingLevel: thinkingLevel,
                episodeGUID: episodeGUID,
                onStage: onStage
            )
        }
    }

    private func analyzeWithGeminiFiles(
        fileURL: URL,
        provider: AdDetectionProvider,
        googleAPIKey: String?,
        mimeType: String,
        episodeDuration: Double?,
        thinkingLevel: AdDetectionThinkingLevel,
        downsampleBeforeUpload: Bool,
        episodeGUID: String?,
        onStage: (@Sendable (CloudAdDetectionStage) -> Void)?
    ) async throws -> CloudAdDetectionResult {
        guard provider.supportsAudioFileDetection else {
            throw CloudAdDetectionError.providerUnsupported(provider.label)
        }
        guard let key = googleAPIKey, !key.isEmpty else {
            throw CloudAdDetectionError.missingAPIKey(provider.label)
        }

        let uploadAudio = try await prepareUploadAudio(
            fileURL: fileURL,
            mimeType: mimeType,
            downsampleBeforeUpload: downsampleBeforeUpload
        )
        defer {
            if let cleanupURL = uploadAudio.cleanupURL {
                try? FileManager.default.removeItem(at: cleanupURL)
            }
        }

        let fileSize = (try? uploadAudio.fileURL.resourceValues(forKeys: [.fileSizeKey]).fileSize).map(Int64.init) ?? 0
        Log.adDetection.info("Audio analysis begin — provider=\(provider.label, privacy: .public) file=\(uploadAudio.fileURL.lastPathComponent, privacy: .public) bytes=\(fileSize) downsampled=\(downsampleBeforeUpload) path=files-api")

        let uploadedFile = try await uploadToGeminiFiles(
            fileURL: uploadAudio.fileURL,
            mimeType: uploadAudio.mimeType,
            apiKey: key,
            taskDescription: episodeGUID,
            onStage: onStage
        )
        onStage?(.analyzing("Asking Gemini…"))
        let ads: [DetectedAd]
        let usage: TokenUsage?
        do {
            (ads, usage) = try await callGeminiCombined(
                model: provider.apiModel,
                fileURI: uploadedFile.uri,
                mimeType: uploadAudio.mimeType,
                episodeDuration: episodeDuration,
                apiKey: key,
                thinkingLevel: provider.supportsThinkingLevel ? thinkingLevel : .automatic,
                taskDescription: episodeGUID
            )
        } catch {
            await deleteGeminiFile(uploadedFile, apiKey: key)
            throw error
        }
        await deleteGeminiFile(uploadedFile, apiKey: key)

        Log.adDetection.info("Audio analysis complete — ads=\(ads.count) input_tokens=\(usage?.inputTokens ?? 0) thought_tokens=\(usage?.thoughtTokens ?? 0) output_tokens=\(usage?.outputTokens ?? 0)")
        return CloudAdDetectionResult(ads: ads, usage: usage)
    }

    private func analyzeWithOpenRouter(
        fileURL: URL,
        provider: AdDetectionProvider,
        openRouterAPIKey: String?,
        mimeType: String,
        episodeDuration: Double?,
        thinkingLevel: AdDetectionThinkingLevel,
        downsampleBeforeUpload: Bool,
        episodeGUID: String?,
        onStage: (@Sendable (CloudAdDetectionStage) -> Void)?
    ) async throws -> CloudAdDetectionResult {
        guard provider.supportsAudioFileDetection else {
            throw CloudAdDetectionError.providerUnsupported(provider.label)
        }
        let key = openRouterAPIKey?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !key.isEmpty else {
            throw CloudAdDetectionError.missingAPIKey("OpenRouter")
        }

        let uploadAudio = try await prepareUploadAudio(
            fileURL: fileURL,
            mimeType: mimeType,
            downsampleBeforeUpload: downsampleBeforeUpload
        )
        defer {
            if let cleanupURL = uploadAudio.cleanupURL {
                try? FileManager.default.removeItem(at: cleanupURL)
            }
        }

        guard let audioFormat = Self.openRouterAudioFormat(
            mimeType: uploadAudio.mimeType,
            fileExtension: uploadAudio.fileURL.pathExtension
        ) else {
            throw CloudAdDetectionError.unsupportedAudioFormat(uploadAudio.mimeType)
        }

        let openRouterModel = provider.openRouterAPIModel
        let effectiveThinkingLevel = provider.supportsThinkingLevel
            ? thinkingLevel
            : .automatic
        let bodyTask = Task.detached(priority: .utility) {
            try Self.writeOpenRouterJSONBody(
                audioFileURL: uploadAudio.fileURL,
                audioFormat: audioFormat,
                model: openRouterModel,
                episodeDuration: episodeDuration,
                thinkingLevel: effectiveThinkingLevel
            )
        }
        let body = try await withTaskCancellationHandler {
            try await bodyTask.value
        } onCancel: {
            bodyTask.cancel()
        }
        // The upload helper also removes this file when its delegate callback
        // settles. This defer covers failures before a task is registered.
        defer { try? FileManager.default.removeItem(at: body.fileURL) }

        guard let url = URL(string: "https://openrouter.ai/api/v1/chat/completions") else {
            throw CloudAdDetectionError.uploadFailed(URLError(.badURL))
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(String(body.byteCount), forHTTPHeaderField: "Content-Length")
        request.setValue("Noadcast", forHTTPHeaderField: "X-Title")

        let sourceBytes = (try? uploadAudio.fileURL.resourceValues(forKeys: [.fileSizeKey]).fileSize)
            .map(Int64.init) ?? 0
        Log.adDetection.info("Audio analysis begin — provider=\(provider.label, privacy: .public) model=\(provider.openRouterAPIModel, privacy: .public) file=\(uploadAudio.fileURL.lastPathComponent, privacy: .public) audio_bytes=\(sourceBytes) request_bytes=\(body.byteCount) downsampled=\(downsampleBeforeUpload) path=openrouter")
        onStage?(.uploading(
            bytesSent: 0,
            totalBytes: body.byteCount,
            status: "Uploading audio to OpenRouter…"
        ))

        let (data, response) = try await upload(
            request: request,
            fromFile: body.fileURL,
            tempBodyURL: body.fileURL,
            taskDescription: episodeGUID,
            onProgress: { sent, total in
                if total > 0, sent >= total {
                    onStage?(.analyzing("Asking OpenRouter…"))
                } else {
                    onStage?(.uploading(
                        bytesSent: sent,
                        totalBytes: total,
                        status: "Uploading audio to OpenRouter…"
                    ))
                }
            }
        )

        guard (200..<300).contains(response.statusCode) else {
            let responseBody = String(data: data, encoding: .utf8) ?? ""
            Log.adDetection.error("OpenRouter call HTTP \(response.statusCode): \(responseBody, privacy: .public)")
            throw Self.openRouterHTTPError(statusCode: response.statusCode, body: responseBody)
        }

        let decoded: OpenRouterResponse
        do {
            decoded = try decoder.decode(OpenRouterResponse.self, from: data)
        } catch {
            throw CloudAdDetectionError.parseFailure(error.localizedDescription)
        }
        guard let text = decoded.choices.first?.message.content, !text.isEmpty else {
            throw CloudAdDetectionError.parseFailure("Missing OpenRouter response text")
        }

        let ads: [DetectedAd]
        do {
            let parsed = try JSONDecoder().decode(
                SegmentsOnlyResponse.self,
                from: Data(Self.strippingJSONFence(from: text).utf8)
            )
            ads = Self.detectedAds(from: parsed.segments)
        } catch {
            throw CloudAdDetectionError.parseFailure(error.localizedDescription)
        }

        let usage = decoded.usage.map { raw in
            let thoughtTokens = raw.completionTokensDetails?.reasoningTokens ?? 0
            return TokenUsage(
                inputTokens: raw.promptTokens ?? 0,
                thoughtTokens: thoughtTokens,
                outputTokens: max(0, (raw.completionTokens ?? 0) - thoughtTokens)
            )
        }
        Log.adDetection.info("Audio analysis complete — openrouter ads=\(ads.count) input_tokens=\(usage?.inputTokens ?? 0) thought_tokens=\(usage?.thoughtTokens ?? 0) output_tokens=\(usage?.outputTokens ?? 0)")
        return CloudAdDetectionResult(
            ads: ads.sorted { $0.startSeconds < $1.startSeconds },
            usage: usage
        )
    }

    private func analyzeWithWhisperServer(
        fileURL: URL,
        provider: AdDetectionProvider,
        googleAPIKey: String?,
        mimeType: String,
        episodeDuration: Double?,
        thinkingLevel: AdDetectionThinkingLevel,
        serverHost: String,
        serverPort: Int,
        episodeGUID: String?,
        onStage: (@Sendable (CloudAdDetectionStage) -> Void)?
    ) async throws -> CloudAdDetectionResult {
        let endpoint = try Self.serverAnalyzeURL(host: serverHost, port: serverPort)
        let fileSize = (try? fileURL.resourceValues(forKeys: [.fileSizeKey]).fileSize).map(Int64.init) ?? 0
        Log.adDetection.info("Audio analysis begin — provider=\(provider.label, privacy: .public) file=\(fileURL.lastPathComponent, privacy: .public) bytes=\(fileSize) path=whisper-server endpoint=\(endpoint.absoluteString, privacy: .public)")

        var fields: [String: String] = [
            "model": provider.apiModel,
            "mime_type": mimeType
        ]
        if let duration = Self.validEpisodeDuration(episodeDuration) {
            fields["episode_duration"] = String(
                format: "%.2f",
                locale: Locale(identifier: "en_US_POSIX"),
                duration
            )
        }
        if provider.supportsThinkingLevel, let level = thinkingLevel.apiValue {
            fields["thinking_level"] = level
        }
        if let googleAPIKey, !googleAPIKey.isEmpty {
            fields["google_api_key"] = googleAPIKey
        }

        let body = try writeMultipartBody(
            fileURL: fileURL,
            fileFieldName: "audio",
            mimeType: mimeType,
            fields: fields
        )

        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.setValue("multipart/form-data; boundary=\(body.boundary)", forHTTPHeaderField: "Content-Type")
        request.setValue(String(body.byteCount), forHTTPHeaderField: "Content-Length")

        let (data, response) = try await upload(
            request: request,
            fromFile: body.fileURL,
            tempBodyURL: body.fileURL,
            taskDescription: episodeGUID,
            onProgress: { sent, total in
                onStage?(.uploading(
                    bytesSent: sent,
                    totalBytes: total,
                    status: "Uploading audio to server…"
                ))
                if total > 0, sent >= total {
                    onStage?(.transcribing(
                        status: "Server transcribing and analyzing…",
                        currentSeconds: nil,
                        totalSeconds: nil
                    ))
                }
            }
        )

        guard (200..<300).contains(response.statusCode) else {
            let bodyText = String(data: data, encoding: .utf8) ?? ""
            Log.adDetection.error("Whisper server call HTTP \(response.statusCode): \(bodyText, privacy: .public)")
            throw CloudAdDetectionError.serverFailed(response.statusCode, String(bodyText.prefix(500)))
        }

        let decoded: ServerAdDetectionResponse
        do {
            decoded = try decoder.decode(ServerAdDetectionResponse.self, from: data)
        } catch {
            throw CloudAdDetectionError.parseFailure(error.localizedDescription)
        }
        let ads = Self.detectedAds(from: decoded.segments)
        Log.adDetection.info("Audio analysis complete — server ads=\(ads.count) input_tokens=\(decoded.usage?.inputTokens ?? 0) thought_tokens=\(decoded.usage?.thoughtTokens ?? 0) output_tokens=\(decoded.usage?.outputTokens ?? 0)")
        return CloudAdDetectionResult(ads: ads, usage: decoded.usage)
    }

    private func analyzeWithAppleSpeech(
        fileURL: URL,
        provider: AdDetectionProvider,
        googleAPIKey: String?,
        episodeDuration: Double?,
        thinkingLevel: AdDetectionThinkingLevel,
        episodeGUID: String?,
        onStage: (@Sendable (CloudAdDetectionStage) -> Void)?
    ) async throws -> CloudAdDetectionResult {
        guard let key = googleAPIKey, !key.isEmpty else {
            throw CloudAdDetectionError.missingAPIKey(provider.label)
        }

        onStage?(.transcribing(
            status: "Preparing Apple local transcription…",
            currentSeconds: nil,
            totalSeconds: nil
        ))
        Log.adDetection.info("Audio analysis begin — provider=\(provider.label, privacy: .public) file=\(fileURL.lastPathComponent, privacy: .public) path=apple-local-transcription")
        let transcript = try await AppleSpeechTranscriptionService.shared.transcribe(fileURL: fileURL) { status, currentSeconds, totalSeconds in
            onStage?(.transcribing(
                status: status,
                currentSeconds: currentSeconds,
                totalSeconds: totalSeconds
            ))
        }
        onStage?(.analyzing("Asking Gemini…"))
        let (ads, usage) = try await callGeminiTranscript(
            model: provider.apiModel,
            transcript: transcript,
            episodeDuration: episodeDuration,
            apiKey: key,
            thinkingLevel: provider.supportsThinkingLevel ? thinkingLevel : .automatic,
            taskDescription: episodeGUID
        )
        Log.adDetection.info("Audio analysis complete — apple-local-transcription ads=\(ads.count) input_tokens=\(usage?.inputTokens ?? 0) thought_tokens=\(usage?.thoughtTokens ?? 0) output_tokens=\(usage?.outputTokens ?? 0)")
        return CloudAdDetectionResult(ads: ads, usage: usage)
    }

    /// Cancels any background tasks tagged with `taskDescription == guid`.
    /// Used by the pipeline's launch-time recovery to clean up tasks left
    /// behind by a previous process before re-enqueueing the episode.
    func cancelTasks(forEpisodeGUID guid: String) async {
        let tasks = await session.allTasks
        for task in tasks where task.taskDescription == guid {
            task.cancel()
        }
    }

    // MARK: - Background-friendly upload helper

    /// Single-task helper: kick off an `uploadTask(with:fromFile:)` on the
    /// background session, accumulate the response body via the data
    /// delegate, and resume the awaiting caller when the task finishes.
    /// Set `tempBodyURL` when the body file is a temp file we built (it
    /// gets deleted after the task settles); leave it `nil` for uploads
    /// whose body is the user's audio file.
    private func upload(
        request: URLRequest,
        fromFile fileURL: URL,
        tempBodyURL: URL? = nil,
        taskDescription: String? = nil,
        onProgress: (@Sendable (Int64, Int64) -> Void)? = nil
    ) async throws -> (Data, HTTPURLResponse) {
        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<(Data, HTTPURLResponse), Error>) in
            let task = session.uploadTask(with: request, fromFile: fileURL)
            task.taskDescription = taskDescription
            lock.lock()
            pending[task.taskIdentifier] = PendingUpload(
                bodyFileURL: tempBodyURL,
                progressHandler: onProgress,
                completion: { result in
                    switch result {
                    case .success(let v): cont.resume(returning: v)
                    case .failure(let e): cont.resume(throwing: e)
                    }
                }
            )
            lock.unlock()
            task.resume()
        }
    }

    /// Writes an in-memory request body to a temp file so it can be passed
    /// to `uploadTask(with:fromFile:)` — background sessions can't accept
    /// `Data` bodies.
    private func writeTempBody(_ data: Data, ext: String = "json") throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("noadcast-cloud-\(UUID().uuidString).\(ext)")
        try data.write(to: url, options: [.atomic])
        return url
    }

    nonisolated private static func writeOpenRouterJSONBody(
        audioFileURL: URL,
        audioFormat: String,
        model: String,
        episodeDuration: Double?,
        thinkingLevel: AdDetectionThinkingLevel
    ) throws -> OpenRouterJSONBody {
        let placeholder = "NOADCAST_AUDIO_DATA_\(UUID().uuidString)"
        let durationContext = Self.durationPromptContext(episodeDuration)
        let userInstruction = ([
            "Analyze the complete attached podcast audio and produce the JSON object as specified.",
            durationContext
        ])
        .filter { !$0.isEmpty }
        .joined(separator: "\n\n")

        var body: [String: Any] = [
            "model": model,
            "messages": [
                [
                    "role": "system",
                    "content": Self.segmentsOnlyPrompt
                ],
                [
                    "role": "user",
                    "content": [
                        ["type": "text", "text": userInstruction],
                        [
                            "type": "input_audio",
                            "input_audio": [
                                "data": placeholder,
                                "format": audioFormat
                            ]
                        ]
                    ]
                ]
            ],
            "response_format": [
                "type": "json_schema",
                "json_schema": [
                    "name": "podcast_skip_segments",
                    "strict": true,
                    "schema": Self.openRouterResponseSchema
                ]
            ]
        ]
        if let effort = thinkingLevel.apiValue {
            body["reasoning"] = ["effort": effort]
        }

        let serialized = try JSONSerialization.data(withJSONObject: body)
        let placeholderData = Data(placeholder.utf8)
        guard let placeholderRange = serialized.range(of: placeholderData) else {
            throw CloudAdDetectionError.parseFailure("Couldn't construct OpenRouter audio request")
        }

        let bodyURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("noadcast-openrouter-\(UUID().uuidString).json")
        guard FileManager.default.createFile(atPath: bodyURL.path, contents: nil) else {
            throw CocoaError(.fileWriteUnknown)
        }

        let output: FileHandle
        do {
            output = try FileHandle(forWritingTo: bodyURL)
        } catch {
            try? FileManager.default.removeItem(at: bodyURL)
            throw error
        }
        var didCloseOutput = false
        do {
            try output.write(contentsOf: Data(serialized[..<placeholderRange.lowerBound]))
            try Self.writeBase64EncodedContents(of: audioFileURL, to: output)
            try output.write(contentsOf: Data(serialized[placeholderRange.upperBound...]))
            try output.close()
            didCloseOutput = true
        } catch {
            if !didCloseOutput { try? output.close() }
            try? FileManager.default.removeItem(at: bodyURL)
            throw error
        }

        let fileSize: Int
        do {
            guard let size = try bodyURL.resourceValues(forKeys: [.fileSizeKey]).fileSize else {
                throw CocoaError(.fileReadUnknown)
            }
            fileSize = size
        } catch {
            try? FileManager.default.removeItem(at: bodyURL)
            throw error
        }
        let byteCount = Int64(fileSize)
        return OpenRouterJSONBody(fileURL: bodyURL, byteCount: byteCount)
    }

    /// Streams base64 without loading a podcast-sized file into memory. A
    /// short read is not necessarily EOF, so carry its final 0–2 bytes into
    /// the next read and emit padding only once at the true end of the file.
    nonisolated static func writeBase64EncodedContents(
        of sourceURL: URL,
        to output: FileHandle,
        chunkSize: Int = 768 * 1024
    ) throws {
        precondition(chunkSize > 0)
        let input = try FileHandle(forReadingFrom: sourceURL)
        defer { try? input.close() }

        var remainder = Data()
        while true {
            try Task.checkCancellation()
            let chunk = try input.read(upToCount: chunkSize) ?? Data()
            guard !chunk.isEmpty else { break }

            var pending = Data()
            pending.reserveCapacity(remainder.count + chunk.count)
            pending.append(remainder)
            pending.append(chunk)

            let encodableCount = pending.count - (pending.count % 3)
            if encodableCount > 0 {
                let encoded = Data(pending.prefix(encodableCount)).base64EncodedData()
                try output.write(contentsOf: encoded)
            }
            remainder = Data(pending.dropFirst(encodableCount))
        }
        if !remainder.isEmpty {
            try output.write(contentsOf: remainder.base64EncodedData())
        }
    }

    nonisolated static func openRouterAudioFormat(
        mimeType: String,
        fileExtension: String
    ) -> String? {
        let normalizedMime = mimeType
            .split(separator: ";", maxSplits: 1)
            .first?
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased() ?? ""
        switch normalizedMime {
        case "audio/mpeg", "audio/mp3": return "mp3"
        case "audio/wav", "audio/x-wav", "audio/wave": return "wav"
        case "audio/mp4", "audio/m4a", "audio/x-m4a": return "m4a"
        case "audio/aac": return "aac"
        case "audio/aiff", "audio/x-aiff": return "aiff"
        case "audio/flac", "audio/x-flac": return "flac"
        case "audio/ogg": return "ogg"
        case "audio/webm": return "webm"
        default: break
        }

        switch fileExtension.lowercased() {
        case "mp3": return "mp3"
        case "wav": return "wav"
        case "m4a", "mp4": return "m4a"
        case "aac": return "aac"
        case "aif", "aiff": return "aiff"
        case "flac": return "flac"
        case "ogg", "oga": return "ogg"
        case "webm": return "webm"
        default: return nil
        }
    }

    nonisolated private static func openRouterHTTPError(
        statusCode: Int,
        body: String
    ) -> Error {
        let detail = String(body.prefix(500))
        let description: String
        switch statusCode {
        case 401:
            description = "OpenRouter rejected the API key. Check it in Settings → Detection model."
        case 413:
            description = "The OpenRouter audio request is too large. Enable upload downsampling and try again."
        case 429:
            description = "OpenRouter is rate-limiting requests. Try this episode again later."
        case 500...599:
            description = "OpenRouter is temporarily unavailable (HTTP \(statusCode)). \(detail)"
        default:
            description = "OpenRouter returned HTTP \(statusCode): \(detail)"
        }
        return NSError(
            domain: "OpenRouter",
            code: statusCode,
            userInfo: [NSLocalizedDescriptionKey: description]
        )
    }

    nonisolated private static func strippingJSONFence(from text: String) -> String {
        var value = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard value.hasPrefix("```") else { return value }
        if let firstNewline = value.firstIndex(of: "\n") {
            value = String(value[value.index(after: firstNewline)...])
        }
        if value.hasSuffix("```") {
            value.removeLast(3)
        }
        return value.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func serverAnalyzeURL(host: String, port: Int) throws -> URL {
        let trimmed = host.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, (1...65_535).contains(port) else {
            throw CloudAdDetectionError.invalidServerURL(host)
        }

        let withScheme = trimmed.contains("://") ? trimmed : "http://\(trimmed)"
        guard var components = URLComponents(string: withScheme),
              components.host != nil
        else {
            throw CloudAdDetectionError.invalidServerURL(host)
        }

        components.port = port
        components.query = nil
        components.fragment = nil
        if components.path.isEmpty || components.path == "/" {
            components.path = "/analyze"
        } else if !components.path.hasSuffix("/analyze") {
            components.path += components.path.hasSuffix("/") ? "analyze" : "/analyze"
        }

        guard let url = components.url else {
            throw CloudAdDetectionError.invalidServerURL(host)
        }
        return url
    }

    private func writeMultipartBody(
        fileURL: URL,
        fileFieldName: String,
        mimeType: String,
        fields: [String: String]
    ) throws -> MultipartBody {
        let boundary = "Noadcast-\(UUID().uuidString)"
        let bodyURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("noadcast-server-upload-\(UUID().uuidString).multipart")
        _ = FileManager.default.createFile(atPath: bodyURL.path, contents: nil)

        let output = try FileHandle(forWritingTo: bodyURL)
        var didCloseOutput = false
        defer {
            if !didCloseOutput {
                try? output.close()
            }
        }

        func write(_ string: String) throws {
            try output.write(contentsOf: Data(string.utf8))
        }

        for (name, value) in fields.sorted(by: { $0.key < $1.key }) {
            try write("--\(boundary)\r\n")
            try write("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n")
            try write("\(value)\r\n")
        }

        let filename = fileURL.lastPathComponent.isEmpty ? "episode-audio" : fileURL.lastPathComponent
        try write("--\(boundary)\r\n")
        try write("Content-Disposition: form-data; name=\"\(fileFieldName)\"; filename=\"\(filename)\"\r\n")
        try write("Content-Type: \(mimeType)\r\n\r\n")

        let input = try FileHandle(forReadingFrom: fileURL)
        defer { try? input.close() }
        while true {
            let chunk = try input.read(upToCount: 1024 * 1024) ?? Data()
            guard !chunk.isEmpty else { break }
            try output.write(contentsOf: chunk)
        }

        try write("\r\n--\(boundary)--\r\n")
        try output.close()
        didCloseOutput = true
        let byteCount = (try bodyURL.resourceValues(forKeys: [.fileSizeKey]).fileSize).map(Int64.init) ?? 0
        return MultipartBody(fileURL: bodyURL, boundary: boundary, byteCount: byteCount)
    }

    // MARK: - Optional upload downsampling

    private func prepareUploadAudio(
        fileURL: URL,
        mimeType: String,
        downsampleBeforeUpload: Bool
    ) async throws -> UploadAudio {
        guard downsampleBeforeUpload else {
            return UploadAudio(fileURL: fileURL, mimeType: mimeType, cleanupURL: nil)
        }
        do {
            let outputURL = try await Self.downsampleForUpload(fileURL)
            return UploadAudio(fileURL: outputURL, mimeType: "audio/mp4", cleanupURL: outputURL)
        } catch {
            throw CloudAdDetectionError.downsampleFailed(error)
        }
    }

    private static func downsampleForUpload(_ sourceURL: URL) async throws -> URL {
        try await Task.detached(priority: .utility) {
            let outputURL = FileManager.default.temporaryDirectory
                .appendingPathComponent("noadcast-upload-\(UUID().uuidString).m4a")
            try? FileManager.default.removeItem(at: outputURL)

            do {
                let asset = AVURLAsset(url: sourceURL)
                guard let track = try await asset.loadTracks(withMediaType: .audio).first else {
                    throw downsampleError("No audio track found.")
                }

                let reader = try AVAssetReader(asset: asset)
                let readerSettings: [String: Any] = [
                    AVFormatIDKey: kAudioFormatLinearPCM,
                    AVSampleRateKey: 16_000,
                    AVNumberOfChannelsKey: 1,
                    AVLinearPCMBitDepthKey: 16,
                    AVLinearPCMIsFloatKey: false,
                    AVLinearPCMIsBigEndianKey: false,
                    AVLinearPCMIsNonInterleaved: false,
                ]
                let readerOutput = AVAssetReaderTrackOutput(track: track, outputSettings: readerSettings)
                readerOutput.alwaysCopiesSampleData = false
                guard reader.canAdd(readerOutput) else {
                    throw downsampleError("Couldn't add audio reader output.")
                }
                reader.add(readerOutput)

                let writer = try AVAssetWriter(outputURL: outputURL, fileType: .m4a)
                let writerSettings: [String: Any] = [
                    AVFormatIDKey: kAudioFormatMPEG4AAC,
                    AVSampleRateKey: 16_000,
                    AVNumberOfChannelsKey: 1,
                    AVEncoderBitRateKey: 32_000,
                ]
                let writerInput = AVAssetWriterInput(mediaType: .audio, outputSettings: writerSettings)
                writerInput.expectsMediaDataInRealTime = false
                guard writer.canAdd(writerInput) else {
                    throw downsampleError("Couldn't add audio writer input.")
                }
                writer.add(writerInput)

                guard reader.startReading() else {
                    throw reader.error ?? downsampleError("Couldn't start audio reader.")
                }
                guard writer.startWriting() else {
                    reader.cancelReading()
                    throw writer.error ?? downsampleError("Couldn't start audio writer.")
                }
                writer.startSession(atSourceTime: .zero)

                let queue = DispatchQueue(label: "Noadcast.upload-downsample")
                let pump = DownsamplePump(
                    reader: reader,
                    readerOutput: readerOutput,
                    writer: writer,
                    writerInput: writerInput
                )
                try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                    pump.start(on: queue) { result in
                        continuation.resume(with: result)
                    }
                }
                return outputURL
            } catch {
                try? FileManager.default.removeItem(at: outputURL)
                throw error
            }
        }.value
    }

    private static func downsampleError(_ description: String) -> NSError {
        NSError(
            domain: "NoadcastAudioDownsample",
            code: 1,
            userInfo: [NSLocalizedDescriptionKey: description]
        )
    }

    private func shouldYieldUploadProgress(
        taskID: Int,
        bytesSent: Int64,
        totalBytes: Int64
    ) -> Bool {
        let now = ProcessInfo.processInfo.systemUptime
        lock.lock()
        defer { lock.unlock() }

        guard let previous = uploadProgressSnapshots[taskID] else {
            uploadProgressSnapshots[taskID] = TransferProgressSnapshot(
                lastYieldUptime: now,
                lastBytesSent: bytesSent,
                lastTotalBytes: totalBytes
            )
            return true
        }

        let isComplete = bytesSent >= totalBytes
        let totalChanged = previous.lastTotalBytes != totalBytes
        let elapsed = now - previous.lastYieldUptime
        let byteDelta = bytesSent - previous.lastBytesSent
        let fractionDelta = totalBytes > 0 ? Double(byteDelta) / Double(totalBytes) : 0
        let shouldYield = isComplete
            || totalChanged
            || (elapsed >= Self.progressThrottleInterval
                && (byteDelta >= Self.progressThrottleBytes
                    || fractionDelta >= Self.progressThrottleFraction))

        if shouldYield {
            uploadProgressSnapshots[taskID] = TransferProgressSnapshot(
                lastYieldUptime: now,
                lastBytesSent: bytesSent,
                lastTotalBytes: totalBytes
            )
        }
        return shouldYield
    }

    // MARK: - Gemini Files API (resumable upload)

    /// Two-step resumable upload to the Gemini Files API. Returned URI is
    /// valid for 48 hours which is plenty for the immediate follow-up
    /// `generateContent` call. Both legs run through the background
    /// `URLSession` so the upload keeps moving when the app is suspended,
    /// and byte progress is reported through `onStage(.uploading(...))`.
    private func uploadToGeminiFiles(
        fileURL: URL,
        mimeType: String,
        apiKey: String,
        taskDescription: String?,
        onStage: (@Sendable (CloudAdDetectionStage) -> Void)?
    ) async throws -> UploadedGeminiFile {
        let byteCount = (try? fileURL.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        Log.adDetection.info("Uploading \(byteCount) bytes to Gemini Files API")

        // Step 1 — start a resumable upload session.
        guard let startURL = URL(string: "https://generativelanguage.googleapis.com/upload/v1beta/files?key=\(apiKey)") else {
            throw CloudAdDetectionError.uploadFailed(URLError(.badURL))
        }
        var startRequest = URLRequest(url: startURL)
        startRequest.httpMethod = "POST"
        startRequest.setValue("resumable", forHTTPHeaderField: "X-Goog-Upload-Protocol")
        startRequest.setValue("start", forHTTPHeaderField: "X-Goog-Upload-Command")
        startRequest.setValue(String(byteCount), forHTTPHeaderField: "X-Goog-Upload-Header-Content-Length")
        startRequest.setValue(mimeType, forHTTPHeaderField: "X-Goog-Upload-Header-Content-Type")
        startRequest.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let displayName = fileURL.lastPathComponent
        let startBodyData = try JSONSerialization.data(withJSONObject: [
            "file": ["display_name": displayName]
        ])
        let startBodyURL = try writeTempBody(startBodyData)

        let (_, startResponse) = try await upload(
            request: startRequest,
            fromFile: startBodyURL,
            tempBodyURL: startBodyURL,
            taskDescription: taskDescription
        )
        guard (200..<300).contains(startResponse.statusCode),
              let uploadURLString = startResponse.value(forHTTPHeaderField: "X-Goog-Upload-URL"),
              let uploadURL = URL(string: uploadURLString)
        else {
            throw CloudAdDetectionError.uploadFailed(
                NSError(domain: "GeminiFiles", code: startResponse.statusCode,
                        userInfo: [NSLocalizedDescriptionKey: "Failed to start upload — HTTP \(startResponse.statusCode)"])
            )
        }

        // Step 2 — finalize: stream the audio file as the body. Going
        // through `fromFile:` keeps the whole MP3 off the heap, and the
        // session's `didSendBodyData` events get bridged to `onStage` so
        // the row's progress bar can show MB / MB.
        var uploadRequest = URLRequest(url: uploadURL)
        uploadRequest.httpMethod = "POST"
        uploadRequest.setValue(String(byteCount), forHTTPHeaderField: "Content-Length")
        uploadRequest.setValue("0", forHTTPHeaderField: "X-Goog-Upload-Offset")
        uploadRequest.setValue("upload, finalize", forHTTPHeaderField: "X-Goog-Upload-Command")
        let (data, uploadResponse) = try await upload(
            request: uploadRequest,
            fromFile: fileURL,
            taskDescription: taskDescription,
            onProgress: { sent, total in
                onStage?(.uploading(
                    bytesSent: sent,
                    totalBytes: total,
                    status: "Uploading audio to Gemini…"
                ))
            }
        )
        guard (200..<300).contains(uploadResponse.statusCode) else {
            let body = String(data: data, encoding: .utf8) ?? ""
            Log.adDetection.error("Gemini Files upload failed — HTTP \(uploadResponse.statusCode): \(body, privacy: .public)")
            throw CloudAdDetectionError.uploadFailed(
                NSError(domain: "GeminiFiles", code: uploadResponse.statusCode,
                        userInfo: [NSLocalizedDescriptionKey: "Upload failed — HTTP \(uploadResponse.statusCode)"])
            )
        }
        let decoded = try decoder.decode(GeminiFileUploadResponse.self, from: data)
        return UploadedGeminiFile(
            uri: decoded.file.uri,
            name: decoded.file.name ?? Self.fileName(fromURI: decoded.file.uri)
        )
    }

    private func deleteGeminiFile(_ file: UploadedGeminiFile, apiKey: String) async {
        guard let name = file.name, !name.isEmpty else {
            Log.adDetection.notice("Skipping Gemini file delete because response did not include a file name")
            return
        }
        guard let encodedName = name.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed),
              let url = URL(string: "https://generativelanguage.googleapis.com/v1beta/\(encodedName)?key=\(apiKey)")
        else {
            Log.adDetection.error("Couldn't build Gemini file delete URL for \(name, privacy: .public)")
            return
        }

        var request = URLRequest(url: url)
        request.httpMethod = "DELETE"
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                Log.adDetection.error("Gemini file delete failed: missing HTTP response")
                return
            }
            guard (200..<300).contains(http.statusCode) else {
                let body = String(data: data, encoding: .utf8) ?? ""
                Log.adDetection.error("Gemini file delete failed — HTTP \(http.statusCode): \(body, privacy: .public)")
                return
            }
            Log.adDetection.info("Deleted Gemini file \(name, privacy: .public)")
        } catch {
            Log.adDetection.error("Gemini file delete failed: \(Log.describe(error), privacy: .public)")
        }
    }

    private static func fileName(fromURI uri: String) -> String? {
        if uri.hasPrefix("files/") {
            return uri
        }
        guard let marker = uri.range(of: "/files/") else { return nil }
        let fileID = uri[marker.upperBound...]
        guard !fileID.isEmpty else { return nil }
        return "files/\(fileID)"
    }

    // MARK: - generateContent with file_data reference

    private func callGeminiCombined(
        model: String,
        fileURI: String,
        mimeType: String,
        episodeDuration: Double?,
        apiKey: String,
        thinkingLevel: AdDetectionThinkingLevel,
        taskDescription: String?
    ) async throws -> ([DetectedAd], TokenUsage?) {
        let instruction = ([
            "Produce the JSON object as specified.",
            Self.durationPromptContext(episodeDuration)
        ])
        .filter { !$0.isEmpty }
        .joined(separator: "\n\n")
        let parts: [[String: Any]] = [
            ["file_data": ["mime_type": mimeType, "file_uri": fileURI]],
            ["text": instruction]
        ]
        // Body is tiny (just the URI reference), so we don't bother
        // reporting upload byte progress here. The outer pipeline has
        // already flipped to `.analyzing` before this call.
        return try await postCombined(
            model: model,
            parts: parts,
            apiKey: apiKey,
            thinkingLevel: thinkingLevel,
            taskDescription: taskDescription
        )
    }

    private func callGeminiTranscript(
        model: String,
        transcript: [TimestampedTranscriptSegment],
        episodeDuration: Double?,
        apiKey: String,
        thinkingLevel: AdDetectionThinkingLevel,
        taskDescription: String?
    ) async throws -> ([DetectedAd], TokenUsage?) {
        let transcriptText = Self.formattedTranscript(transcript)
        let endpointGuidance = Self.transcriptEndpointGuidance(
            episodeDuration: episodeDuration,
            transcriptEnd: transcript.map(\.endSeconds).max()
        )
        let parts: [[String: Any]] = [
            [
                "text": """
                Classify only the following timestamped transcript. Segment \
                starts and all non-outro timestamps must stay within these \
                transcript ranges.

                \(endpointGuidance)

                \(transcriptText)
                """
            ]
        ]
        return try await postCombined(
            model: model,
            parts: parts,
            apiKey: apiKey,
            thinkingLevel: thinkingLevel,
            taskDescription: taskDescription,
            systemPrompt: Self.transcriptSegmentsPrompt
        )
    }

    // MARK: - Shared request body + parsing

    /// Posts a `generateContent` request whose user content is `parts`
    /// and parses the structured-JSON response.
    private func postCombined(
        model: String,
        parts: [[String: Any]],
        apiKey: String,
        thinkingLevel: AdDetectionThinkingLevel,
        taskDescription: String?,
        systemPrompt: String? = nil
    ) async throws -> ([DetectedAd], TokenUsage?) {
        guard let url = URL(string: "https://generativelanguage.googleapis.com/v1beta/models/\(model):generateContent?key=\(apiKey)") else {
            throw CloudAdDetectionError.uploadFailed(URLError(.badURL))
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        var generationConfig: [String: Any] = [
            "responseMimeType": "application/json",
            "responseSchema": Self.responseSchema
        ]
        if let apiValue = thinkingLevel.apiValue {
            generationConfig["thinkingConfig"] = ["thinkingLevel": apiValue]
        }

        let body: [String: Any] = [
            "systemInstruction": ["parts": [["text": systemPrompt ?? Self.segmentsOnlyPrompt]]],
            "contents": [
                ["role": "user", "parts": parts]
            ],
            "generationConfig": generationConfig
        ]
        let bodyData = try JSONSerialization.data(withJSONObject: body)
        let bodyURL = try writeTempBody(bodyData)

        let (data, response) = try await upload(
            request: request,
            fromFile: bodyURL,
            tempBodyURL: bodyURL,
            taskDescription: taskDescription
        )
        if !(200..<300).contains(response.statusCode) {
            let bodyText = String(data: data, encoding: .utf8) ?? ""
            Log.adDetection.error("Gemini combined call HTTP \(response.statusCode): \(bodyText, privacy: .public)")
            throw CloudAdDetectionError.uploadFailed(
                NSError(domain: "Gemini", code: response.statusCode,
                        userInfo: [NSLocalizedDescriptionKey: "HTTP \(response.statusCode): \(bodyText.prefix(500))"])
            )
        }
        let decoded = try decoder.decode(GeminiResponse.self, from: data)
        guard let text = decoded.candidates.first?.content.parts.first?.text else {
            throw CloudAdDetectionError.parseFailure("Missing response text")
        }
        let ads: [DetectedAd]
        do {
            let parsed = try JSONDecoder().decode(SegmentsOnlyResponse.self, from: Data(text.utf8))
            ads = Self.detectedAds(from: parsed.segments)
        } catch {
            throw CloudAdDetectionError.parseFailure(error.localizedDescription)
        }
        let usage = decoded.usageMetadata.map {
            TokenUsage(
                inputTokens: $0.promptTokenCount ?? 0,
                thoughtTokens: $0.thoughtsTokenCount ?? 0,
                outputTokens: $0.candidatesTokenCount ?? 0
            )
        }
        return (ads.sorted { $0.startSeconds < $1.startSeconds }, usage)
    }

    private static func formattedTranscript(_ transcript: [TimestampedTranscriptSegment]) -> String {
        transcript.map { segment in
            let text = segment.text
                .replacingOccurrences(of: "\n", with: " ")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            return String(
                format: "[%.2f - %.2f] %@",
                segment.startSeconds,
                segment.endSeconds,
                text
            )
        }
        .joined(separator: "\n")
    }

    private static let responseSchema: [String: Any] = {
        let segmentSchema: [String: Any] = [
            "type": "OBJECT",
            "properties": [
                "startSeconds": ["type": "NUMBER"],
                "endSeconds": ["type": "NUMBER"],
                "summary": ["type": "STRING"],
                "kind": [
                    "type": "STRING",
                    "enum": ["ad", "intro", "outro"]
                ]
            ],
            "required": ["startSeconds", "endSeconds", "summary", "kind"]
        ]
        return [
            "type": "OBJECT",
            "properties": [
                "segments": [
                    "type": "ARRAY",
                    "items": segmentSchema
                ]
            ],
            "required": ["segments"]
        ]
    }()

    private static let openRouterResponseSchema: [String: Any] = {
        let segmentSchema: [String: Any] = [
            "type": "object",
            "properties": [
                "startSeconds": ["type": "number"],
                "endSeconds": ["type": "number"],
                "summary": ["type": "string"],
                "kind": [
                    "type": "string",
                    "enum": ["ad", "intro", "outro"]
                ]
            ],
            "required": ["startSeconds", "endSeconds", "summary", "kind"],
            "additionalProperties": false
        ]
        return [
            "type": "object",
            "properties": [
                "segments": [
                    "type": "array",
                    "items": segmentSchema
                ]
            ],
            "required": ["segments"],
            "additionalProperties": false
        ]
    }()

    private static func detectedAds(from rows: [CombinedResponse.SegmentRow]) -> [DetectedAd] {
        rows.compactMap { row -> DetectedAd? in
            guard row.endSeconds > row.startSeconds else { return nil }
            let kind = SegmentKind(rawValue: row.kind) ?? .ad
            return DetectedAd(
                startSeconds: row.startSeconds,
                endSeconds: row.endSeconds,
                summary: row.summary,
                kind: kind
            ).sanitized(episodeDuration: nil)
        }
    }
}

// MARK: - URLSession delegate

extension CloudAdDetectionService: @preconcurrency URLSessionDataDelegate {
    func urlSession(
        _ session: URLSession,
        dataTask: URLSessionDataTask,
        didReceive data: Data
    ) {
        lock.lock()
        pending[dataTask.taskIdentifier]?.receivedData.append(data)
        lock.unlock()
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didSendBodyData _: Int64,
        totalBytesSent: Int64,
        totalBytesExpectedToSend: Int64
    ) {
        guard totalBytesExpectedToSend > 0 else { return }
        guard shouldYieldUploadProgress(
            taskID: task.taskIdentifier,
            bytesSent: totalBytesSent,
            totalBytes: totalBytesExpectedToSend
        ) else {
            return
        }
        lock.lock()
        let handler = pending[task.taskIdentifier]?.progressHandler
        lock.unlock()
        handler?(totalBytesSent, totalBytesExpectedToSend)
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didCompleteWithError error: Error?
    ) {
        lock.lock()
        let entry = pending.removeValue(forKey: task.taskIdentifier)
        uploadProgressSnapshots.removeValue(forKey: task.taskIdentifier)
        lock.unlock()
        if let tempBody = entry?.bodyFileURL {
            try? FileManager.default.removeItem(at: tempBody)
        }
        if let error {
            let urlStr = task.originalRequest?.url?.absoluteString ?? "?"
            Log.adDetection.error("Cloud task failed url=\(urlStr, privacy: .public) \(Log.describe(error), privacy: .public)")
            entry?.completion(.failure(error))
            return
        }
        guard let http = task.response as? HTTPURLResponse else {
            entry?.completion(.failure(URLError(.badServerResponse)))
            return
        }
        entry?.completion(.success((entry?.receivedData ?? Data(), http)))
    }

    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        lock.lock()
        let pending = pendingBackgroundCompletion
        pendingBackgroundCompletion = nil
        lock.unlock()
        DispatchQueue.main.async { pending?.handler() }
    }
}

/// Holds a non-`Sendable` UIKit completion handler so it can be stored on
/// the service (which is `@unchecked Sendable`) and invoked later on the
/// main queue. The handler is set once at construction (`let`), so reading
/// it from any thread is safe.
nonisolated private final class BackgroundCompletion: @unchecked Sendable {
    let handler: () -> Void
    init(_ handler: @escaping () -> Void) { self.handler = handler }
}

// MARK: - Decodable shapes

nonisolated private struct CombinedResponse {
    struct SegmentRow: Decodable {
        let startSeconds: Double
        let endSeconds: Double
        let summary: String
        let kind: String
    }
}

nonisolated private struct SegmentsOnlyResponse: Decodable {
    let segments: [CombinedResponse.SegmentRow]
}

nonisolated private struct ServerAdDetectionResponse: Decodable {
    let segments: [CombinedResponse.SegmentRow]
    let usage: TokenUsage?
}

nonisolated private struct GeminiFileUploadResponse: Decodable {
    let file: FileInfo
    struct FileInfo: Decodable {
        let uri: String
        let mimeType: String?
        let name: String?
    }
}

nonisolated private struct GeminiResponse: Decodable {
    let candidates: [Candidate]
    let usageMetadata: UsageMetadata?
    struct Candidate: Decodable {
        let content: Content
        struct Content: Decodable {
            let parts: [Part]
            struct Part: Decodable {
                let text: String
            }
        }
    }
    struct UsageMetadata: Decodable {
        let promptTokenCount: Int?
        let thoughtsTokenCount: Int?
        let candidatesTokenCount: Int?
    }
}

nonisolated private struct OpenRouterResponse: Decodable {
    let choices: [Choice]
    let usage: Usage?

    struct Choice: Decodable {
        let message: Message
    }

    struct Message: Decodable {
        let content: String?

        private enum CodingKeys: String, CodingKey {
            case content
        }

        private struct ContentPart: Decodable {
            let text: String?
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            if let text = try? container.decode(String.self, forKey: .content) {
                content = text
                return
            }
            if let parts = try? container.decode([ContentPart].self, forKey: .content) {
                let joined = parts.compactMap(\.text).joined()
                content = joined.isEmpty ? nil : joined
                return
            }
            content = nil
        }
    }

    struct Usage: Decodable {
        let promptTokens: Int?
        let completionTokens: Int?
        let completionTokensDetails: CompletionTokensDetails?

        enum CodingKeys: String, CodingKey {
            case promptTokens = "prompt_tokens"
            case completionTokens = "completion_tokens"
            case completionTokensDetails = "completion_tokens_details"
        }
    }

    struct CompletionTokensDetails: Decodable {
        let reasoningTokens: Int?

        enum CodingKeys: String, CodingKey {
            case reasoningTokens = "reasoning_tokens"
        }
    }
}
