import Foundation

// Wire types for docs/API.md. JSON is camelCase, so property names match the
// keys and no key strategy is used. Every type decodes leniently: unknown
// fields are ignored, optional fields tolerate absence or a wrong type, and
// unknown enum strings decode to a safe default (`.unknown`). Only the fields
// a row cannot exist without (ids, marker bounds, the sync cursor) are strict.
//
// Every nested type is marked `nonisolated` explicitly: the app target
// defaults to MainActor isolation, and a MainActor `CodingKeys` conformance
// could not be used from the API actor.

// MARK: - Dates

/// RFC 3339 parser that accepts any number of fractional-second digits (or
/// none), `Z`/`z`, `±HH:MM`, `±HHMM`, a space instead of `T`, and a bare
/// `YYYY-MM-DD`. `JSONDecoder`'s `.iso8601` rejects fractional seconds, which
/// is exactly what the server emits (`2026-09-22T18:03:11.123Z`).
nonisolated enum APIDateParser {
    static func parse(_ string: String) -> Date? {
        let bytes = Array(string.trimmingCharacters(in: .whitespaces).utf8)
        let count = bytes.count

        func digits(_ start: Int, _ length: Int) -> Int? {
            guard start >= 0, length > 0, start + length <= count else { return nil }
            var value = 0
            for index in start..<(start + length) {
                let byte = bytes[index]
                guard byte >= 48, byte <= 57 else { return nil }
                value = value * 10 + Int(byte - 48)
            }
            return value
        }
        func byte(_ index: Int) -> UInt8? {
            index < count ? bytes[index] : nil
        }

        guard let year = digits(0, 4), byte(4) == UInt8(ascii: "-"),
              let month = digits(5, 2), byte(7) == UInt8(ascii: "-"),
              let day = digits(8, 2)
        else { return nil }

        var hour = 0
        var minute = 0
        var second = 0
        var fractionNumerator = 0
        var fractionDenominator = 1
        var offsetSeconds = 0
        var index = 10

        if index < count {
            guard let separator = byte(index),
                  separator == UInt8(ascii: "T") || separator == UInt8(ascii: "t") || separator == UInt8(ascii: " ")
            else { return nil }
            index += 1
            guard let h = digits(index, 2), byte(index + 2) == UInt8(ascii: ":"),
                  let m = digits(index + 3, 2)
            else { return nil }
            hour = h
            minute = m
            index += 5
            if byte(index) == UInt8(ascii: ":") {
                guard let s = digits(index + 1, 2) else { return nil }
                second = s
                index += 3
            }
            if let mark = byte(index), mark == UInt8(ascii: ".") || mark == UInt8(ascii: ",") {
                index += 1
                var sawDigit = false
                while let next = byte(index), next >= 48, next <= 57 {
                    // Beyond nanoseconds the digits cannot change a Double date.
                    if fractionDenominator < 1_000_000_000 {
                        fractionNumerator = fractionNumerator * 10 + Int(next - 48)
                        fractionDenominator *= 10
                    }
                    sawDigit = true
                    index += 1
                }
                guard sawDigit else { return nil }
            }
            if let zone = byte(index) {
                if zone == UInt8(ascii: "Z") || zone == UInt8(ascii: "z") {
                    index += 1
                } else if zone == UInt8(ascii: "+") || zone == UInt8(ascii: "-") {
                    let sign = zone == UInt8(ascii: "+") ? 1 : -1
                    index += 1
                    guard let offsetHours = digits(index, 2) else { return nil }
                    index += 2
                    if byte(index) == UInt8(ascii: ":") {
                        index += 1
                    }
                    var offsetMinutes = 0
                    if let m = digits(index, 2) {
                        offsetMinutes = m
                        index += 2
                    }
                    offsetSeconds = sign * (offsetHours * 3600 + offsetMinutes * 60)
                } else {
                    return nil
                }
            }
            guard index == count else { return nil }
        }

        guard (1...12).contains(month), (1...31).contains(day),
              hour <= 23, minute <= 59, second <= 60
        else { return nil }

        let days = daysFromCivil(year: year, month: month, day: day)
        let wholeSeconds = days * 86_400 + hour * 3_600 + minute * 60 + second - offsetSeconds
        let fraction = Double(fractionNumerator) / Double(fractionDenominator)
        return Date(timeIntervalSince1970: Double(wholeSeconds) + fraction)
    }

    /// Days since 1970-01-01 in the proleptic Gregorian calendar
    /// (H. Hinnant's `days_from_civil`). Avoids `Calendar`, which is neither
    /// cheap nor obviously thread-safe to share.
    static func daysFromCivil(year: Int, month: Int, day: Int) -> Int {
        let y = month <= 2 ? year - 1 : year
        let era = (y >= 0 ? y : y - 399) / 400
        let yearOfEra = y - era * 400
        let shiftedMonth = (month + 9) % 12
        let dayOfYear = (153 * shiftedMonth + 2) / 5 + day - 1
        let dayOfEra = yearOfEra * 365 + yearOfEra / 4 - yearOfEra / 100 + dayOfYear
        return era * 146_097 + dayOfEra - 719_468
    }
}

