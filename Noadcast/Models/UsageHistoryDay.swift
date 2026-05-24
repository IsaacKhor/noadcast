import Foundation
import SwiftData

@Model
final class UsageHistoryDay {
    var dayStart: Date

    var playbackSeconds: Double = 0
    var adSkippedSeconds: Double = 0

    init(dayStart: Date) {
        self.dayStart = Calendar.current.startOfDay(for: dayStart)
    }

    var totalPlaybackSeconds: Double {
        playbackSeconds + adSkippedSeconds
    }

    var hasPlayback: Bool {
        totalPlaybackSeconds > 0
    }

    static func recordPlayback(
        playedSeconds: Double,
        adSkippedSeconds: Double,
        in context: ModelContext,
        date: Date = .now
    ) {
        guard playedSeconds > 0 || adSkippedSeconds > 0 else { return }
        let day = day(for: date, in: context)
        day.playbackSeconds += playedSeconds
        day.adSkippedSeconds += adSkippedSeconds
    }

    private static func day(for date: Date, in context: ModelContext) -> UsageHistoryDay {
        let dayStart = Calendar.current.startOfDay(for: date)
        let descriptor = FetchDescriptor<UsageHistoryDay>(
            predicate: #Predicate { $0.dayStart == dayStart }
        )
        if let existing = try? context.fetch(descriptor).first {
            return existing
        }
        let day = UsageHistoryDay(dayStart: dayStart)
        context.insert(day)
        return day
    }
}
