import Foundation
import os

nonisolated struct DetectedAd: Sendable {
    let startSeconds: Double
    let endSeconds: Double
    let summary: String
    let kind: SegmentKind
    var duration: Double { endSeconds - startSeconds }
}

/// Token usage reported by a provider for a single detection call. `nil`
/// fields mean that provider's response didn't include the breakdown.
nonisolated struct TokenUsage: Sendable {
    var inputTokens: Int
    var outputTokens: Int
}

nonisolated struct DetectionResult: Sendable {
    let ads: [DetectedAd]
    let usage: TokenUsage?
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

/// The structured JSON shape both providers are asked to return. Each
/// provider's structured-output mode is configured with the equivalent JSON
/// schema, so parsing is provider-agnostic.
nonisolated struct AdSegmentJSON: Decodable, Sendable {
    let startSeconds: Double
    let endSeconds: Double
    let summary: String
    /// `"ad"`, `"intro"`, or `"outro"` — enforced by the response-schema
    /// `enum` on the provider side, so a malformed value here means the
    /// model ignored the schema.
    let kind: String
}

nonisolated struct AdSegmentsJSON: Decodable, Sendable {
    let segments: [AdSegmentJSON]
}

/// Single-shot cloud ad detection. The entire transcript is sent in one
/// request to Gemini or OpenAI, the response is parsed against
/// `AdSegmentsJSON`, and the resulting `DetectedAd`s are passed through the
/// shared merge / minimum-duration post-processing.
actor AdDetectionService {
    static let shared = AdDetectionService()

    /// The system prompt sent to whichever model is doing ad detection.
    /// Hardcoded — the Settings screen shows this text but can't edit it.
    nonisolated static let detectionPrompt: String = """
    You are analyzing a podcast transcript and identifying segments the \
    listener probably wants to skip. Each segment you return has a `kind`:

    - "intro": a single contiguous segment at the very BEGINNING of the \
    episode covering theme music, branding, host introductions, episode \
    teasers, and any preroll ads played before the main content starts. \
    There is at most one intro per episode, and it spans from the start \
    of the episode through to where the substantive content begins.

    - "outro": a single contiguous segment at the very END of the episode \
    covering closing music, credits, next-episode teasers, postroll ads, \
    and farewells. There is at most one outro per episode, and it spans \
    from where the substantive content finishes through to the end of \
    the episode. Intros and outros may include ads — they're still a \
    single intro/outro segment, not separate entries.

    - "ad": a mid-episode advertisement, sponsored message, host-read ad, \
    promo code, paid endorsement, or cross-promotion of another podcast \
    that appears BETWEEN the intro and outro (i.e. inside the main \
    content). Editorial mentions, listener mail, the host's own products \
    discussed editorially, and interview segments are NOT ads.

    Given the timestamped transcript lines, return the start and end \
    seconds of each segment using only timestamps that appear in the \
    transcript. Be conservative — flag ads only when you're confident. \
    Return an empty list if nothing should be skipped. If two ad segments \
    are adjacent (no real content between them), merge them into one.
    """

    private let urlSession: URLSession = .shared
    private let decoder = JSONDecoder()

    // MARK: - Top-level entry point

    func detectAds(
        in transcript: [TranscribedSegment],
        provider: AdDetectionProvider,
        googleAPIKey: String? = nil,
        openAIAPIKey: String? = nil,
        progress: (@Sendable (Int, Int) -> Void)? = nil
    ) async throws -> DetectionResult {
        guard !transcript.isEmpty else { return DetectionResult(ads: [], usage: nil) }

        Log.adDetection.info("Begin ad detection — provider=\(provider.label, privacy: .public) segments=\(transcript.count)")
        progress?(0, 1)

        let prompt = Self.buildPrompt(transcript: transcript)
        let raw: [DetectedAd]
        let usage: TokenUsage?
        switch provider {
        case .gemini35Flash, .gemini31FlashLite, .gemini25Flash, .gemini25FlashLite:
            guard let key = googleAPIKey, !key.isEmpty else {
                throw AdDetectionError.modelUnavailable("Google API key missing — add one in Settings → Detection model")
            }
            (raw, usage) = try await callGemini(
                model: provider.apiModel,
                prompt: prompt,
                apiKey: key
            )
        }
        progress?(1, 1)

        let sorted = raw.sorted { $0.startSeconds < $1.startSeconds }
        Log.adDetection.info("Ad detection complete — provider=\(provider.label, privacy: .public) ads=\(sorted.count) input_tokens=\(usage?.inputTokens ?? 0) output_tokens=\(usage?.outputTokens ?? 0)")
        return DetectionResult(ads: sorted, usage: usage)
    }

    // MARK: - Prompt assembly

    private static func buildPrompt(transcript: [TranscribedSegment]) -> String {
        let lines = transcript.map { seg in
            String(format: "[%.1f-%.1f] %@", seg.startSeconds, seg.endSeconds, seg.text)
        }.joined(separator: "\n")
        return """
        The following is a podcast transcript with timestamps in seconds:

        \(lines)

        Identify every segment the listener would want to skip — the intro \
        (at most one, at the start), the outro (at most one, at the end), \
        and every mid-episode advertisement. Use only the timestamps that \
        appear in the transcript above. Return an object with a `segments` \
        array; each entry has `startSeconds`, `endSeconds`, a one-sentence \
        `summary`, and a `kind` of "intro", "outro", or "ad". Return an \
        empty `segments` array if nothing should be skipped.
        """
    }

    // MARK: - Gemini

    private func callGemini(
        model: String,
        prompt: String,
        apiKey: String
    ) async throws -> ([DetectedAd], TokenUsage?) {
        let urlString = "https://generativelanguage.googleapis.com/v1beta/models/\(model):generateContent?key=\(apiKey)"
        guard let url = URL(string: urlString) else {
            throw AdDetectionError.generationFailure(URLError(.badURL))
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        // Gemini's response-schema dialect uses uppercase type names.
        let body: [String: Any] = [
            "systemInstruction": ["parts": [["text": Self.detectionPrompt]]],
            "contents": [
                ["role": "user", "parts": [["text": prompt]]]
            ],
            "generationConfig": [
                "responseMimeType": "application/json",
                "responseSchema": [
                    "type": "OBJECT",
                    "properties": [
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
                    "required": ["segments"]
                ]
            ]
        ]
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        Log.adDetection.info("Gemini call → model=\(model, privacy: .public) transcript_chars=\(prompt.count)")
        let data = try await performRequest(request, providerName: "Gemini")
        let decoded = try decoder.decode(GeminiResponse.self, from: data)
        guard let text = decoded.candidates.first?.content.parts.first?.text else {
            throw AdDetectionError.generationFailure(URLError(.cannotParseResponse))
        }
        let usage = decoded.usageMetadata.map {
            TokenUsage(inputTokens: $0.promptTokenCount ?? 0, outputTokens: $0.candidatesTokenCount ?? 0)
        }
        return (try Self.parseAdsJSON(text), usage)
    }

    // MARK: - OpenAI

    private func callOpenAI(
        model: String,
        prompt: String,
        apiKey: String
    ) async throws -> ([DetectedAd], TokenUsage?) {
        guard let url = URL(string: "https://api.openai.com/v1/chat/completions") else {
            throw AdDetectionError.generationFailure(URLError(.badURL))
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")

        // OpenAI's strict JSON-schema mode requires `additionalProperties: false`
        // at every object level and `required` listing every property.
        let body: [String: Any] = [
            "model": model,
            "messages": [
                ["role": "system", "content": Self.detectionPrompt],
                ["role": "user", "content": prompt]
            ],
            "response_format": [
                "type": "json_schema",
                "json_schema": [
                    "name": "ad_segments",
                    "strict": true,
                    "schema": [
                        "type": "object",
                        "additionalProperties": false,
                        "properties": [
                            "segments": [
                                "type": "array",
                                "items": [
                                    "type": "object",
                                    "additionalProperties": false,
                                    "properties": [
                                        "startSeconds": ["type": "number"],
                                        "endSeconds": ["type": "number"],
                                        "summary": ["type": "string"],
                                        "kind": [
                                            "type": "string",
                                            "enum": ["ad", "intro", "outro"]
                                        ]
                                    ],
                                    "required": ["startSeconds", "endSeconds", "summary", "kind"]
                                ]
                            ]
                        ],
                        "required": ["segments"]
                    ]
                ]
            ]
        ]
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        Log.adDetection.info("OpenAI call → model=\(model, privacy: .public) transcript_chars=\(prompt.count)")
        let data = try await performRequest(request, providerName: "OpenAI")
        let decoded = try decoder.decode(OpenAIResponse.self, from: data)
        guard let content = decoded.choices.first?.message.content else {
            throw AdDetectionError.generationFailure(URLError(.cannotParseResponse))
        }
        let usage = decoded.usage.map {
            TokenUsage(inputTokens: $0.prompt_tokens ?? 0, outputTokens: $0.completion_tokens ?? 0)
        }
        return (try Self.parseAdsJSON(content), usage)
    }

    // MARK: - HTTP + JSON helpers

    private func performRequest(_ request: URLRequest, providerName: String) async throws -> Data {
        do {
            let (data, response) = try await urlSession.data(for: request)
            if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
                let body = String(data: data, encoding: .utf8) ?? ""
                Log.adDetection.error("\(providerName, privacy: .public) HTTP \(http.statusCode): \(body, privacy: .public)")
                throw AdDetectionError.generationFailure(
                    NSError(
                        domain: providerName,
                        code: http.statusCode,
                        userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode): \(body.prefix(500))"]
                    )
                )
            }
            return data
        } catch let error as AdDetectionError {
            throw error
        } catch {
            Log.adDetection.error("\(providerName, privacy: .public) request failed: \(Log.describe(error), privacy: .public)")
            throw AdDetectionError.generationFailure(error)
        }
    }

    private static func parseAdsJSON(_ text: String) throws -> [DetectedAd] {
        guard let data = text.data(using: .utf8) else {
            throw AdDetectionError.generationFailure(URLError(.cannotParseResponse))
        }
        let parsed: AdSegmentsJSON
        do {
            parsed = try JSONDecoder().decode(AdSegmentsJSON.self, from: data)
        } catch {
            Log.adDetection.error("Couldn't parse provider JSON: \(error.localizedDescription, privacy: .public) — raw=\(text.prefix(500), privacy: .public)")
            throw AdDetectionError.generationFailure(error)
        }
        return parsed.segments.compactMap { seg -> DetectedAd? in
            guard seg.endSeconds > seg.startSeconds else { return nil }
            // The schema constrains `kind` to ad/intro/outro, but a model
            // can still go off the rails — fall back to `.ad` rather than
            // dropping the segment entirely.
            let kind = SegmentKind(rawValue: seg.kind) ?? .ad
            return DetectedAd(
                startSeconds: seg.startSeconds,
                endSeconds: seg.endSeconds,
                summary: seg.summary,
                kind: kind
            )
        }
    }

}

// MARK: - Provider response wrappers

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

nonisolated private struct OpenAIResponse: Decodable {
    let choices: [Choice]
    let usage: Usage?
    struct Choice: Decodable {
        let message: Message
        struct Message: Decodable {
            let content: String
        }
    }
    struct Usage: Decodable {
        let prompt_tokens: Int?
        let completion_tokens: Int?
    }
}