nonisolated enum APIJSON {
    static func makeDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            if let text = try? container.decode(String.self) {
                if let date = APIDateParser.parse(text) {
                    return date
                }
                throw DecodingError.dataCorruptedError(
                    in: container,
                    debugDescription: "Unrecognised timestamp \(text)"
                )
            }
            let seconds = try container.decode(Double.self)
            return Date(timeIntervalSince1970: seconds)
        }
        return decoder
    }
}

// MARK: - Podcast

nonisolated struct PodcastDTO: Decodable, Sendable, Equatable {
    let id: Int
    let feedUrl: String
    let title: String
    let author: String?
    let summary: String?
    let artworkUrl: String?
    let language: String?
    let link: String?
    let autoProcessEnabled: Bool
    let adAnalysisEnabled: Bool
    let episodeCount: Int
    let latestEpisodeAt: Date?
    let lastFetchAt: Date?
    let lastFetchError: String?
    let createdAt: Date?
    let updatedAt: Date?
    let seq: Int?

    init(
        id: Int,
        feedUrl: String,
        title: String,
        author: String? = nil,
        summary: String? = nil,
        artworkUrl: String? = nil,
        language: String? = nil,
        link: String? = nil,
        autoProcessEnabled: Bool = true,
        adAnalysisEnabled: Bool = true,
        episodeCount: Int = 0,
        latestEpisodeAt: Date? = nil,
        lastFetchAt: Date? = nil,
        lastFetchError: String? = nil,
        createdAt: Date? = nil,
        updatedAt: Date? = nil,
        seq: Int? = nil
    ) {
        self.id = id
        self.feedUrl = feedUrl
        self.title = title
        self.author = author
        self.summary = summary
        self.artworkUrl = artworkUrl
        self.language = language
        self.link = link
        self.autoProcessEnabled = autoProcessEnabled
        self.adAnalysisEnabled = adAnalysisEnabled
        self.episodeCount = episodeCount
        self.latestEpisodeAt = latestEpisodeAt
        self.lastFetchAt = lastFetchAt
        self.lastFetchError = lastFetchError
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.seq = seq
    }

    nonisolated enum CodingKeys: String, CodingKey {
        case id, feedUrl, title, author, summary, artworkUrl, language, link
        case autoProcessEnabled, adAnalysisEnabled, episodeCount
        case latestEpisodeAt, lastFetchAt, lastFetchError, createdAt, updatedAt, seq
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        feedUrl = (try? c.decodeIfPresent(String.self, forKey: .feedUrl)) ?? ""
        title = (try? c.decodeIfPresent(String.self, forKey: .title)) ?? ""
        author = try? c.decodeIfPresent(String.self, forKey: .author)
        summary = try? c.decodeIfPresent(String.self, forKey: .summary)
        artworkUrl = try? c.decodeIfPresent(String.self, forKey: .artworkUrl)
        language = try? c.decodeIfPresent(String.self, forKey: .language)
        link = try? c.decodeIfPresent(String.self, forKey: .link)
        autoProcessEnabled = (try? c.decodeIfPresent(Bool.self, forKey: .autoProcessEnabled)) ?? true
        adAnalysisEnabled = (try? c.decodeIfPresent(Bool.self, forKey: .adAnalysisEnabled)) ?? true
        episodeCount = (try? c.decodeIfPresent(Int.self, forKey: .episodeCount)) ?? 0
        latestEpisodeAt = try? c.decodeIfPresent(Date.self, forKey: .latestEpisodeAt)
        lastFetchAt = try? c.decodeIfPresent(Date.self, forKey: .lastFetchAt)
        lastFetchError = try? c.decodeIfPresent(String.self, forKey: .lastFetchError)
        createdAt = try? c.decodeIfPresent(Date.self, forKey: .createdAt)
        updatedAt = try? c.decodeIfPresent(Date.self, forKey: .updatedAt)
        seq = try? c.decodeIfPresent(Int.self, forKey: .seq)
    }
}

