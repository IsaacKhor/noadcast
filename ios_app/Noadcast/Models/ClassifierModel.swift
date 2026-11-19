import Foundation

nonisolated enum ClassifierProvider: String, CaseIterable, Identifiable, Sendable {
    case openRouter = "openrouter"
    case openAI = "openai"

    var id: String { rawValue }
    var label: String {
        switch self {
        case .openRouter: "OpenRouter"
        case .openAI: "OpenAI (direct)"
        }
    }

    var models: [ClassifierModel] {
        ClassifierModel.allCases.filter { $0.provider == self }
    }
}

/// Model identifiers accepted by each server classifier.
nonisolated enum ClassifierModel: String, CaseIterable, Identifiable, Sendable {
    case deepSeekFlash = "deepseek/deepseek-v4.1-flash"
    case qwenFlash = "qwen/qwen3.8-flash"
    case gptLunaHigh = "openai/gpt-6-luna"
    case directGptLunaHigh = "gpt-6-luna"

    static let defaultValue: ClassifierModel = .deepSeekFlash

    var id: String { rawValue }

    var provider: ClassifierProvider {
        switch self {
        case .deepSeekFlash, .qwenFlash, .gptLunaHigh: .openRouter
        case .directGptLunaHigh: .openAI
        }
    }

    var label: String {
        switch self {
        case .deepSeekFlash: "DeepSeek 4.1 Flash"
        case .qwenFlash: "Qwen 3.8 Flash"
        case .gptLunaHigh: "GPT 6 Luna (High)"
        case .directGptLunaHigh: "GPT 6 Luna (High)"
        }
    }
}
