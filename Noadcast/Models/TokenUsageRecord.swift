import Foundation
import SwiftData

@Model
final class TokenUsageRecord {
    var createdAt: Date

    var providerRaw: String
    var providerLabel: String
    var episodeGUID: String?
    var episodeTitle: String?

    var inputTokens: Int
    var thoughtTokens: Int
    var outputTokens: Int

    var inputCostUSD: Double
    var thoughtCostUSD: Double
    var outputCostUSD: Double

    init(
        createdAt: Date = .now,
        provider: AdDetectionProvider,
        episodeGUID: String?,
        episodeTitle: String?,
        inputTokens: Int,
        thoughtTokens: Int,
        outputTokens: Int,
        inputCostUSD: Double,
        thoughtCostUSD: Double,
        outputCostUSD: Double
    ) {
        self.createdAt = createdAt
        self.providerRaw = provider.rawValue
        self.providerLabel = provider.label
        self.episodeGUID = episodeGUID
        self.episodeTitle = episodeTitle
        self.inputTokens = inputTokens
        self.thoughtTokens = thoughtTokens
        self.outputTokens = outputTokens
        self.inputCostUSD = inputCostUSD
        self.thoughtCostUSD = thoughtCostUSD
        self.outputCostUSD = outputCostUSD
    }

    var totalTokens: Int {
        inputTokens + thoughtTokens + outputTokens
    }

    var totalCostUSD: Double {
        inputCostUSD + thoughtCostUSD + outputCostUSD
    }

    static func resetAll(in context: ModelContext) {
        let records = (try? context.fetch(FetchDescriptor<TokenUsageRecord>())) ?? []
        for record in records {
            context.delete(record)
        }
    }
}