// MARK: - Episode

nonisolated struct AdMarkerDTO: Decodable, Sendable, Equatable {
    let id: Int?
    let startSeconds: Double
    let endSeconds: Double
    /// Raw kind string. Unknown kinds are kept as skip segments of kind `.ad`
    /// (see `segmentKind`); the server only ever sends skip segments.
    let kind: String
    let summary: String?
    let source: String?

    init(
        id: Int? = nil,
        startSeconds: Double,
        endSeconds: Double,
        kind: String = SegmentKind.ad.rawValue,
        summary: String? = nil,
        source: String? = "auto"
    ) {
        self.id = id
        self.startSeconds = startSeconds
        self.endSeconds = endSeconds
        self.kind = kind
        self.summary = summary
        self.source = source
    }

    nonisolated enum CodingKeys: String, CodingKey {
        case id, startSeconds, endSeconds, kind, summary, source
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try? c.decodeIfPresent(Int.self, forKey: .id)
        startSeconds = try c.decode(Double.self, forKey: .startSeconds)
        endSeconds = try c.decode(Double.self, forKey: .endSeconds)
        kind = (try? c.decodeIfPresent(String.self, forKey: .kind)) ?? SegmentKind.ad.rawValue
        summary = try? c.decodeIfPresent(String.self, forKey: .summary)
        source = try? c.decodeIfPresent(String.self, forKey: .source)
    }

    var segmentKind: SegmentKind {
        SegmentKind(rawValue: kind) ?? .ad
    }

    var isManual: Bool {
        source == "manual"
    }
}

