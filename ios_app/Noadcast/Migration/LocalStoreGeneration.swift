import Foundation
import SwiftData
import os

/// Chooses the SwiftData store before the app constructs its container.
/// The previous on-device schema cannot be opened as the server-backed
/// schema. Starting at a different URL avoids opening that old store at all.
enum LocalStoreGeneration {
    static let currentGeneration = 2
    static let generationKey = "LocalStoreGeneration"
    static let freshStoreKey = "UsesServerBackedStore"
    static let outcomeKey = "LegacyMigrationOutcome"

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

    /// SwiftData's unnamed store in Application Support.
    static var defaultStoreURL: URL {
        ModelConfiguration(isStoredInMemoryOnly: false).url
    }

    static var activeStoreURL: URL {
        storeURL(defaultStoreURL: defaultStoreURL, defaults: .standard)
    }

    static var failedStoreBackupDirectory: URL {
        AudioStorage.applicationSupportDirectory.appendingPathComponent("failed-store-backup", isDirectory: true)
    }

    /// On the first server-backed launch, leave an old database untouched and
    /// use a separate path. On later launches, preserve whichever current
    /// store was selected. Also retire any restore job left by older builds.
    static func prepare() {
        prepare(
            defaultStoreURL: defaultStoreURL,
            defaults: .standard,
            jobsDirectory: AudioStorage.applicationSupportDirectory.appendingPathComponent("pending-restores", isDirectory: true)
        )
    }

    static func prepare(defaultStoreURL: URL, defaults: UserDefaults, jobsDirectory: URL) {
        discardLegacyRestoreState(defaults: defaults, jobsDirectory: jobsDirectory)

        guard defaults.integer(forKey: generationKey) < currentGeneration else { return }
        if FileManager.default.fileExists(atPath: defaultStoreURL.path) {
            // Do not ask SwiftData to open or migrate the incompatible store.
            // The old file stays in place; the new path is stable across relaunches.
            defaults.set(true, forKey: freshStoreKey)
            Log.migration.notice("Previous local store found; starting with a fresh server-backed store")
        }
        defaults.set(currentGeneration, forKey: generationKey)
    }

    static func storeURL(defaultStoreURL: URL, defaults: UserDefaults) -> URL {
        guard defaults.bool(forKey: freshStoreKey) else { return defaultStoreURL }
        return defaultStoreURL.deletingLastPathComponent().appendingPathComponent("noadcast-server.store")
    }

    /// An older build may have queued an export restore indefinitely. Drop
    /// only those jobs; instance-change and local-reset jobs remain intact.
    static func discardLegacyRestoreState(defaults: UserDefaults, jobsDirectory: URL) {
        let fileManager = FileManager.default
        let names = (try? fileManager.contentsOfDirectory(atPath: jobsDirectory.path)) ?? []
        var removedJob = false
        for name in names where name.hasSuffix(".json") {
            let url = jobsDirectory.appendingPathComponent(name)
            guard let data = try? Data(contentsOf: url),
                  let job = try? JSONDecoder().decode(RestoreJob.self, from: data),
                  job.snapshot.source == .legacyStore else { continue }
            do {
                try fileManager.removeItem(at: url)
                removedJob = true
            } catch {
                Log.migration.error("Could not discard legacy restore job: \(Log.describe(error), privacy: .public)")
            }
        }

        let previousOutcome = defaults.string(forKey: outcomeKey)
        let oldStatus = defaults.string(forKey: DeviceStateRestoreService.statusDefaultsKey) ?? ""
        let isLegacyStatus = oldStatus.hasPrefix("Migration ")
            || oldStatus.hasPrefix("Migrated the previous library")
            || oldStatus.hasPrefix("The previous library")
        if removedJob || previousOutcome == "exported" || previousOutcome == "exportFailed" || isLegacyStatus {
            defaults.removeObject(forKey: DeviceStateRestoreService.statusDefaultsKey)
        }
        // Prevent older exports from being imported after this startup.
        defaults.set(true, forKey: DeviceStateRestoreService.legacyImportedDefaultsKey)
        defaults.removeObject(forKey: outcomeKey)
    }

    /// Opens the selected server-backed store. An unopenable current store is
    /// moved aside and recreated; if storage itself is unavailable, the app
    /// can still launch with an in-memory container.
    static func makeContainer() -> ModelContainer {
        let schema = schema
        let configuration = ModelConfiguration(schema: schema, url: activeStoreURL, cloudKitDatabase: .none)
        do {
            return try ModelContainer(for: schema, configurations: [configuration])
        } catch {
            Log.startup.error("ModelContainer failed: \(Log.describe(error), privacy: .public); moving the store aside and retrying")
        }
        replaceFailedStoreBackup(with: configuration.url)
        do {
            return try ModelContainer(for: schema, configurations: [configuration])
        } catch {
            Log.startup.fault("ModelContainer failed after reset: \(Log.describe(error), privacy: .public); using an in-memory store")
        }
        let memory = ModelConfiguration(schema: schema, isStoredInMemoryOnly: true, cloudKitDatabase: .none)
        do {
            return try ModelContainer(for: schema, configurations: [memory])
        } catch {
            fatalError("Could not create even an in-memory ModelContainer: \(error)")
        }
    }

    /// Moves the store and SQLite sidecars without overwriting an earlier backup.
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

    private static func replaceFailedStoreBackup(with storeURL: URL) {
        try? FileManager.default.removeItem(at: failedStoreBackupDirectory)
        moveStoreFiles(at: storeURL, into: failedStoreBackupDirectory)
    }
}
