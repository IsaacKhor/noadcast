import Foundation

/// Validates the exact polling interval accepted by the server settings API.
nonisolated enum FeedIntervalMinutes {
    static func parse(_ input: String) -> Int? {
        let trimmed = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
              trimmed.utf8.allSatisfy({ $0 >= 48 && $0 <= 57 }),
              let minutes = Int(trimmed),
              (1...1440).contains(minutes)
        else { return nil }
        return minutes
    }
}
