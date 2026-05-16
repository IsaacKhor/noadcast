import Foundation

enum TimeFormatting {
    /// Formats seconds as `H:MM:SS` or `M:SS`.
    static func timestamp(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds >= 0 else { return "0:00" }
        let total = Int(seconds.rounded())
        let h = total / 3600
        let m = (total % 3600) / 60
        let s = total % 60
        if h > 0 {
            return String(format: "%d:%02d:%02d", h, m, s)
        }
        return String(format: "%d:%02d", m, s)
    }

    /// Formats a byte count for the downloads list.
    static func fileSize(_ bytes: Int64) -> String {
        let formatter = ByteCountFormatter()
        formatter.allowedUnits = [.useMB, .useGB]
        formatter.countStyle = .file
        return formatter.string(fromByteCount: bytes)
    }

    /// Stage-specific detail string rendered next to the progress bar while
    /// an episode is being processed. Returns `nil` when there's nothing
    /// meaningful to show yet.
    /// * `.downloading` → "12.3 MB / 50 MB" (or "12.3 MB" if total unknown)
    /// * `.transcribing` → "12:34 / 45:00"
    /// * `.detectingAds` → "Chunk 3 of 12"
    static func progressDetail(for episode: Episode) -> String? {
        switch episode.processingState {
        case .downloading:
            let current = episode.processingCurrent.map { Int64($0) }
            let total = episode.processingTotal.map { Int64($0) }
            switch (current, total) {
            case (let c?, let t?) where t > 0:
                return "\(fileSize(c)) / \(fileSize(t))"
            case (let c?, _):
                return fileSize(c)
            default:
                let pct = Int((episode.processingProgress * 100).rounded())
                return "\(pct)%"
            }
        case .transcribing:
            guard let current = episode.processingCurrent,
                  let total = episode.processingTotal,
                  total > 0 else { return nil }
            return "\(timestamp(current)) / \(timestamp(total))"
        case .detectingAds:
            guard let current = episode.processingCurrent,
                  let total = episode.processingTotal,
                  total > 0 else { return nil }
            return "Chunk \(Int(current)) of \(Int(total))"
        default:
            return nil
        }
    }

    /// "3:47 PM · 5 min ago" — absolute (locale time) and relative (locale
    /// relative) renderings of the same instant. Used by the refresh-status
    /// rows on the Podcasts list and detail.
    static func refreshTimestamp(_ date: Date) -> String {
        let absolute = date.formatted(date: .omitted, time: .shortened)
        let relative = date.formatted(.relative(presentation: .named))
        return "\(absolute) · \(relative)"
    }

    /// Whole-minutes duration, rounded down. Returns `"4h 23m"` or `"23m"`
    /// (locale-formatted via `Duration.UnitsFormatStyle`). Used by the
    /// listening-stats rows in Settings where seconds-level precision is
    /// noise.
    static func minutesDuration(_ seconds: Double) -> String {
        let minutes = max(0, Int(seconds) / 60)
        let duration = Duration.seconds(minutes * 60)
        let allowed: Set<Duration.UnitsFormatStyle.Unit> = minutes >= 60
            ? [.hours, .minutes]
            : [.minutes]
        return duration.formatted(.units(allowed: allowed, width: .abbreviated))
    }

    /// Long-form duration for the lifetime time-saved counters. Uses
    /// `Duration.UnitsFormatStyle` so the units render in the user's locale
    /// (e.g. `1 hr 23 min` in en, `1 hod 23 min` in cs, etc.).
    static func longDuration(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds >= 1 else {
            return Duration.seconds(0).formatted(
                .units(allowed: [.seconds], width: .abbreviated)
            )
        }
        let total = Int64(seconds.rounded())
        let duration = Duration.seconds(total)
        let allowed: Set<Duration.UnitsFormatStyle.Unit>
        if total >= 3600 {
            allowed = [.hours, .minutes]
        } else if total >= 60 {
            allowed = [.minutes, .seconds]
        } else {
            allowed = [.seconds]
        }
        return duration.formatted(.units(allowed: allowed, width: .abbreviated))
    }
}
