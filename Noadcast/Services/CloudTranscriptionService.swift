import Foundation
import AVFoundation
import os

/// One-shot file-analysis helper: uploads the entire audio file to a Gemini
/// model and gets back skip segments in one structured-JSON response.
///
/// Replaces the `SpeechAnalyzer` + `AdDetectionService` two-step for the
/// file-upload detection modes. Wins:
/// * No on-device transcription — no CPU spike, no 79-min `SpeechAnalyzer`
///   crash, no chunking.
/// * Just an HTTP upload + response, so the whole pipeline rides on a
///   **background** `URLSession` and keeps moving while the app is
///   suspended.
/// * The model has access to actual audio cues (music stings, voice
///   changes), which makes intros / outros / ad reads more obvious than
///   from text alone.
///
/// Costs: token spend goes up ~10–30× per episode (audio frames count as
/// input tokens), and bandwidth depends on whether Settings uses the
/// original playback file or a temporary downsampled upload copy.
nonisolated struct CloudTranscriptionResult: Sendable {
    let ads: [DetectedAd]
    let usage: TokenUsage?
}

/// Per-stage signal the pipeline subscribes to so it can flip
/// `Episode.processingState` and surface upload byte counts in the UI.
nonisolated enum CloudTranscriptionStage: Sendable {
    /// Bytes have started moving. `totalBytes` is the request body size as
    /// reported by `URLSession` during the Gemini Files upload.
    case uploading(bytesSent: Int64, totalBytes: Int64)
    /// Upload finished; we're now waiting on the LLM to produce the
    /// structured response.
    case analyzing
}

enum CloudTranscriptionError: LocalizedError {
    case providerUnsupported(String)
    case missingAPIKey(String)
    case downsampleFailed(Error)
    case uploadFailed(Error)
    case parseFailure(String)

