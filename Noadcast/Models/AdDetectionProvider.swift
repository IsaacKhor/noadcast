import Foundation

/// Cloud model that handles ad detection by analyzing the uploaded audio
/// file and returning a structured JSON list of skip segments.
nonisolated enum AdDetectionProvider: String, Codable, CaseIterable, Sendable {
    case gemini35Flash
    case gemini31FlashLite
    case gemini25Flash
    case gemini25FlashLite

    var label: String {
        switch self {
        case .gemini35Flash: "Gemini 3.5 Flash"
        case .gemini31FlashLite: "Gemini 3.1 Flash Lite"
        case .gemini25Flash: "Gemini 2.5 Flash"
        case .gemini25FlashLite: "Gemini 2.5 Flash Lite"
        }
    }

    /// Exact model identifier passed to the provider's REST API.
    var apiModel: String {
        switch self {
        case .gemini35Flash: "gemini-3.5-flash"
        case .gemini31FlashLite: "gemini-3.1-flash-lite"
        case .gemini25Flash: "gemini-2.5-flash"
        case .gemini25FlashLite: "gemini-2.5-flash-lite"
        }
    }

    var requiresGoogleKey: Bool {
        true
    }

    var requiresOpenAIKey: Bool {
        false
    }

    /// All currently-exposed detection providers are Gemini models and can
    /// accept file-based multimodal input through `CloudTranscriptionService`.
    var supportsCloudTranscription: Bool {
        true
    }

    /// USD per million text/image/video input tokens, based on the
    /// provider's published standard paid-tier rates.
    var pricePerMTokensTextInput: Double {
        switch self {
        case .gemini35Flash: 1.50
        case .gemini31FlashLite: 0.25
        case .gemini25Flash: 0.30
        case .gemini25FlashLite: 0.10
        }
    }

    /// USD per million audio input tokens, based on the provider's
    /// published standard paid-tier rates.
    var pricePerMTokensAudioInput: Double {
        switch self {
        case .gemini35Flash: 1.50
        case .gemini31FlashLite: 0.50
        case .gemini25Flash: 1.00
        case .gemini25FlashLite: 0.30
        }
    }

    /// USD per million output tokens.
    var pricePerMTokensOutput: Double {
        switch self {
        case .gemini35Flash: 9.00
        case .gemini31FlashLite: 1.50
        case .gemini25Flash: 2.50
        case .gemini25FlashLite: 0.40
        }
    }
}