nonisolated struct EpisodeDTO: Decodable, Sendable, Equatable {
    let id: Int
    let podcastId: Int
    let guid: String
    let title: String
    let description: String?
    let publishedAt: Date?
    let durationSeconds: Double?
    let durationIsMeasured: Bool
    let enclosureUrl: String?
    let enclosureType: String?
    let artworkUrl: String?
    let audioState: ServerAudioState
    let audioBytes: Int64?
    let audioSha256: String?
    let audioContentType: String?
    let state: ServerEpisodeState
    let error: String?
    let transcriptState: ServerTranscriptState
    let classifyState: ServerClassifyState
    let markerRevision: Int
    let adMarkers: [AdMarkerDTO]
    let updatedAt: Date?
    let seq: Int?

    init(
        id: Int,
        podcastId: Int,
        guid: String,
        title: String,
        description: String? = nil,
        publishedAt: Date? = nil,
        durationSeconds: Double? = nil,
        durationIsMeasured: Bool = false,
        enclosureUrl: String? = nil,
        enclosureType: String? = nil,
        artworkUrl: String? = nil,
        audioState: ServerAudioState = .absent,
        audioBytes: Int64? = nil,
        audioSha256: String? = nil,
        audioContentType: String? = nil,
        state: ServerEpisodeState = .discovered,
        error: String? = nil,
        transcriptState: ServerTranscriptState = .notStarted,
        classifyState: ServerClassifyState = .notStarted,
        markerRevision: Int = 0,
        adMarkers: [AdMarkerDTO] = [],
        updatedAt: Date? = nil,
        seq: Int? = nil
    ) {
        self.id = id
        self.podcastId = podcastId
        self.guid = guid
        self.title = title
        self.description = description
        self.publishedAt = publishedAt
        self.durationSeconds = durationSeconds
        self.durationIsMeasured = durationIsMeasured
        self.enclosureUrl = enclosureUrl
        self.enclosureType = enclosureType
        self.artworkUrl = artworkUrl
        self.audioState = audioState
        self.audioBytes = audioBytes
        self.audioSha256 = audioSha256
        self.audioContentType = audioContentType
        self.state = state
        self.error = error
        self.transcriptState = transcriptState
        self.classifyState = classifyState
        self.markerRevision = markerRevision
        self.adMarkers = adMarkers
        self.updatedAt = updatedAt
        self.seq = seq
    }

    nonisolated enum CodingKeys: String, CodingKey {
        case id, podcastId, guid, title, description, publishedAt
        case durationSeconds, durationIsMeasured, enclosureUrl, enclosureType, artworkUrl
        case audioState, audioBytes, audioSha256, audioContentType
        case state, error, transcriptState, classifyState, markerRevision, adMarkers
        case updatedAt, seq
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        podcastId = try c.decode(Int.self, forKey: .podcastId)
        guid = (try? c.decodeIfPresent(String.self, forKey: .guid)) ?? ""
        title = (try? c.decodeIfPresent(String.self, forKey: .title)) ?? "Untitled"
        description = try? c.decodeIfPresent(String.self, forKey: .description)
        publishedAt = try? c.decodeIfPresent(Date.self, forKey: .publishedAt)
        durationSeconds = try? c.decodeIfPresent(Double.self, forKey: .durationSeconds)
        durationIsMeasured = (try? c.decodeIfPresent(Bool.self, forKey: .durationIsMeasured)) ?? false
        enclosureUrl = try? c.decodeIfPresent(String.self, forKey: .enclosureUrl)
        enclosureType = try? c.decodeIfPresent(String.self, forKey: .enclosureType)
        artworkUrl = try? c.decodeIfPresent(String.self, forKey: .artworkUrl)
        audioState = (try? c.decodeIfPresent(ServerAudioState.self, forKey: .audioState)) ?? .absent
        audioBytes = try? c.decodeIfPresent(Int64.self, forKey: .audioBytes)
        audioSha256 = try? c.decodeIfPresent(String.self, forKey: .audioSha256)
        audioContentType = try? c.decodeIfPresent(String.self, forKey: .audioContentType)
        state = (try? c.decodeIfPresent(ServerEpisodeState.self, forKey: .state)) ?? .discovered
        error = try? c.decodeIfPresent(String.self, forKey: .error)
        transcriptState = (try? c.decodeIfPresent(ServerTranscriptState.self, forKey: .transcriptState)) ?? .notStarted
        classifyState = (try? c.decodeIfPresent(ServerClassifyState.self, forKey: .classifyState)) ?? .notStarted
        markerRevision = (try? c.decodeIfPresent(Int.self, forKey: .markerRevision)) ?? 0
        // Strict on purpose: the marker set replaces the local one wholesale,
        // so a silently-empty array would erase every marker.
        adMarkers = try c.decodeIfPresent([AdMarkerDTO].self, forKey: .adMarkers) ?? []
        updatedAt = try? c.decodeIfPresent(Date.self, forKey: .updatedAt)
        seq = try? c.decodeIfPresent(Int.self, forKey: .seq)
    }
}

// MARK: - Sync

nonisolated struct DeletionDTO: Decodable, Sendable, Equatable {
    /// `"podcast"` (the podcast and all its episodes) or `"episode"`. Unknown
    /// entities are ignored.
    let entity: String
    let id: Int

    init(entity: String, id: Int) {
        self.entity = entity
        self.id = id
    }

    nonisolated enum CodingKeys: String, CodingKey {
        case entity, id
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        entity = (try? c.decodeIfPresent(String.self, forKey: .entity)) ?? ""
        id = try c.decode(Int.self, forKey: .id)
    }
}

