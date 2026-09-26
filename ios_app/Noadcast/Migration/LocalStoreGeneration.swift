import Foundation
import SwiftData
import os

/// Launch-time store lifecycle, run before the app's `ModelContainer`
/// exists.
///
/// Generation 1 is the previous build's on-device store (its own RSS,
/// downloads and analysis; `@Attribute(.unique)` GUIDs and feed URLs, no
/// server ids). A `SchemaMigrationPlan` cannot get from there to generation 2:
/// the new unique `serverID` has no value until the server assigns one, and
/// dropping unique constraints is not a lightweight migration. So, once:
///
/// 1. open the old store with the frozen `LegacySchemaV1`, export
///    device-local state to `legacy-export.json` (+ an OPML of the feeds);
/// 2. release that container and **move** (never delete) `default.store`,
///    `-wal`, `-shm` into `legacy-store-backup/`;
/// 3. stamp generation 2 in `UserDefaults`;
///
/// then open the new container with wipe-and-retry instead of a
/// `fatalError`. If the legacy store cannot be read it is still moved aside
/// and the app continues — never bricks. `DeviceStateRestoreService` re-applies
/// the export after the server is configured and the first full sync
/// completes.
enum LocalStoreGeneration {
    static let currentGeneration = 2
    static let generationKey = "LocalStoreGeneration"
    /// `exported` | `exportFailed` | `noLegacyStore`.
    static let outcomeKey = "LegacyMigrationOutcome"

    enum Outcome: String {
        case exported
        case exportFailed
        case noLegacyStore
    }

    static var outcome: Outcome? {
        UserDefaults.standard.string(forKey: outcomeKey).flatMap(Outcome.init(rawValue:))
    }

    static var legacyExportURL: URL {
        AudioStorage.applicationSupportDirectory.appendingPathComponent("legacy-export.json")
    }

    static var legacyOPMLURL: URL {
        AudioStorage.applicationSupportDirectory.appendingPathComponent("legacy-subscriptions.opml")
    }

    static var legacyBackupDirectory: URL {
        AudioStorage.applicationSupportDirectory.appendingPathComponent("legacy-store-backup", isDirectory: true)
    }

    static var failedStoreBackupDirectory: URL {
        AudioStorage.applicationSupportDirectory.appendingPathComponent("failed-store-backup", isDirectory: true)
    }

    static var schema: Schema {
        Schema([
            Podcast.self,
            Episode.self,
            AdMarker.self,
            QueueItem.self,
            AppSettings.self,
            UsageHistoryDay.self,
            SyncCursor.self
        ])
    }

    /// Where SwiftData keeps an unnamed on-disk store (`default.store` in
    /// Application Support).
    static var defaultStoreURL: URL {
        ModelConfiguration(isStoredInMemoryOnly: false).url
    }

    // MARK: - Prepare

    /// Idempotent; does real work only on the first launch of generation 2.
    static func prepare() {
        let defaults = UserDefaults.standard
        guard defaults.integer(forKey: generationKey) < currentGeneration else { return }
        let storeURL = defaultStoreURL
        if FileManager.default.fileExists(atPath: storeURL.path) {
            Log.migration.notice("Found a generation-1 store; exporting device-local state")
            let exported = exportLegacyStore(at: storeURL)
            moveStoreFiles(at: storeURL, into: legacyBackupDirectory)
            defaults.set(exported ? Outcome.exported.rawValue : Outcome.exportFailed.rawValue, forKey: outcomeKey)
        } else {
            defaults.set(Outcome.noLegacyStore.rawValue, forKey: outcomeKey)
        }
        defaults.set(currentGeneration, forKey: generationKey)
    }

