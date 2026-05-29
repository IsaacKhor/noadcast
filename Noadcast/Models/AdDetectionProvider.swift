import Foundation

/// Cloud model that handles ad detection by analyzing the uploaded audio
/// file and returning a structured JSON list of skip segments.
nonisolated enum AdDetectionProvider: String, Codable, CaseIterable, Sendable {
    case gemini3Flash
    case gemini35Flash
    case gemini31FlashLite
    case gemini25Flash
    case gemini25FlashLite

    var label: String {
        switch self {
        case .gemini3Flash: "Gemini 3 Flash Preview"
        case .gemini35Flash: "Gemini 3.5 Flash"
        case .gemini31FlashLite: "Gemini 3.1 Flash Lite"
        case .gemini25Flash: "Gemini 2.5 Flash"
        case .gemini25FlashLite: "Gemini 2.5 Flash Lite"
        }
    }

    /// Exact model identifier passed to the provider's REST API.
    var apiModel: String {
        switch self {
        case .gemini3Flash: "gemini-3-flash-preview"
        case .gemini35Flash: "gemini-3.5-flash"
        case .gemini31FlashLite: "gemini-3.1-flash-lite"
        case .gemini25Flash: "gemini-2.5-flash"
        case .gemini25FlashLite: "gemini-2.5-flash-lite"
        }
    }

    var requiresGoogleKey: Bool {
        true
    }

    /// All currently-exposed detection providers are Gemini models and can
    /// accept file-based multimodal input through `CloudTranscriptionService`.
    var supportsCloudTranscription: Bool {
        true
    }

    var supportsThinkingLevel: Bool {
        switch self {
        case .gemini3Flash, .gemini35Flash, .gemini31FlashLite:
            true
        case .gemini25Flash, .gemini25FlashLite:
            false
        }
    }

    var thinkingLevelOptions: [AdDetectionThinkingLevel] {
        supportsThinkingLevel ? AdDetectionThinkingLevel.allCases : [.automatic]
    }

    /// USD per million text/image/video input tokens, based on the
    /// provider's published standard paid-tier rates.
    var pricePerMTokensTextInput: Double {
        switch self {
        case .gemini3Flash: 0.50
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
        case .gemini3Flash: 1.00
        case .gemini35Flash: 1.50
        case .gemini31FlashLite: 0.50
        case .gemini25Flash: 1.00
        case .gemini25FlashLite: 0.30
        }
    }

    /// USD per million output tokens.
    var pricePerMTokensOutput: Double {
        switch self {
        case .gemini3Flash: 3.00
        case .gemini35Flash: 9.00
        case .gemini31FlashLite: 1.50
        case .gemini25Flash: 2.50
        case .gemini25FlashLite: 0.40
        }
    }

    /// Gemini bills thinking tokens as output tokens; keep this separate so
    /// Settings can show the estimate as its own line.
    var pricePerMTokensThoughtOutput: Double {
        pricePerMTokensOutput
    }
}

nonisolated enum AdDetectionThinkingLevel: String, Codable, CaseIterable, Sendable {
    case automatic
    case minimal
    case low
    case medium
    case high

    var label: String {
        switch self {
        case .automatic: "Default"
        case .minimal: "Minimal"
        case .low: "Low"
        case .medium: "Medium"
        case .high: "High"
        }
    }

    var apiValue: String? {
        switch self {
        case .automatic: nil
        case .minimal: "minimal"
        case .low: "low"
        case .medium: "medium"
        case .high: "high"
        }
    }
}