nonisolated struct ServerSettingsDTO: Decodable, Sendable, Equatable {
    let adAnalysisEnabled: Bool
    let autoProcessEnabled: Bool
    let classifier: String?
    let classifierModel: String?
    /// Optional for compatibility with servers predating polling settings.
    let feedIntervalMinutes: Int?
    /// Which providers have server-side keys. Keys themselves are never sent.
    let availableClassifiers: [String: Bool]

    init(
        adAnalysisEnabled: Bool = true,
        autoProcessEnabled: Bool = true,
        classifier: String? = nil,
        classifierModel: String? = nil,
        feedIntervalMinutes: Int? = nil,
        availableClassifiers: [String: Bool] = [:]
    ) {
        self.adAnalysisEnabled = adAnalysisEnabled
        self.autoProcessEnabled = autoProcessEnabled
        self.classifier = classifier
        self.classifierModel = classifierModel
        self.feedIntervalMinutes = feedIntervalMinutes
        self.availableClassifiers = availableClassifiers
    }

    nonisolated enum CodingKeys: String, CodingKey {
        case adAnalysisEnabled, autoProcessEnabled, classifier, classifierModel, availableClassifiers
        case feedIntervalMinutes
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        adAnalysisEnabled = (try? c.decodeIfPresent(Bool.self, forKey: .adAnalysisEnabled)) ?? true
        autoProcessEnabled = (try? c.decodeIfPresent(Bool.self, forKey: .autoProcessEnabled)) ?? true
        classifier = try? c.decodeIfPresent(String.self, forKey: .classifier)
        classifierModel = try? c.decodeIfPresent(String.self, forKey: .classifierModel)
        feedIntervalMinutes = try? c.decodeIfPresent(Int.self, forKey: .feedIntervalMinutes)
        availableClassifiers = (try? c.decodeIfPresent([String: Bool].self, forKey: .availableClassifiers)) ?? [:]
    }
}

nonisolated struct SyncPageDTO: Decodable, Sendable, Equatable {
    /// `nil` only from a server that predates the field; the instance check
    /// is skipped rather than wiping the mirror.
    let instanceId: String?
    let podcasts: [PodcastDTO]
    let episodes: [EpisodeDTO]
    let deletions: [DeletionDTO]
    let settings: ServerSettingsDTO?
    let nextSince: Int
    let hasMore: Bool
    let serverTime: Date?

    init(
        instanceId: String?,
        podcasts: [PodcastDTO] = [],
        episodes: [EpisodeDTO] = [],
        deletions: [DeletionDTO] = [],
        settings: ServerSettingsDTO? = nil,
        nextSince: Int,
        hasMore: Bool = false,
        serverTime: Date? = nil
    ) {
        self.instanceId = instanceId
        self.podcasts = podcasts
        self.episodes = episodes
        self.deletions = deletions
        self.settings = settings
        self.nextSince = nextSince
        self.hasMore = hasMore
        self.serverTime = serverTime
    }

    nonisolated enum CodingKeys: String, CodingKey {
        case instanceId, podcasts, episodes, deletions, settings, nextSince, hasMore, serverTime
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        instanceId = try? c.decodeIfPresent(String.self, forKey: .instanceId)
        podcasts = try c.decodeIfPresent([PodcastDTO].self, forKey: .podcasts) ?? []
        episodes = try c.decodeIfPresent([EpisodeDTO].self, forKey: .episodes) ?? []
        deletions = try c.decodeIfPresent([DeletionDTO].self, forKey: .deletions) ?? []
        settings = try? c.decodeIfPresent(ServerSettingsDTO.self, forKey: .settings)
        nextSince = try c.decode(Int.self, forKey: .nextSince)
        hasMore = (try? c.decodeIfPresent(Bool.self, forKey: .hasMore)) ?? false
        serverTime = try? c.decodeIfPresent(Date.self, forKey: .serverTime)
    }
}

// MARK: - Health / session

nonisolated struct HealthDTO: Decodable, Sendable, Equatable {
    let status: String
    let version: String?
    let apiVersion: Int?
    let instanceId: String?
    let authRequired: Bool
    let capabilities: [String]

    init(
        status: String = "ok",
        version: String? = nil,
        apiVersion: Int? = nil,
        instanceId: String? = nil,
        authRequired: Bool = true,
        capabilities: [String] = []
    ) {
        self.status = status
        self.version = version
        self.apiVersion = apiVersion
        self.instanceId = instanceId
        self.authRequired = authRequired
        self.capabilities = capabilities
    }

    nonisolated enum CodingKeys: String, CodingKey {
        case status, version, apiVersion, instanceId, authRequired, capabilities
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        status = (try? c.decodeIfPresent(String.self, forKey: .status)) ?? "unknown"
        version = try? c.decodeIfPresent(String.self, forKey: .version)
        apiVersion = try? c.decodeIfPresent(Int.self, forKey: .apiVersion)
        instanceId = try? c.decodeIfPresent(String.self, forKey: .instanceId)
        authRequired = (try? c.decodeIfPresent(Bool.self, forKey: .authRequired)) ?? true
        capabilities = (try? c.decodeIfPresent([String].self, forKey: .capabilities)) ?? []
    }
}