    /// Opens the generation-2 container. A store that cannot be opened is
    /// moved aside and recreated (the mirror is a cache; device-local state
    /// is re-attached from the server by `feedURL + guid`); as a last resort
    /// the app runs on an in-memory store rather than crashing.
    static func makeContainer() -> ModelContainer {
        let schema = schema
        let configuration = ModelConfiguration(
            schema: schema,
            isStoredInMemoryOnly: false,
            cloudKitDatabase: .none
        )
        do {
            return try ModelContainer(for: schema, configurations: [configuration])
        } catch {
            Log.startup.error("ModelContainer failed: \(Log.describe(error), privacy: .public); moving the store aside and retrying")
        }
        replaceFailedStoreBackup(with: configuration.url)
        do {
            return try ModelContainer(for: schema, configurations: [configuration])
        } catch {
            Log.startup.fault("ModelContainer failed after wipe: \(Log.describe(error), privacy: .public); using an in-memory store")
        }
        let memory = ModelConfiguration(schema: schema, isStoredInMemoryOnly: true, cloudKitDatabase: .none)
        do {
            return try ModelContainer(for: schema, configurations: [memory])
        } catch {
            fatalError("Could not create even an in-memory ModelContainer: \(error)")
        }
    }

    // MARK: - Export

    /// `true` when `legacy-export.json` was written.
    private static func exportLegacyStore(at storeURL: URL) -> Bool {
        var succeeded = false
        autoreleasepool {
            do {
                let snapshot = try readLegacySnapshot(at: storeURL)
                let data = try DeviceStateSnapshot.encode(snapshot)
                try data.write(to: legacyExportURL, options: .atomic)
                let feeds = snapshot.podcasts.map { (url: $0.feedURL, title: $0.title) }
                try? OPMLWriter.document(feeds: feeds).write(to: legacyOPMLURL, options: .atomic)
                Log.migration.notice("Exported \(snapshot.podcasts.count) podcast(s), \(snapshot.episodes.count) episode state(s), \(snapshot.queue.count) queue item(s)")
                succeeded = true
            } catch {
                Log.migration.error("Legacy store could not be read: \(Log.describe(error), privacy: .public)")
            }
        }
        return succeeded
    }

    /// All model objects and the container are released when this returns.
    private static func readLegacySnapshot(at storeURL: URL) throws -> DeviceStateSnapshot {
        let schema = Schema(versionedSchema: LegacySchemaV1.self)
        let configuration = ModelConfiguration(schema: schema, url: storeURL, cloudKitDatabase: .none)
        let container = try ModelContainer(for: schema, configurations: [configuration])
        let context = ModelContext(container)
        context.autosaveEnabled = false

        let podcasts = try context.fetch(FetchDescriptor<LegacySchemaV1.Podcast>())
        let podcastStates = podcasts.map { podcast in
            DeviceStateSnapshot.PodcastState(
                feedURL: podcast.feedURL.absoluteString,
                title: podcast.title,
                autoDownloadEnabled: podcast.autoDownloadEnabled,
                customPlaybackSpeed: podcast.customPlaybackSpeed,
                adAnalysisEnabled: podcast.aiProcessingEnabled
            )
        }

        // Only episodes carrying device-local state; the archive itself comes
        // back from the server.
        let interesting = FetchDescriptor<LegacySchemaV1.Episode>(
            predicate: #Predicate<LegacySchemaV1.Episode> { episode in
                episode.localFilename != nil || episode.playbackPosition > 0 || episode.isPlayed
            }
        )
        var episodeStates: [DeviceStateSnapshot.EpisodeState] = []
        for episode in try context.fetch(interesting) {
            guard let feed = episode.podcast?.feedURL.absoluteString else { continue }
            episodeStates.append(DeviceStateSnapshot.EpisodeState(
                feedURL: feed,
                guid: episode.guid,
                title: episode.title,
                localFilename: episode.localFilename,
                fileSizeBytes: episode.fileSizeBytes,
                audioMimeType: episode.audioMimeType,
                playbackPosition: episode.playbackPosition,
                isPlayed: episode.isPlayed,
                datePlayed: episode.datePlayed
            ))
        }

