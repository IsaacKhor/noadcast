import Foundation
import SwiftData

@Model
final class AdMarker {
    var startSeconds: Double
    var endSeconds: Double
    var summary: String
    var manuallyEdited: Bool
    var isDeleted: Bool
    var episode: Episode?

    /// Backs `kind`. Stored as `String` so SwiftData migration is trivial
    /// for rows that pre-date the field (they default to `"ad"`).
    var kindRaw: String = SegmentKind.ad.rawValue

    var kind: SegmentKind {
        get { SegmentKind(rawValue: kindRaw) ?? .ad }
        set { kindRaw = newValue.rawValue }
    }

    init(
        startSeconds: Double,
        endSeconds: Double,
        summary: String,
        kind: SegmentKind = .ad,
        manuallyEdited: Bool = false,
        isDeleted: Bool = false,
        episode: Episode? = nil
    ) {
        self.startSeconds = startSeconds
        self.endSeconds = endSeconds
        self.summary = summary
        self.kindRaw = kind.rawValue
        self.manuallyEdited = manuallyEdited
        self.isDeleted = isDeleted
        self.episode = episode
    }

    var duration: Double { max(0, endSeconds - startSeconds) }

    func contains(_ time: Double) -> Bool {
        !isDeleted && time >= startSeconds && time < endSeconds
    }
}