nonisolated struct SessionDTO: Decodable, Sendable, Equatable {
    let authenticated: Bool
    let serverTime: Date?

    init(authenticated: Bool, serverTime: Date? = nil) {
        self.authenticated = authenticated
        self.serverTime = serverTime
    }

    nonisolated enum CodingKeys: String, CodingKey {
        case authenticated, serverTime
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        authenticated = (try? c.decodeIfPresent(Bool.self, forKey: .authenticated)) ?? false
        serverTime = try? c.decodeIfPresent(Date.self, forKey: .serverTime)
    }
}

// MARK: - Jobs

nonisolated struct ActiveJobDTO: Decodable, Sendable, Equatable {
    let episodeId: Int
    let state: ServerEpisodeState
    let stage: JobStage
    let current: Double?
    let total: Double?
    let statusText: String?
    let updatedAt: Date?

    init(
        episodeId: Int,
        state: ServerEpisodeState,
        stage: JobStage,
        current: Double? = nil,
        total: Double? = nil,
        statusText: String? = nil,
        updatedAt: Date? = nil
    ) {
        self.episodeId = episodeId
        self.state = state
        self.stage = stage
        self.current = current
        self.total = total
        self.statusText = statusText
        self.updatedAt = updatedAt
    }

    nonisolated enum CodingKeys: String, CodingKey {
        case episodeId, state, stage, current, total, statusText, updatedAt
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        episodeId = try c.decode(Int.self, forKey: .episodeId)
        state = (try? c.decodeIfPresent(ServerEpisodeState.self, forKey: .state)) ?? .unknown
        stage = (try? c.decodeIfPresent(JobStage.self, forKey: .stage)) ?? .unknown
        current = try? c.decodeIfPresent(Double.self, forKey: .current)
        total = try? c.decodeIfPresent(Double.self, forKey: .total)
        statusText = try? c.decodeIfPresent(String.self, forKey: .statusText)
        updatedAt = try? c.decodeIfPresent(Date.self, forKey: .updatedAt)
    }

    /// Determinate fraction when the stage reports both ends.
    var fraction: Double? {
        guard let current, let total, total > 0 else { return nil }
        return max(0, min(1, current / total))
    }
}

nonisolated struct ActiveJobsDTO: Decodable, Sendable, Equatable {
    let items: [ActiveJobDTO]

    init(items: [ActiveJobDTO]) {
        self.items = items
    }

    nonisolated enum CodingKeys: String, CodingKey {
        case items
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        items = try c.decodeIfPresent([ActiveJobDTO].self, forKey: .items) ?? []
    }
}

/// `GET /jobs/active` with `If-None-Match`: a 304 means nothing changed and
/// must cause zero SwiftData writes.
nonisolated enum ActiveJobsResult: Sendable, Equatable {
    case notModified
    case updated(items: [ActiveJobDTO], etag: String?)
}

// MARK: - Mutations

nonisolated struct SubscribeResponseDTO: Decodable, Sendable, Equatable {
    let podcast: PodcastDTO
    let jobId: Int?

    init(podcast: PodcastDTO, jobId: Int? = nil) {
        self.podcast = podcast
        self.jobId = jobId
    }

    nonisolated enum CodingKeys: String, CodingKey {
        case podcast, jobId
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        podcast = try c.decode(PodcastDTO.self, forKey: .podcast)
        jobId = try? c.decodeIfPresent(Int.self, forKey: .jobId)
    }
}

nonisolated struct JobResponseDTO: Decodable, Sendable, Equatable {
    let jobId: Int?

    init(jobId: Int?) {
        self.jobId = jobId
    }

    nonisolated enum CodingKeys: String, CodingKey {
        case jobId
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        jobId = try? c.decodeIfPresent(Int.self, forKey: .jobId)
    }
}

