import Foundation
import SwiftData

@Model
final class Episode {
    /// Unique GUID from the RSS `<guid>` element (falls back to the enclosure URL).
    @Attribute(.unique) var guid: String

    var title: String
    var episodeDescription: String?
    var publishedAt: Date?
    var duration: Double?
    var audioURL: URL
    var audioMimeType: String?

    /// Relative path inside the app's `Application Support/episodes/` directory,
    /// once downloaded. `nil` means not on disk.
    var localFilename: String?
    var fileSizeBytes: Int64?

    /// Underlying storage for `processingState`. Stored as `String` rather
    /// than the enum directly so SwiftData `#Predicate` queries can filter
    /// by state — the macro can't handle Codable enum cases as predicate
    /// operands.
    var processingStateRaw: String = EpisodeProcessingState.new.rawValue

    /// Denormalized flag mirroring "is this episode currently being processed
    /// in some way?". Maintained by the `processingState` setter; predicates
    /// on a `Bool` are trivial for SwiftData to translate to SQL, unlike
    /// multi-clause `||` chains over an enum's raw value.
    var isInProgress: Bool = false

    var processingState: EpisodeProcessingState {
        get { EpisodeProcessingState(rawValue: processingStateRaw) ?? .new }
        set {
            processingStateRaw = newValue.rawValue
            switch newValue {
            case .downloading, .uploading, .transcribing, .detectingAds:
                isInProgress = true
            case .new, .downloaded, .ready, .failed:
                isInProgress = false
            }
        }
    }

    var processingError: String?
    /// Stage-relative progress in `[0, 1]`. Resets at every stage transition,
    /// so 1.0 means "this stage is done" (the next will then start at 0).
    /// Drives the `ProgressView` fill.
    var processingProgress: Double
    /// Stage-relative current value in the stage's natural unit. Interpret
    /// with `processingState`:
    /// * `.downloading` — bytes written
    /// * `.transcribing` — seconds of audio transcribed
    /// * `.detectingAds` — number of LLM chunks completed
    var processingCurrent: Double?
    /// Stage-relative total value in the stage's natural unit. Same unit
    /// scheme as `processingCurrent`.
    var processingTotal: Double?

    /// Last playback position in seconds.
    var playbackPosition: Double
    var isPlayed: Bool
    var datePlayed: Date?

    var podcast: Podcast?

    @Relationship(deleteRule: .cascade, inverse: \TranscriptSegment.episode)
    var transcript: [TranscriptSegment] = []

    @Relationship(deleteRule: .cascade, inverse: \AdMarker.episode)
    var adMarkers: [AdMarker] = []

    init(
        guid: String,
        title: String,
        episodeDescription: String? = nil,
        publishedAt: Date? = nil,
        duration: Double? = nil,
        audioURL: URL,
        audioMimeType: String? = nil,
        podcast: Podcast? = nil
    ) {
        self.guid = guid
        self.title = title
        self.episodeDescription = episodeDescription
        self.publishedAt = publishedAt
        self.duration = duration
        self.audioURL = audioURL
        self.audioMimeType = audioMimeType
        self.podcast = podcast
        // `processingStateRaw` already defaults to `.new`'s raw value.
        self.processingProgress = 0
        self.playbackPosition = 0
        self.isPlayed = false
    }

    var localFileURL: URL? {
        guard let localFilename else { return nil }
        return Episode.episodesDirectory.appendingPathComponent(localFilename)
    }

    var hasLocalFile: Bool {
        guard let url = localFileURL else { return false }
        return FileManager.default.fileExists(atPath: url.path)
    }

    static var episodesDirectory: URL = {
        let base = try! FileManager.default.url(
            for: .applicationSupportDirectory,
            in: .userDomainMask,
            appropriateFor: nil,
            create: true
        )
        let dir = base.appendingPathComponent("episodes", isDirectory: true)
        if !FileManager.default.fileExists(atPath: dir.path) {
            try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        }
        return dir
    }()
}
