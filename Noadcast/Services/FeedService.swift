import Foundation

struct ParsedFeed: Sendable {
    var title: String
    var author: String?
    var summary: String?
    var artworkURL: URL?
    var episodes: [ParsedEpisode]
}

struct ParsedEpisode: Sendable {
    var guid: String
    var title: String
    var description: String?
    var publishedAt: Date?
    var duration: Double?
    var audioURL: URL
    var audioMimeType: String?
}

enum FeedError: LocalizedError {
    case invalidURL
    case networkFailure(Error)
    case parseFailure(String)
    case noEpisodes

    var errorDescription: String? {
        switch self {
        case .invalidURL: "The feed URL is invalid."
        case .networkFailure(let err): "Couldn't reach the feed: \(err.localizedDescription)"
        case .parseFailure(let msg): "Feed parse error: \(msg)"
        case .noEpisodes: "Feed has no episodes."
        }
    }
}

/// Fetches and parses podcast RSS feeds. Pure value-in / value-out — no DB writes.
actor FeedService {
    static let shared = FeedService()

    private let urlSession: URLSession

    init(urlSession: URLSession = .shared) {
        self.urlSession = urlSession
    }

    func fetch(feedURL: URL) async throws -> ParsedFeed {
        let data: Data
        do {
            (data, _) = try await urlSession.data(from: feedURL)
        } catch {
            throw FeedError.networkFailure(error)
        }

        let parser = RSSParser()
        guard let feed = parser.parse(data: data) else {
            throw FeedError.parseFailure("Unable to parse XML.")
        }
        return feed
    }
}

// MARK: - RSS / Atom XML parsing

/// Minimal RSS 2.0 / iTunes-namespaced podcast feed parser.
nonisolated private final class RSSParser: NSObject, XMLParserDelegate {
    private enum Section { case none, channel, item }

    private var section: Section = .none
    private var currentElement: String = ""
    private var currentAttributes: [String: String] = [:]
    private var textBuffer: String = ""

    private var channelTitle: String = ""
    private var channelAuthor: String?
    private var channelSummary: String?
    private var channelArtwork: URL?

    private var itemTitle: String = ""
    private var itemDescription: String?
    private var itemPubDate: Date?
    private var itemDuration: Double?
    private var itemGUID: String?
    private var itemEnclosureURL: URL?
    private var itemEnclosureMIME: String?

    private var episodes: [ParsedEpisode] = []

    private static let pubDateFormatters: [DateFormatter] = {
        let formats = [
            "EEE, dd MMM yyyy HH:mm:ss zzz",
            "EEE, dd MMM yyyy HH:mm:ss Z",
            "yyyy-MM-dd'T'HH:mm:ssZ",
            "yyyy-MM-dd'T'HH:mm:ss.SSSZ"
        ]
        return formats.map {
            let f = DateFormatter()
            f.locale = Locale(identifier: "en_US_POSIX")
            f.dateFormat = $0
            return f
        }
    }()

    func parse(data: Data) -> ParsedFeed? {
        let parser = XMLParser(data: data)
        parser.delegate = self
        parser.shouldProcessNamespaces = false
        guard parser.parse() else { return nil }
        return ParsedFeed(
            title: channelTitle,
            author: channelAuthor,
            summary: channelSummary,
            artworkURL: channelArtwork,
            episodes: episodes
        )
    }

    func parser(
        _ parser: XMLParser,
        didStartElement elementName: String,
        namespaceURI: String?,
        qualifiedName qName: String?,
        attributes attributeDict: [String: String] = [:]
    ) {
        currentElement = elementName
        currentAttributes = attributeDict
        textBuffer = ""

        switch elementName {
        case "channel": section = .channel
        case "item":
            section = .item
            itemTitle = ""
            itemDescription = nil
            itemPubDate = nil
            itemDuration = nil
            itemGUID = nil
            itemEnclosureURL = nil
            itemEnclosureMIME = nil
        case "enclosure" where section == .item:
            if let urlStr = attributeDict["url"], let url = URL(string: urlStr) {
                itemEnclosureURL = url
                itemEnclosureMIME = attributeDict["type"]
            }
        case "itunes:image" where section == .channel:
            if channelArtwork == nil,
               let href = attributeDict["href"],
               let url = URL(string: href) {
                channelArtwork = url
            }
        default:
            break
        }
    }

    func parser(_ parser: XMLParser, foundCharacters string: String) {
        textBuffer.append(string)
    }

    func parser(_ parser: XMLParser, foundCDATA CDATABlock: Data) {
        if let str = String(data: CDATABlock, encoding: .utf8) {
            textBuffer.append(str)
        }
    }

    func parser(
        _ parser: XMLParser,
        didEndElement elementName: String,
        namespaceURI: String?,
        qualifiedName qName: String?
    ) {
        let text = textBuffer.trimmingCharacters(in: .whitespacesAndNewlines)

        switch (section, elementName) {
        case (.channel, "title") where channelTitle.isEmpty:
            channelTitle = text
        case (.channel, "itunes:author"):
            channelAuthor = text
        case (.channel, "itunes:summary"), (.channel, "description"):
            if channelSummary == nil || channelSummary?.isEmpty == true {
                channelSummary = text
            }
        case (.channel, "url"):
            // <image><url>...</url></image> — fallback artwork.
            if channelArtwork == nil, let url = URL(string: text) {
                channelArtwork = url
            }

        case (.item, "title"):
            itemTitle = text
        case (.item, "description"), (.item, "itunes:summary"), (.item, "content:encoded"):
            if itemDescription == nil || (itemDescription?.isEmpty ?? true) {
                itemDescription = text
            }
        case (.item, "pubDate"):
            itemPubDate = Self.parseDate(text)
        case (.item, "itunes:duration"):
            itemDuration = Self.parseDuration(text)
        case (.item, "guid"):
            itemGUID = text
        case (.item, "item"):
            break

        default:
            break
        }

        if elementName == "item" {
            if let audioURL = itemEnclosureURL {
                let guid = itemGUID ?? audioURL.absoluteString
                episodes.append(ParsedEpisode(
                    guid: guid,
                    title: itemTitle.isEmpty ? "Untitled" : itemTitle,
                    description: itemDescription,
                    publishedAt: itemPubDate,
                    duration: itemDuration,
                    audioURL: audioURL,
                    audioMimeType: itemEnclosureMIME
                ))
            }
            section = .channel
        }
    }

    private static func parseDate(_ s: String) -> Date? {
        for f in pubDateFormatters {
            if let d = f.date(from: s) { return d }
        }
        return ISO8601DateFormatter().date(from: s)
    }

    private static func parseDuration(_ s: String) -> Double? {
        // iTunes duration can be either seconds or `HH:MM:SS` / `MM:SS`.
        if let asDouble = Double(s) { return asDouble }
        let parts = s.split(separator: ":").compactMap { Double($0) }
        switch parts.count {
        case 2: return parts[0] * 60 + parts[1]
        case 3: return parts[0] * 3600 + parts[1] * 60 + parts[2]
        default: return nil
        }
    }
}
