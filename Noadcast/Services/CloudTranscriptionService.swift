import Foundation
import os

/// One-shot cloud pipeline replacement: uploads the entire audio file to a
/// Gemini model and gets back both a timestamped transcript and labelled
/// ad/intro/outro segments in a single structured-JSON response.
///
/// Replaces the `SpeechAnalyzer` + `AdDetectionService` two-step when
/// `AppSettings.useCloudTranscription` is on. Wins:
/// * No on-device transcription — no CPU spike, no 79-min `SpeechAnalyzer`
///   crash, no chunking.
/// * Just an HTTP upload + response, so the whole pipeline fits inside
///   `URLSessionConfiguration.background` semantics if we wire it up there
///   later.
/// * The model has access to actual audio cues (music stings, voice
///   changes), which makes intros / outros / ad reads more obvious than
///   from text alone.
///
/// Costs: token spend goes up ~10–30× per episode (audio frames count as
/// input tokens), and ~2× bandwidth (we download the MP3 for local
/// playback, then upload it).
nonisolated struct CloudTranscriptionResult: Sendable {
    let transcript: [TranscribedSegment]
    let ads: [DetectedAd]
    let usage: TokenUsage?
}

/// Per-stage signal the pipeline subscribes to so it can flip
/// `Episode.processingState` and surface upload byte counts in the UI.
nonisolated enum CloudTranscriptionStage: Sendable {
    /// Bytes have started moving. `totalBytes` is the request body size as
    /// reported by `URLSession` (matches the audio file's size in the
    /// Files-API path, or the base64-padded JSON body inline).
    case uploading(bytesSent: Int64, totalBytes: Int64)
    /// Upload finished; we're now waiting on the LLM to produce the
    /// transcript + segments response.
    case analyzing
}

enum CloudTranscriptionError: LocalizedError {
    case providerUnsupported(String)
    case missingAPIKey(String)
    case uploadFailed(Error)
    case parseFailure(String)

    var errorDescription: String? {
        switch self {
        case .providerUnsupported(let provider):
            "\(provider) doesn't support cloud transcription. Pick a Gemini model in Settings → Detection model, or turn off cloud transcription."
        case .missingAPIKey(let provider):
            "\(provider) API key missing — add one in Settings → Detection model."
        case .uploadFailed(let err):
            "Couldn't upload the audio file: \(err.localizedDescription)"
        case .parseFailure(let msg):
            "Couldn't parse the provider response: \(msg)"
        }
    }
}

