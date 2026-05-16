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
}
