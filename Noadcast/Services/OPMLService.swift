import Foundation

struct OPMLEntry: Sendable {
    let title: String?
    let feedURL: URL
}

enum OPMLError: LocalizedError {
    case parseFailure
    var errorDescription: String? { "Couldn't parse OPML file." }
}

actor OPMLService {
    static let shared = OPMLService()

    func parse(data: Data) throws -> [OPMLEntry] {
        let parser = OPMLParser()
        guard let entries = parser.parse(data: data) else {
            throw OPMLError.parseFailure
        }
        return entries
    }

    func parse(url: URL) async throws -> [OPMLEntry] {
        let data = try Data(contentsOf: url)
        return try parse(data: data)
    }
}

nonisolated private final class OPMLParser: NSObject, XMLParserDelegate {
    private var entries: [OPMLEntry] = []

    func parse(data: Data) -> [OPMLEntry]? {
        let parser = XMLParser(data: data)
        parser.delegate = self
        guard parser.parse() else { return nil }
        return entries
    }

    func parser(
        _ parser: XMLParser,
        didStartElement elementName: String,
        namespaceURI: String?,
        qualifiedName qName: String?,
        attributes attributeDict: [String: String] = [:]
    ) {
        guard elementName == "outline" else { return }
        let type = attributeDict["type"]?.lowercased()
        let isFeed = type == "rss" || type == "atom" || attributeDict["xmlUrl"] != nil
        guard isFeed,
              let xmlUrl = attributeDict["xmlUrl"],
              let url = URL(string: xmlUrl)
        else { return }
        entries.append(OPMLEntry(
            title: attributeDict["title"] ?? attributeDict["text"],
            feedURL: url
        ))
    }
}