        var queue: [DeviceStateSnapshot.EpisodeKey] = []
        let queueDescriptor = FetchDescriptor<LegacySchemaV1.QueueItem>(
            sortBy: [SortDescriptor(\LegacySchemaV1.QueueItem.position)]
        )
        for item in try context.fetch(queueDescriptor) {
            guard let episode = item.episode, let feed = episode.podcast?.feedURL.absoluteString else { continue }
            queue.append(DeviceStateSnapshot.EpisodeKey(feedURL: feed, guid: episode.guid))
        }

        var stats: DeviceStateSnapshot.Stats?
        var preferences: DeviceStateSnapshot.Preferences?
        var lastPlayed: DeviceStateSnapshot.EpisodeKey?
        if let settings = try context.fetch(FetchDescriptor<LegacySchemaV1.AppSettings>()).first {
            stats = DeviceStateSnapshot.Stats(
                lifetimePlayedSeconds: settings.lifetimePlayedSeconds,
                lifetimeAdSkipSeconds: settings.lifetimeAdSkipSeconds
            )
            preferences = DeviceStateSnapshot.Preferences(
                defaultPlaybackSpeed: settings.defaultPlaybackSpeed,
                autoDownloadPolicy: settings.autoDownloadPolicyRaw,
                autoDeleteAfterPlayed: settings.autoDeleteAfterPlayed,
                podcastSortMode: settings.podcastSortModeRaw,
                skipAds: settings.skipAds,
                skipIntrosAndOutros: settings.skipIntrosAndOutros,
                chainSkipGapSeconds: settings.chainSkipGapSeconds,
                adAnalysisEnabled: settings.adAnalysisEnabled
            )
            if let guid = settings.lastPlayedEpisodeGUID {
                let lastDescriptor = FetchDescriptor<LegacySchemaV1.Episode>(
                    predicate: #Predicate<LegacySchemaV1.Episode> { $0.guid == guid }
                )
                if let episode = try context.fetch(lastDescriptor).first,
                   let feed = episode.podcast?.feedURL.absoluteString {
                    lastPlayed = DeviceStateSnapshot.EpisodeKey(feedURL: feed, guid: episode.guid)
                }
            }
        }

        let usageDays = try context.fetch(FetchDescriptor<LegacySchemaV1.UsageHistoryDay>()).map { day in
            DeviceStateSnapshot.UsageDay(
                dayStart: day.dayStart,
                playbackSeconds: day.playbackSeconds,
                adSkippedSeconds: day.adSkippedSeconds
            )
        }

        return DeviceStateSnapshot(
            source: .legacyStore,
            podcasts: podcastStates,
            episodes: episodeStates,
            queue: queue,
            lastPlayed: lastPlayed,
            stats: stats,
            usageDays: usageDays,
            preferences: preferences
        )
    }

    // MARK: - Moving stores aside

    /// Moves `<store>`, `<store>-wal`, `<store>-shm` into `directory`,
    /// never overwriting an earlier backup.
    static func moveStoreFiles(at storeURL: URL, into directory: URL) {
        let fileManager = FileManager.default
        try? fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        let stamp = String(Int(Date().timeIntervalSince1970))
        for suffix in ["", "-wal", "-shm"] {
            let source = URL(fileURLWithPath: storeURL.path + suffix)
            guard fileManager.fileExists(atPath: source.path) else { continue }
            var destination = directory.appendingPathComponent(source.lastPathComponent)
            if fileManager.fileExists(atPath: destination.path) {
                destination = directory.appendingPathComponent("\(stamp)-\(source.lastPathComponent)")
            }
            do {
                try fileManager.moveItem(at: source, to: destination)
            } catch {
                Log.migration.error("Could not move \(source.lastPathComponent, privacy: .public) aside: \(error.localizedDescription, privacy: .public)")
            }
        }
    }

    /// Keeps only the most recent unopenable generation-2 store.
    private static func replaceFailedStoreBackup(with storeURL: URL) {
        try? FileManager.default.removeItem(at: failedStoreBackupDirectory)
        moveStoreFiles(at: storeURL, into: failedStoreBackupDirectory)
    }
}
