import Foundation
import os

nonisolated struct DetectedAd: Sendable {
    let startSeconds: Double
    let endSeconds: Double
    let summary: String
    var duration: Double { endSeconds - startSeconds }
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
    You are analyzing a podcast transcript to identify advertisement segments. \
    Advertisements include: sponsored messages, host-read ads, promo codes, \
    paid endorsements, and cross-promotion of other podcasts. They do NOT include: \
    editorial mentions, listener mail, the host's own products discussed editorially, \
    or interview segments.

    Given a list of timestamped transcript lines, return the start and end seconds \
    of each contiguous ad segment. Use the timestamps provided. Be conservative — \
    only flag a segment if you are confident it is a paid advertisement. Return an \
    empty list if no ads are present. If two ads segment are adjacent, merge them \
    into one longer segment.
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
    ) async throws -> [DetectedAd] {
        guard !transcript.isEmpty else { return [] }

        Log.adDetection.info("Begin ad detection — provider=\(provider.label, privacy: .public) segments=\(transcript.count)")
        progress?(0, 1)

        let prompt = Self.buildPrompt(transcript: transcript)
        let raw: [DetectedAd]
        switch provider {
        case .geminiFlashLite:
            guard let key = googleAPIKey, !key.isEmpty else {
                throw AdDetectionError.modelUnavailable("Google API key missing — add one in Settings → Detection model")
            }
            raw = try await callGemini(
                model: provider.apiModel,
                prompt: prompt,
                apiKey: key
            )
        case .gpt54Nano, .gpt54Mini:
            guard let key = openAIAPIKey, !key.isEmpty else {
                throw AdDetectionError.modelUnavailable("OpenAI API key missing — add one in Settings → Detection model")
            }
            raw = try await callOpenAI(
                model: provider.apiModel,
                prompt: prompt,
                apiKey: key
            )
        }
        progress?(1, 1)

        let sorted = raw.sorted { $0.startSeconds < $1.startSeconds }
        Log.adDetection.info("Ad detection complete — provider=\(provider.label, privacy: .public) ads=\(sorted.count)")
        return sorted
    }

    // MARK: - Prompt assembly

    private static func buildPrompt(transcript: [TranscribedSegment]) -> String {
        let lines = transcript.map { seg in
            String(format: "[%.1f-%.1f] %@", seg.startSeconds, seg.endSeconds, seg.text)
        }.joined(separator: "\n")
        return """
        The following is a podcast transcript with timestamps in seconds:

        \(lines)

        Identify every advertisement segment in this transcript. Use only the \
        timestamps that appear in the transcript above. Return an object with \
        a `segments` array; each entry has `startSeconds`, `endSeconds`, and a \
        one-sentence `summary` describing what's being advertised. If no ads \
        are present, return an empty `segments` array.
        """
    }

    // MARK: - Gemini

    private func callGemini(
        model: String,
        prompt: String,
        apiKey: String
    ) async throws -> [DetectedAd] {
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
                                    "summary": ["type": "STRING"]
                                ],
                                "required": ["startSeconds", "endSeconds", "summary"]
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
        return try Self.parseAdsJSON(text)
    }

    // MARK: - OpenAI

    private func callOpenAI(
        model: String,
        prompt: String,
        apiKey: String
    ) async throws -> [DetectedAd] {
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
                                        "summary": ["type": "string"]
                                    ],
                                    "required": ["startSeconds", "endSeconds", "summary"]
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
        return try Self.parseAdsJSON(content)
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
            return DetectedAd(
                startSeconds: seg.startSeconds,
                endSeconds: seg.endSeconds,
                summary: seg.summary
            )
        }
    }

}

// MARK: - Provider response wrappers

nonisolated private struct GeminiResponse: Decodable {
    let candidates: [Candidate]
    struct Candidate: Decodable {
        let content: Content
        struct Content: Decodable {
            let parts: [Part]
            struct Part: Decodable {
                let text: String
            }
        }
    }
}

nonisolated private struct OpenAIResponse: Decodable {
    let choices: [Choice]
    struct Choice: Decodable {
        let message: Message
        struct Message: Decodable {
            let content: String
        }
    }
}
