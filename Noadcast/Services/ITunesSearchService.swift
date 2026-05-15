import Foundation

struct ITunesPodcastResult: Identifiable, Sendable {
    let id: Int
    let collectionName: String
    let artistName: String
    let feedURL: URL?
    let artworkURL: URL?
}

actor ITunesSearchService {
    static let shared = ITunesSearchService()

    private let urlSession: URLSession = .shared

    private struct SearchResponse: Decodable {
        let results: [Entry]
        struct Entry: Decodable {
            let collectionId: Int?
            let collectionName: String?
            let artistName: String?
            let feedUrl: String?
            let artworkUrl600: String?
            let artworkUrl100: String?
        }
    }

    func search(term: String) async throws -> [ITunesPodcastResult] {
        let trimmed = term.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return [] }

        var comps = URLComponents(string: "https://itunes.apple.com/search")!
        comps.queryItems = [
            URLQueryItem(name: "term", value: trimmed),
            URLQueryItem(name: "media", value: "podcast"),
            URLQueryItem(name: "entity", value: "podcast"),
            URLQueryItem(name: "limit", value: "30")
        ]

        let (data, _) = try await urlSession.data(from: comps.url!)
        let decoded = try JSONDecoder().decode(SearchResponse.self, from: data)
        return decoded.results.compactMap { entry in
            guard let id = entry.collectionId else { return nil }
            return ITunesPodcastResult(
                id: id,
                collectionName: entry.collectionName ?? "Unknown",
                artistName: entry.artistName ?? "",
                feedURL: entry.feedUrl.flatMap(URL.init(string:)),
                artworkURL: (entry.artworkUrl600 ?? entry.artworkUrl100).flatMap(URL.init(string:))
            )
        }
    }
}
