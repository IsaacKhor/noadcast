import Foundation
import SwiftData

@Model
final class QueueItem {
    var position: Int
    var addedAt: Date
    var episode: Episode?

    init(position: Int, episode: Episode?, addedAt: Date = .now) {
        self.position = position
        self.episode = episode
        self.addedAt = addedAt
    }
}
