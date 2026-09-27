import Foundation

/// Model identifiers accepted by the server's OpenRouter classifier.
nonisolated enum ClassifierModel: String, CaseIterable, Identifiable, Sendable {
    case deepSeekFlash = "deepseek/deepseek-v4.1-flash"
    case qwenFlash = "qwen/qwen3.8-flash"
    case gptLunaHigh = "openai/gpt-6-luna"

    static let defaultValue: ClassifierModel = .deepSeekFlash

    var id: String { rawValue }

    var label: String {
        switch self {
        case .deepSeekFlash: "DeepSeek 4.1 Flash"
        case .qwenFlash: "Qwen 3.8 Flash"
        case .gptLunaHigh: "GPT 6 Luna (High)"
        }
    }
}