actor CloudTranscriptionService {
    static let shared = CloudTranscriptionService()

    /// Gemini's `generateContent` request payload caps at 20 MB total.
    /// Inline `inlineData` is base64-encoded, which inflates raw bytes by
    /// ~33%, plus there's the JSON envelope and `responseSchema`. Use a
    /// conservative 15 MB raw-audio threshold below which we send inline
    /// and skip the Files API upload roundtrip; above it, two-step
    /// resumable upload.
    private static let inlineThresholdBytes: Int64 = 15 * 1024 * 1024

    private let urlSession: URLSession = .shared
    private let decoder = JSONDecoder()

    /// System prompt for the combined transcribe + label call. Reuses the
    /// segment-classification rules from `AdDetectionService` and adds a
    /// transcript-shape requirement.
    nonisolated static let combinedPrompt: String = """
    You are analyzing a podcast episode audio file. Produce two outputs in \
    a single JSON object:

    1) `transcript`: a verbatim, timestamped transcript of the audio. Each \
    entry covers one sentence (or a short clause if the speaker pauses), \
    with `startSeconds` and `endSeconds` matching the actual time in the \
    audio and `text` containing exactly what was said. Use punctuation and \
    capitalization. Do not paraphrase.

    2) `segments`: every contiguous portion of the audio the listener would \
    want to skip. Each segment has a `kind`:

    - "intro": one contiguous segment at the very BEGINNING of the episode \
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
    nothing should be skipped.
    """

    /// Top-level entry point. For files under the inline threshold, embeds
    /// the audio directly in the `generateContent` request (single round-
    /// trip). For larger files, runs a resumable Files API upload first
    /// and references the resulting URI. `onStage` fires with
    /// `.uploading(sent, total)` continuously as bytes go up, then once
    /// with `.analyzing` while we wait on the LLM.
    func transcribeAndDetect(
        fileURL: URL,
        provider: AdDetectionProvider,
        googleAPIKey: String?,
        mimeType: String,
        onStage: (@Sendable (CloudTranscriptionStage) -> Void)? = nil
    ) async throws -> CloudTranscriptionResult {
        guard provider.supportsCloudTranscription else {
            throw CloudTranscriptionError.providerUnsupported(provider.label)
        }
        guard let key = googleAPIKey, !key.isEmpty else {
            throw CloudTranscriptionError.missingAPIKey(provider.label)
        }

        let fileSize = (try? fileURL.resourceValues(forKeys: [.fileSizeKey]).fileSize).map(Int64.init) ?? 0
        let useInline = fileSize > 0 && fileSize <= Self.inlineThresholdBytes
        Log.adDetection.info("Cloud transcription begin — provider=\(provider.label, privacy: .public) file=\(fileURL.lastPathComponent, privacy: .public) bytes=\(fileSize) path=\(useInline ? "inline" : "files-api", privacy: .public)")

        let transcript: [TranscribedSegment]
        let ads: [DetectedAd]
        let usage: TokenUsage?

        if useInline {
            (transcript, ads, usage) = try await callGeminiInline(
                model: provider.apiModel,
                fileURL: fileURL,
                mimeType: mimeType,
                apiKey: key,
                onStage: onStage
            )
        } else {
            let fileURI = try await uploadToGeminiFiles(
                fileURL: fileURL,
                mimeType: mimeType,
                apiKey: key,
                onStage: onStage
            )
            onStage?(.analyzing)
            (transcript, ads, usage) = try await callGeminiCombined(
                model: provider.apiModel,
                fileURI: fileURI,
                mimeType: mimeType,
                apiKey: key
            )
        }

        Log.adDetection.info("Cloud transcription complete — transcript=\(transcript.count) ads=\(ads.count) input_tokens=\(usage?.inputTokens ?? 0) output_tokens=\(usage?.outputTokens ?? 0)")
        return CloudTranscriptionResult(transcript: transcript, ads: ads, usage: usage)
    }

    // MARK: - Inline path (file ≤ 15 MB)

    /// Single-call inline variant. Reads the file, base64-encodes it as an
    /// `inlineData` part, writes the whole JSON body to a temp file so
    /// `URLSession.upload(for:fromFile:delegate:)` can report MB progress
    /// during the send, then parses the structured response. Saves a
    /// network roundtrip vs the Files-API path for small episodes.
    private func callGeminiInline(
        model: String,
        fileURL: URL,
        mimeType: String,
        apiKey: String,
        onStage: (@Sendable (CloudTranscriptionStage) -> Void)?
    ) async throws -> ([TranscribedSegment], [DetectedAd], TokenUsage?) {
        let fileData: Data
        do {
            fileData = try Data(contentsOf: fileURL)
        } catch {
            throw CloudTranscriptionError.uploadFailed(error)
        }
        let base64 = fileData.base64EncodedString()
        let parts: [[String: Any]] = [
            ["inlineData": ["mimeType": mimeType, "data": base64]],
            ["text": "Produce the JSON object as specified."]
        ]
        return try await postCombined(
            model: model,
            parts: parts,
            apiKey: apiKey,
            onStage: onStage
        )
    }

    // MARK: - Gemini Files API (resumable upload)

    /// Two-step resumable upload to the Gemini Files API. The inline-data
    /// path caps at 20 MB and most podcasts blow past that, so larger
    /// files come here. Returned URI is valid for 48 hours which is
    /// plenty for the immediate follow-up `generateContent` call. Reports
    /// byte progress through `onStage(.uploading(...))` so the row's
    /// progress bar can show MB / MB.
    private func uploadToGeminiFiles(
        fileURL: URL,
        mimeType: String,
        apiKey: String,
        onStage: (@Sendable (CloudTranscriptionStage) -> Void)?
    ) async throws -> String {
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
        startRequest.httpBody = try JSONSerialization.data(withJSONObject: [
            "file": ["display_name": displayName]
        ])

        let (_, startResponse) = try await urlSession.data(for: startRequest)
        guard let http = startResponse as? HTTPURLResponse,
              (200..<300).contains(http.statusCode),
              let uploadURLString = http.value(forHTTPHeaderField: "X-Goog-Upload-URL"),
              let uploadURL = URL(string: uploadURLString)
        else {
            let status = (startResponse as? HTTPURLResponse)?.statusCode ?? -1
            throw CloudTranscriptionError.uploadFailed(
                NSError(domain: "GeminiFiles", code: status,
                        userInfo: [NSLocalizedDescriptionKey: "Failed to start upload — HTTP \(status)"])
            )
        }

        // Step 2 — finalize: stream the audio file as the body. Going
        // through `fromFile:` (not `from: Data`) keeps the whole MP3 off
        // the heap, and pairing it with `UploadProgressDelegate` gives
        // us `didSendBodyData` events that the UI surfaces as MB / MB.
        var uploadRequest = URLRequest(url: uploadURL)
        uploadRequest.httpMethod = "POST"
        uploadRequest.setValue(String(byteCount), forHTTPHeaderField: "Content-Length")
        uploadRequest.setValue("0", forHTTPHeaderField: "X-Goog-Upload-Offset")
        uploadRequest.setValue("upload, finalize", forHTTPHeaderField: "X-Goog-Upload-Command")
        let progressDelegate = UploadProgressDelegate(onStage: onStage)
        let (data, uploadResponse) = try await urlSession.upload(
            for: uploadRequest,
            fromFile: fileURL,
            delegate: progressDelegate
        )
        guard let httpUp = uploadResponse as? HTTPURLResponse, (200..<300).contains(httpUp.statusCode) else {
            let status = (uploadResponse as? HTTPURLResponse)?.statusCode ?? -1
            let body = String(data: data, encoding: .utf8) ?? ""
            Log.adDetection.error("Gemini Files upload failed — HTTP \(status): \(body, privacy: .public)")
            throw CloudTranscriptionError.uploadFailed(
                NSError(domain: "GeminiFiles", code: status,
                        userInfo: [NSLocalizedDescriptionKey: "Upload failed — HTTP \(status)"])
            )
        }
        let decoded = try decoder.decode(GeminiFileUploadResponse.self, from: data)
        return decoded.file.uri
    }

    // MARK: - generateContent with file_data reference

    private func callGeminiCombined(
        model: String,
        fileURI: String,
        mimeType: String,
        apiKey: String
    ) async throws -> ([TranscribedSegment], [DetectedAd], TokenUsage?) {
        let parts: [[String: Any]] = [
            ["file_data": ["mime_type": mimeType, "file_uri": fileURI]],
            ["text": "Produce the JSON object as specified."]
        ]
        // Body is tiny (just the URI reference), so we don't bother
        // reporting upload byte progress here. The outer pipeline has
        // already flipped to `.analyzing` before this call.
        return try await postCombined(model: model, parts: parts, apiKey: apiKey, onStage: nil)
    }

    // MARK: - Shared request body + parsing

    /// Posts a `generateContent` request whose user content is `parts`
    /// (file_data reference, inline data, or whatever the caller assembled)
    /// and parses the structured-JSON response. If `onStage` is non-nil,
    /// the JSON body is written to a temp file and the upload is streamed
    /// through `URLSession.upload(for:fromFile:delegate:)` so the delegate
    /// can emit byte-progress events; for tiny bodies (file_uri only) we
    /// just send the bytes inline.
    private func postCombined(
        model: String,
        parts: [[String: Any]],
        apiKey: String,
        onStage: (@Sendable (CloudTranscriptionStage) -> Void)?
    ) async throws -> ([TranscribedSegment], [DetectedAd], TokenUsage?) {
        guard let url = URL(string: "https://generativelanguage.googleapis.com/v1beta/models/\(model):generateContent?key=\(apiKey)") else {
            throw CloudTranscriptionError.uploadFailed(URLError(.badURL))
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let body: [String: Any] = [
            "systemInstruction": ["parts": [["text": Self.combinedPrompt]]],
            "contents": [
                ["role": "user", "parts": parts]
            ],
            "generationConfig": [
                "responseMimeType": "application/json",
                "responseSchema": [
                    "type": "OBJECT",
                    "properties": [
                        "transcript": [
                            "type": "ARRAY",
                            "items": [
                                "type": "OBJECT",
                                "properties": [
                                    "startSeconds": ["type": "NUMBER"],
                                    "endSeconds": ["type": "NUMBER"],
                                    "text": ["type": "STRING"]
                                ],
                                "required": ["startSeconds", "endSeconds", "text"]
                            ]
                        ],
                        "segments": [
                            "type": "ARRAY",
                            "items": [
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
                        ]
                    ],
                    "required": ["transcript", "segments"]
                ]
            ]
        ]
        let bodyData = try JSONSerialization.data(withJSONObject: body)

        let data: Data
        let response: URLResponse
        if onStage != nil {
            // Stream the body from disk so `didSendBodyData` events on the
            // task delegate can fire; otherwise `URLSession.data(for:)`
            // wouldn't surface upload progress.
            let tempURL = FileManager.default.temporaryDirectory
                .appendingPathComponent("cloud-tx-\(UUID().uuidString).json")
            try bodyData.write(to: tempURL, options: .atomic)
            defer { try? FileManager.default.removeItem(at: tempURL) }
            let progressDelegate = UploadProgressDelegate(onStage: onStage)
            (data, response) = try await urlSession.upload(
                for: request,
                fromFile: tempURL,
                delegate: progressDelegate
            )
        } else {
            request.httpBody = bodyData
            (data, response) = try await urlSession.data(for: request)
        }
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let bodyText = String(data: data, encoding: .utf8) ?? ""
            Log.adDetection.error("Gemini combined call HTTP \(http.statusCode): \(bodyText, privacy: .public)")
            throw CloudTranscriptionError.uploadFailed(
                NSError(domain: "Gemini", code: http.statusCode,
                        userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode): \(bodyText.prefix(500))"])
            )
        }
        let decoded = try decoder.decode(GeminiResponse.self, from: data)
        guard let text = decoded.candidates.first?.content.parts.first?.text else {
            throw CloudTranscriptionError.parseFailure("Missing response text")
        }
        let parsed: CombinedResponse
        do {
            parsed = try JSONDecoder().decode(CombinedResponse.self, from: Data(text.utf8))
        } catch {
            throw CloudTranscriptionError.parseFailure(error.localizedDescription)
        }
        let transcript = parsed.transcript.compactMap { row -> TranscribedSegment? in
            guard row.endSeconds > row.startSeconds else { return nil }
            return TranscribedSegment(
                startSeconds: row.startSeconds,
                endSeconds: row.endSeconds,
                text: row.text
            )
        }
        let ads = parsed.segments.compactMap { row -> DetectedAd? in
            guard row.endSeconds > row.startSeconds else { return nil }
            let kind = SegmentKind(rawValue: row.kind) ?? .ad
            return DetectedAd(
                startSeconds: row.startSeconds,
                endSeconds: row.endSeconds,
                summary: row.summary,
                kind: kind
            )
        }
        let usage = decoded.usageMetadata.map {
            TokenUsage(inputTokens: $0.promptTokenCount ?? 0, outputTokens: $0.candidatesTokenCount ?? 0)
        }
        return (transcript.sorted { $0.startSeconds < $1.startSeconds },
                ads.sorted { $0.startSeconds < $1.startSeconds },
                usage)
    }
}

// MARK: - Decodable shapes

nonisolated private struct CombinedResponse: Decodable {
    let transcript: [TranscriptRow]
    let segments: [SegmentRow]
    struct TranscriptRow: Decodable {
        let startSeconds: Double
        let endSeconds: Double
        let text: String
    }
    struct SegmentRow: Decodable {
        let startSeconds: Double
        let endSeconds: Double
        let summary: String
        let kind: String
    }
}

/// Per-task delegate the service hands to
/// `URLSession.upload(for:fromFile:delegate:)` so it can translate
/// `didSendBodyData` events into `CloudTranscriptionStage.uploading`
/// callbacks. When the upload reaches 100% of the expected bytes, the
/// delegate also fires one `.analyzing` event so the row's UI label
/// flips from "Uploading…" to "Analyzing…" while the LLM crunches.
nonisolated private final class UploadProgressDelegate: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    private let onStage: (@Sendable (CloudTranscriptionStage) -> Void)?
    private var flippedToAnalyzing = false

    init(onStage: (@Sendable (CloudTranscriptionStage) -> Void)?) {
        self.onStage = onStage
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didSendBodyData bytesSent: Int64,
        totalBytesSent: Int64,
        totalBytesExpectedToSend: Int64
    ) {
        guard let onStage, totalBytesExpectedToSend > 0 else { return }
        onStage(.uploading(bytesSent: totalBytesSent, totalBytes: totalBytesExpectedToSend))
        if !flippedToAnalyzing && totalBytesSent >= totalBytesExpectedToSend {
            flippedToAnalyzing = true
            onStage(.analyzing)
        }
    }
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
        let candidatesTokenCount: Int?
    }
}