    var errorDescription: String? {
        switch self {
        case .providerUnsupported(let provider):
            "\(provider) doesn't support file-based analysis. Pick a different Gemini model in Settings → Detection model."
        case .missingAPIKey(let provider):
            "\(provider) API key missing — add one in Settings → Detection model."
        case .downsampleFailed(let err):
            "Couldn't downsample the audio file: \(err.localizedDescription)"
        case .uploadFailed(let err):
            "Couldn't upload the audio file: \(err.localizedDescription)"
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
nonisolated final class CloudTranscriptionService: NSObject, @unchecked Sendable {
    static let shared = CloudTranscriptionService()

    static let backgroundSessionIdentifier = "com.isaackhor.Noadcast.background-cloud"

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
                                .failure(writer.error ?? CloudTranscriptionService.downsampleError("Couldn't append audio sample.")),
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
                return .failure(reader.error ?? CloudTranscriptionService.downsampleError("Audio reader failed."))
            }
            if writer.status == .failed || writer.status == .cancelled {
                return .failure(writer.error ?? CloudTranscriptionService.downsampleError("Audio writer failed."))
            }
            guard writer.status == .completed else {
                return .failure(CloudTranscriptionService.downsampleError("Audio writer ended in state \(writer.status.rawValue)."))
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
    substantive content finishes through to the end of the audio. Intros \
    and outros may include ads — they're still a single intro/outro \
    segment, not separate entries.

    - "ad": a mid-episode advertisement, sponsored message, host-read ad, \
    promo code, paid endorsement, or cross-promotion of another podcast \
    that appears BETWEEN the intro and outro. Editorial mentions, \
    listener mail, the host's own products discussed editorially, and \
    interview segments are NOT ads.

    Use only timestamps that match the audio. Be conservative — flag segments \
    only when you're confident. Return an empty `segments` array if \
    nothing should be skipped. Do not include transcript text or any fields \
    other than `segments`.
    """

    /// Top-level entry point. Always uploads the audio through Gemini's
    /// resumable Files API, then references the resulting file URI in the
    /// follow-up `generateContent` call. `onStage` fires with
    /// `.uploading(sent, total)` continuously as bytes go up, then once
    /// with `.analyzing` while we wait on the LLM.
    ///
    /// `episodeGUID`, when supplied, is set as each task's
    /// `taskDescription` so that `cancelTasks(forEpisodeGUID:)` can find
    /// and cancel orphaned tasks belonging to a given episode after an
    /// app termination + relaunch.
    func analyzeFile(
        fileURL: URL,
        provider: AdDetectionProvider,
        googleAPIKey: String?,
        mimeType: String,
        downsampleBeforeUpload: Bool = false,
        episodeGUID: String? = nil,
        onStage: (@Sendable (CloudTranscriptionStage) -> Void)? = nil
    ) async throws -> CloudTranscriptionResult {
        guard provider.supportsCloudTranscription else {
            throw CloudTranscriptionError.providerUnsupported(provider.label)
        }
        guard let key = googleAPIKey, !key.isEmpty else {
            throw CloudTranscriptionError.missingAPIKey(provider.label)
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
        Log.adDetection.info("Cloud analysis begin — provider=\(provider.label, privacy: .public) file=\(uploadAudio.fileURL.lastPathComponent, privacy: .public) bytes=\(fileSize) downsampled=\(downsampleBeforeUpload) path=files-api")

        let uploadedFile = try await uploadToGeminiFiles(
            fileURL: uploadAudio.fileURL,
            mimeType: uploadAudio.mimeType,
            apiKey: key,
            taskDescription: episodeGUID,
            onStage: onStage
        )
        onStage?(.analyzing)
        let ads: [DetectedAd]
        let usage: TokenUsage?
        do {
            (ads, usage) = try await callGeminiCombined(
                model: provider.apiModel,
                fileURI: uploadedFile.uri,
                mimeType: uploadAudio.mimeType,
                apiKey: key,
                taskDescription: episodeGUID
            )
        } catch {
            await deleteGeminiFile(uploadedFile, apiKey: key)
            throw error
        }
        await deleteGeminiFile(uploadedFile, apiKey: key)

        Log.adDetection.info("Cloud analysis complete — ads=\(ads.count) input_tokens=\(usage?.inputTokens ?? 0) thought_tokens=\(usage?.thoughtTokens ?? 0) output_tokens=\(usage?.outputTokens ?? 0)")
        return CloudTranscriptionResult(ads: ads, usage: usage)
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
            throw CloudTranscriptionError.downsampleFailed(error)
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
        onStage: (@Sendable (CloudTranscriptionStage) -> Void)?
    ) async throws -> UploadedGeminiFile {
        let byteCount = (try? fileURL.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        Log.adDetection.info("Uploading \(byteCount) bytes to Gemini Files API")

        // Step 1 — start a resumable upload session.
        guard let startURL = URL(string: "https://generativelanguage.googleapis.com/upload/v1beta/files?key=\(apiKey)") else {
            throw CloudTranscriptionError.uploadFailed(URLError(.badURL))
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
            throw CloudTranscriptionError.uploadFailed(
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
                onStage?(.uploading(bytesSent: sent, totalBytes: total))
            }
        )
        guard (200..<300).contains(uploadResponse.statusCode) else {
            let body = String(data: data, encoding: .utf8) ?? ""
            Log.adDetection.error("Gemini Files upload failed — HTTP \(uploadResponse.statusCode): \(body, privacy: .public)")
            throw CloudTranscriptionError.uploadFailed(
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
        apiKey: String,
        taskDescription: String?
    ) async throws -> ([DetectedAd], TokenUsage?) {
        let parts: [[String: Any]] = [
            ["file_data": ["mime_type": mimeType, "file_uri": fileURI]],
            ["text": "Produce the JSON object as specified."]
        ]
        // Body is tiny (just the URI reference), so we don't bother
        // reporting upload byte progress here. The outer pipeline has
        // already flipped to `.analyzing` before this call.
        return try await postCombined(
            model: model,
            parts: parts,
            apiKey: apiKey,
            taskDescription: taskDescription
        )
    }

    // MARK: - Shared request body + parsing

    /// Posts a `generateContent` request whose user content is `parts`
    /// and parses the structured-JSON response.
    private func postCombined(
        model: String,
        parts: [[String: Any]],
        apiKey: String,
        taskDescription: String?
    ) async throws -> ([DetectedAd], TokenUsage?) {
        guard let url = URL(string: "https://generativelanguage.googleapis.com/v1beta/models/\(model):generateContent?key=\(apiKey)") else {
            throw CloudTranscriptionError.uploadFailed(URLError(.badURL))
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let body: [String: Any] = [
            "systemInstruction": ["parts": [["text": Self.segmentsOnlyPrompt]]],
            "contents": [
                ["role": "user", "parts": parts]
            ],
            "generationConfig": [
                "responseMimeType": "application/json",
                "responseSchema": Self.responseSchema
            ]
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
            throw CloudTranscriptionError.uploadFailed(
                NSError(domain: "Gemini", code: response.statusCode,
                        userInfo: [NSLocalizedDescriptionKey: "HTTP \(response.statusCode): \(bodyText.prefix(500))"])
            )
        }
        let decoded = try decoder.decode(GeminiResponse.self, from: data)
        guard let text = decoded.candidates.first?.content.parts.first?.text else {
            throw CloudTranscriptionError.parseFailure("Missing response text")
        }
        let ads: [DetectedAd]
        do {
            let parsed = try JSONDecoder().decode(SegmentsOnlyResponse.self, from: Data(text.utf8))
            ads = Self.detectedAds(from: parsed.segments)
        } catch {
            throw CloudTranscriptionError.parseFailure(error.localizedDescription)
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

    private static func detectedAds(from rows: [CombinedResponse.SegmentRow]) -> [DetectedAd] {
        rows.compactMap { row -> DetectedAd? in
            guard row.endSeconds > row.startSeconds else { return nil }
            let kind = SegmentKind(rawValue: row.kind) ?? .ad
            return DetectedAd(
                startSeconds: row.startSeconds,
                endSeconds: row.endSeconds,
                summary: row.summary,
                kind: kind
            )
        }
    }
}

// MARK: - URLSession delegate

extension CloudTranscriptionService: @preconcurrency URLSessionDataDelegate {
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
