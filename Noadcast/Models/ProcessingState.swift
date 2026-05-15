import Foundation

enum EpisodeProcessingState: String, Codable, CaseIterable, Sendable {
    case new
    case downloading
    case downloaded
    case transcribing
    case detectingAds
    case ready
    case failed
}

enum AutoDownloadPolicy: String, Codable, CaseIterable, Sendable {
    case wifiOnly
    case anyNetwork
    case manualOnly

    var label: String {
        switch self {
        case .wifiOnly: "Wi-Fi only"
        case .anyNetwork: "Any network"
        case .manualOnly: "Manual only"
        }
    }
}

enum PodcastSortMode: String, Codable, CaseIterable, Sendable {
    case latestEpisode
    case alphabetical

    var label: String {
        switch self {
        case .latestEpisode: "Latest episode"
        case .alphabetical: "Alphabetical"
        }
    }
}
