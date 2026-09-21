import Foundation

/// The canonical set of playback speeds the user can pick from. Used by the
/// Settings default-speed picker, the per-podcast override picker, and the
/// Now Playing speed menu. Keep this in sync across the app by referencing
/// `PlaybackSpeed.options` rather than hard-coding the array.
enum PlaybackSpeed {
    static let options: [Double] = [1.0, 1.5, 2.0, 2.5, 3.0, 3.2, 3.4, 3.6, 3.8, 4.0, 4.1, 4.2]

    static func label(for rate: Double) -> String {
        String(format: "%.2g×", rate)
    }
}
