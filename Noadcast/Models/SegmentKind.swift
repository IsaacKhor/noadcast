import Foundation
import SwiftUI

/// Classification of a skippable segment within an episode. The LLM returns
/// one of these for every detected segment; the player decides whether to
/// actually skip it based on the user's per-kind toggles.
nonisolated enum SegmentKind: String, Codable, CaseIterable, Sendable {
    case ad
    case intro
    case outro

    var label: String {
        switch self {
        case .ad: "Ad"
        case .intro: "Intro"
        case .outro: "Outro"
        }
    }

    /// Color used on the timeline + transcript badge for this kind. Ads stay
    /// orange (existing behaviour); intros and outros share an indigo tone
    /// so they read as "boundary segments" distinct from the main ad colour.
    var tint: Color {
        switch self {
        case .ad: .orange
        case .intro, .outro: .indigo
        }
    }
}
