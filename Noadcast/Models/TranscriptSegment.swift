import Foundation
import SwiftData

@Model
final class TranscriptSegment {
    var startSeconds: Double
    var endSeconds: Double
    var text: String
    var episode: Episode?

    init(startSeconds: Double, endSeconds: Double, text: String, episode: Episode? = nil) {
        self.startSeconds = startSeconds
        self.endSeconds = endSeconds
        self.text = text
        self.episode = episode
    }
}
