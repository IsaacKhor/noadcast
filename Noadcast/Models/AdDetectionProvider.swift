import Foundation

/// Cloud LLM that handles ad detection. All providers receive the entire
/// transcript in a single call and return a structured-JSON response
/// (`AdSegmentsJSON`) constrained by the provider's response-schema feature.
nonisolated enum AdDetectionProvider: String, Codable, CaseIterable, Sendable {
    case geminiFlashLite
    case geminiFlash
    case gpt54Nano
    case gpt54Mini

    var label: String {
        switch self {
        case .geminiFlashLite: "Gemini Flash-Lite (latest)"
        case .geminiFlash: "Gemini Flash (latest)"
        case .gpt54Nano: "GPT-5.4 nano"
        case .gpt54Mini: "GPT-5.4 mini"
        }
    }

    /// Exact model identifier passed to the provider's REST API.
    var apiModel: String {
        switch self {
        case .geminiFlashLite: "gemini-flash-lite-latest"
        case .geminiFlash: "gemini-flash-latest"
        case .gpt54Nano: "gpt-5.4-nano"
        case .gpt54Mini: "gpt-5.4-mini"
        }
    }

    var requiresGoogleKey: Bool {
        switch self {
        case .geminiFlashLite, .geminiFlash: true
        default: false
        }
    }

    var requiresOpenAIKey: Bool {
        switch self {
        case .gpt54Nano, .gpt54Mini: true
        default: false
        }
    }

    /// Whether `CloudTranscriptionService` can route through this provider.
    /// Today only the Gemini family is wired up — OpenAI's audio-input
    /// pricing and file-handling differ enough that it's worth doing
    /// separately, later.
    var supportsCloudTranscription: Bool {
        switch self {
        case .geminiFlashLite, .geminiFlash: true
        case .gpt54Nano, .gpt54Mini: false
        }
    }

    /// USD per million input tokens (text or audio). Used for the rough
    /// running-cost estimate in Settings. Audio is reported by the provider
    /// in the same `promptTokenCount` field as text — Gemini converts 1
    /// second of audio to 32 tokens internally.
    var pricePerMTokensInput: Double {
        switch self {
        case .geminiFlashLite: 0.10
        case .geminiFlash: 0.30
        case .gpt54Nano: 0.05
        case .gpt54Mini: 0.25
        }
    }

    /// USD per million output tokens.
    var pricePerMTokensOutput: Double {
        switch self {
        case .geminiFlashLite: 0.40
        case .geminiFlash: 2.50
        case .gpt54Nano: 0.40
        case .gpt54Mini: 2.00
        }
    }
}
