import Foundation
import os

/// Shared `Logger` categories for the app. All output appears in Xcode's
/// debug console while running, and in Console.app filtered by subsystem
/// `com.isaackhor.Noadcast` for device-based debugging.
///
/// Use these instead of `print()` so messages get structured metadata
/// (timestamp, subsystem, category, log level).
nonisolated enum Log {
    private static let subsystem = "com.isaackhor.Noadcast"

    static let pipeline = Logger(subsystem: subsystem, category: "Pipeline")
    static let download = Logger(subsystem: subsystem, category: "Download")
    static let transcription = Logger(subsystem: subsystem, category: "Transcription")
    static let adDetection = Logger(subsystem: subsystem, category: "AdDetection")
    static let player = Logger(subsystem: subsystem, category: "Player")
    static let feed = Logger(subsystem: subsystem, category: "Feed")
    static let startup = Logger(subsystem: subsystem, category: "Startup")

    /// Signposter for cold-start measurements. View intervals in Instruments
    /// → Logging template, filter to subsystem `com.isaackhor.Noadcast`
    /// category `Startup`. Each `withIntervalSignpost("Name") { ... }` block
    /// becomes a labelled span on the timeline.
    static let signposter = OSSignposter(subsystem: subsystem, category: "Startup")

    /// Verbose dump of any `Error` — type, description, `NSError` domain/code,
    /// and the mirror dump (which exposes enum case names + associated values
    /// for things like `LanguageModelSession.GenerationError.guardrailViolation`).
    static func describe(_ error: Error) -> String {
        let ns = error as NSError
        var parts: [String] = [
            "type=\(type(of: error))",
            "description=\(error.localizedDescription)",
            "reflecting=\(String(reflecting: error))",
            "domain=\(ns.domain)",
            "code=\(ns.code)"
        ]
        if let local = error as? LocalizedError {
            if let reason = local.failureReason {
                parts.append("failureReason=\(reason)")
            }
            if let recovery = local.recoverySuggestion {
                parts.append("recovery=\(recovery)")
            }
        }
        if !ns.userInfo.isEmpty {
            parts.append("userInfo=\(ns.userInfo)")
        }
        return parts.joined(separator: " | ")
    }
}
