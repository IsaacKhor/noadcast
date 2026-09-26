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

    /// Detail string for a download to this device, rendered next to its
    /// progress bar: "12.3 MB / 50 MB" (or "12.3 MB" if the total is unknown,
    /// or a percentage). `nil` when there's nothing meaningful to show yet.
    static func progressDetail(for episode: Episode) -> String? {
        switch episode.downloadState {
        case .downloading, .queued:
            switch (episode.downloadedBytes, episode.downloadTotalBytes) {
            case (let current?, let total?) where total > 0:
                return "\(fileSize(current)) / \(fileSize(total))"
            case (let current?, _):
                return fileSize(current)
            default:
                guard episode.downloadProgress > 0 else { return nil }
                let pct = Int((episode.downloadProgress * 100).rounded())
                return "\(pct)%"
            }
        case .idle, .downloaded, .failed:
            return nil
        }
    }

    /// Detail string for a server job from `GET /jobs/active`, in the
    /// stage's natural unit:
    /// * `download` → "12.3 MB / 50 MB" (bytes)
    /// * `transcribe` → "12:34 / 45:00" (audio time)
    /// * `classify` → nothing (indeterminate)
    static func progressDetail(for job: ActiveJobDTO) -> String? {
        switch job.stage {
        case .download:
            switch (job.current, job.total) {
            case (let current?, let total?) where total > 0:
                return "\(fileSize(byteCount(current))) / \(fileSize(byteCount(total)))"
            case (let current?, _):
                return fileSize(byteCount(current))
            default:
                return nil
            }
        case .transcribe:
            guard let current = job.current, let total = job.total, total > 0 else { return nil }
            return "\(timestamp(max(0, min(current, total)))) / \(timestamp(total))"
        case .classify, .unknown:
            return nil
        }
    }

    /// Server-reported byte counts arrive as `Double`; never trap on a bad one.
    private static func byteCount(_ value: Double) -> Int64 {
        guard value.isFinite, value > 0 else { return 0 }
        return value >= Double(Int64.max) ? Int64.max : Int64(value)
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
