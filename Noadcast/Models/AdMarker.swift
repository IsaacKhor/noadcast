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

    init(
        startSeconds: Double,
        endSeconds: Double,
        summary: String,
        manuallyEdited: Bool = false,
        isDeleted: Bool = false,
        episode: Episode? = nil
    ) {
        self.startSeconds = startSeconds
        self.endSeconds = endSeconds
        self.summary = summary
        self.manuallyEdited = manuallyEdited
        self.isDeleted = isDeleted
        self.episode = episode
    }

    var duration: Double { max(0, endSeconds - startSeconds) }

    func contains(_ time: Double) -> Bool {
        !isDeleted && time >= startSeconds && time < endSeconds
    }
}