nonisolated struct AudioURLDTO: Decodable, Sendable, Equatable {
    /// Resolve against the configured base URL (the server may not know the
    /// address the phone uses to reach it, e.g. a tailnet name).
    let path: String?
    let url: String?
    let expiresAt: Date?

    init(path: String?, url: String? = nil, expiresAt: Date? = nil) {
        self.path = path
        self.url = url
        self.expiresAt = expiresAt
    }

    nonisolated enum CodingKeys: String, CodingKey {
        case path, url, expiresAt
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        path = try? c.decodeIfPresent(String.self, forKey: .path)
        url = try? c.decodeIfPresent(String.self, forKey: .url)
        expiresAt = try? c.decodeIfPresent(Date.self, forKey: .expiresAt)
    }
}

/// A playable, signed stream URL minted by `POST /episodes/{id}/audio-url`.
nonisolated struct SignedAudioURL: Sendable, Equatable {
    let url: URL
    let expiresAt: Date?
}

nonisolated enum AudioReleaseReason: String, Sendable {
    case played
    case manual
}

// MARK: - Usage

nonisolated struct UsageDayDTO: Decodable, Sendable, Equatable, Identifiable {
    /// `YYYY-MM-DD` (server-local calendar day).
    let date: String
    let calls: Int
    let inputTokens: Int
    let thoughtTokens: Int
    let outputTokens: Int
    let costUsd: Double

    var id: String { date }

    /// Local midnight of the calendar day, so charts bucket it on the day
    /// the server named rather than shifting it across a UTC boundary.
    var day: Date? {
        let parts = date.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return nil }
        return Calendar.current.date(from: DateComponents(year: parts[0], month: parts[1], day: parts[2]))
    }
    var totalTokens: Int { inputTokens + thoughtTokens + outputTokens }

    init(date: String, calls: Int = 0, inputTokens: Int = 0, thoughtTokens: Int = 0, outputTokens: Int = 0, costUsd: Double = 0) {
        self.date = date
        self.calls = calls
        self.inputTokens = inputTokens
        self.thoughtTokens = thoughtTokens
        self.outputTokens = outputTokens
        self.costUsd = costUsd
    }

    nonisolated enum CodingKeys: String, CodingKey {
        case date, calls, inputTokens, thoughtTokens, outputTokens, costUsd
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        date = (try? c.decodeIfPresent(String.self, forKey: .date)) ?? ""
        calls = (try? c.decodeIfPresent(Int.self, forKey: .calls)) ?? 0
        inputTokens = (try? c.decodeIfPresent(Int.self, forKey: .inputTokens)) ?? 0
        thoughtTokens = (try? c.decodeIfPresent(Int.self, forKey: .thoughtTokens)) ?? 0
        outputTokens = (try? c.decodeIfPresent(Int.self, forKey: .outputTokens)) ?? 0
        costUsd = (try? c.decodeIfPresent(Double.self, forKey: .costUsd)) ?? 0
    }
}

nonisolated struct UsageModelDTO: Decodable, Sendable, Equatable, Identifiable {
    let provider: String
    let model: String
    let calls: Int
    let inputTokens: Int
    let thoughtTokens: Int
    let outputTokens: Int
    let costUsd: Double

    var id: String { "\(provider)/\(model)" }
    var totalTokens: Int { inputTokens + thoughtTokens + outputTokens }

    init(provider: String, model: String, calls: Int = 0, inputTokens: Int = 0, thoughtTokens: Int = 0, outputTokens: Int = 0, costUsd: Double = 0) {
        self.provider = provider
        self.model = model
        self.calls = calls
        self.inputTokens = inputTokens
        self.thoughtTokens = thoughtTokens
        self.outputTokens = outputTokens
        self.costUsd = costUsd
    }

    nonisolated enum CodingKeys: String, CodingKey {
        case provider, model, calls, inputTokens, thoughtTokens, outputTokens, costUsd
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        provider = (try? c.decodeIfPresent(String.self, forKey: .provider)) ?? "unknown"
        model = (try? c.decodeIfPresent(String.self, forKey: .model)) ?? "unknown"
        calls = (try? c.decodeIfPresent(Int.self, forKey: .calls)) ?? 0
        inputTokens = (try? c.decodeIfPresent(Int.self, forKey: .inputTokens)) ?? 0
        thoughtTokens = (try? c.decodeIfPresent(Int.self, forKey: .thoughtTokens)) ?? 0
        outputTokens = (try? c.decodeIfPresent(Int.self, forKey: .outputTokens)) ?? 0
        costUsd = (try? c.decodeIfPresent(Double.self, forKey: .costUsd)) ?? 0
    }
}

