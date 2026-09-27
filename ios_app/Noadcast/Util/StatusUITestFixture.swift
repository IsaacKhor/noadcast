#if DEBUG
import Foundation
import SwiftData

/// Isolated Status screen for UI tests. It never opens the user's store,
/// configures transfers, or sends a request to a configured server.
enum StatusUITestFixture {
    private static let suiteName = "Noadcast.StatusUITestFixture"
    private static let defaults = UserDefaults(suiteName: suiteName)!

    static let subscription = SubscriptionService(
        releasePlayedAudio: { PendingReleaseStore.add($0, instanceId: "status-ui-test", defaults: defaults) },
        cancelPendingRelease: { PendingReleaseStore.remove($0, defaults: defaults) },
        cancelLocalTransfer: { _ in },
        cancelServerAudioRequest: { _ in }
    )

    static func makeContainer() -> ModelContainer {
        defaults.removePersistentDomain(forName: suiteName)
        let schema = LocalStoreGeneration.schema
        let configuration = ModelConfiguration(schema: schema, isStoredInMemoryOnly: true)
        do {
            let container = try ModelContainer(for: schema, configurations: [configuration])
            let context = container.mainContext
            AppSettings.current(in: context).autoDownloadPolicy = .manualOnly
            let podcast = Podcast(
                serverID: 9_000_000,
                feedURL: URL(string: "https://status-ui-test.invalid/feed.xml")!,
                title: "Status test podcast"
            )
            context.insert(podcast)

            let pending = Episode(serverID: 9_000_001, podcastServerID: podcast.serverID, guid: "status-pending", title: "Status pending", podcast: podcast)
            pending.applyServerState(ServerEpisodeState.transcribing.rawValue)
            context.insert(pending)

            let failed = Episode(serverID: 9_000_002, podcastServerID: podcast.serverID, guid: "status-failed", title: "Status failed", podcast: podcast)
            failed.applyServerState(ServerEpisodeState.failed.rawValue)
            context.insert(failed)

            let downloaded = Episode(serverID: 9_000_003, podcastServerID: podcast.serverID, guid: "status-downloaded", title: "Status downloaded", podcast: podcast)
            downloaded.localFilename = "status-ui-downloaded.mp3"
            downloaded.fileSizeBytes = 10_000_000
            downloaded.setDownloadState(.downloaded)
            context.insert(downloaded)

            let retained = Episode(serverID: 9_000_004, podcastServerID: podcast.serverID, guid: "status-retained", title: "Status retained", podcast: podcast)
            retained.isPlayed = true
            retained.localFilename = "status-ui-retained.mp3"
            retained.fileSizeBytes = 5_000_000
            context.insert(retained)

            try context.save()
            return container
        } catch {
            fatalError("Could not create Status UI test fixture: \(error)")
        }
    }
}
#endif