nonisolated struct UsageTotalsDTO: Decodable, Sendable, Equatable {
    let calls: Int
    let inputTokens: Int
    let thoughtTokens: Int
    let outputTokens: Int
    let costUsd: Double

    var totalTokens: Int { inputTokens + thoughtTokens + outputTokens }

    init(calls: Int = 0, inputTokens: Int = 0, thoughtTokens: Int = 0, outputTokens: Int = 0, costUsd: Double = 0) {
        self.calls = calls
        self.inputTokens = inputTokens
        self.thoughtTokens = thoughtTokens
        self.outputTokens = outputTokens
        self.costUsd = costUsd
    }

    nonisolated enum CodingKeys: String, CodingKey {
        case calls, inputTokens, thoughtTokens, outputTokens, costUsd
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        calls = (try? c.decodeIfPresent(Int.self, forKey: .calls)) ?? 0
        inputTokens = (try? c.decodeIfPresent(Int.self, forKey: .inputTokens)) ?? 0
        thoughtTokens = (try? c.decodeIfPresent(Int.self, forKey: .thoughtTokens)) ?? 0
        outputTokens = (try? c.decodeIfPresent(Int.self, forKey: .outputTokens)) ?? 0
        costUsd = (try? c.decodeIfPresent(Double.self, forKey: .costUsd)) ?? 0
    }
}

nonisolated struct UsageDTO: Decodable, Sendable, Equatable {
    let days: [UsageDayDTO]
    let byModel: [UsageModelDTO]
    let totals: UsageTotalsDTO?

    init(days: [UsageDayDTO] = [], byModel: [UsageModelDTO] = [], totals: UsageTotalsDTO? = nil) {
        self.days = days
        self.byModel = byModel
        self.totals = totals
    }

    nonisolated enum CodingKeys: String, CodingKey {
        case days, byModel, totals
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        days = (try? c.decodeIfPresent([UsageDayDTO].self, forKey: .days)) ?? []
        byModel = (try? c.decodeIfPresent([UsageModelDTO].self, forKey: .byModel)) ?? []
        totals = try? c.decodeIfPresent(UsageTotalsDTO.self, forKey: .totals)
    }
}

// MARK: - OPML

nonisolated struct OPMLFailureDTO: Decodable, Sendable, Equatable {
    let feedUrl: String
    let error: String?

    init(feedUrl: String, error: String? = nil) {
        self.feedUrl = feedUrl
        self.error = error
    }

    nonisolated enum CodingKeys: String, CodingKey {
        case feedUrl, error
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        feedUrl = (try? c.decodeIfPresent(String.self, forKey: .feedUrl)) ?? ""
        error = try? c.decodeIfPresent(String.self, forKey: .error)
    }
}

nonisolated struct OPMLImportResultDTO: Decodable, Sendable, Equatable {
    let added: [PodcastDTO]
    let existing: [PodcastDTO]
    let failed: [OPMLFailureDTO]

    init(added: [PodcastDTO] = [], existing: [PodcastDTO] = [], failed: [OPMLFailureDTO] = []) {
        self.added = added
        self.existing = existing
        self.failed = failed
    }

    nonisolated enum CodingKeys: String, CodingKey {
        case added, existing, failed
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        added = (try? c.decodeIfPresent([PodcastDTO].self, forKey: .added)) ?? []
        existing = (try? c.decodeIfPresent([PodcastDTO].self, forKey: .existing)) ?? []
        failed = (try? c.decodeIfPresent([OPMLFailureDTO].self, forKey: .failed)) ?? []
    }
}

// MARK: - Helpers

nonisolated enum LenientURL {
    /// `URL(string:)`, falling back to percent-encoding for feeds with stray
    /// spaces or non-ASCII characters.
    static func make(_ string: String?) -> URL? {
        guard let raw = string?.trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty else {
            return nil
        }
        if let url = URL(string: raw) {
            return url
        }
        return raw.addingPercentEncoding(withAllowedCharacters: .urlFragmentAllowed)
            .flatMap { URL(string: $0) }
    }
}
