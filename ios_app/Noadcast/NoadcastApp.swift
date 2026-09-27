import SwiftUI
import SwiftData
import os

@main
struct NoadcastApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    let sharedModelContainer: ModelContainer

    #if DEBUG
    private static var isUnitTesting: Bool {
        if ProcessInfo.processInfo.arguments.contains("--ui-test-queue")
            || ProcessInfo.processInfo.arguments.contains("--ui-test-status")
            || ProcessInfo.processInfo.arguments.contains("--ui-test-refresh") {
            return false
        }
        return ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
            || ProcessInfo.processInfo.environment["XCTestBundlePath"] != nil
            || NSClassFromString("XCTestCase") != nil
    }
    #endif

    init() {
        #if DEBUG
        if Self.isUnitTesting {
            // A hosted unit test must not launch sync against the server
            // configured in the simulator's normal app container.
            let schema = LocalStoreGeneration.schema
            sharedModelContainer = try! ModelContainer(
                for: schema,
                configurations: [ModelConfiguration(schema: schema, isStoredInMemoryOnly: true)]
            )
            PlayerService.shared.setModelContainer(sharedModelContainer)
            return
        }
        if ProcessInfo.processInfo.arguments.contains("--ui-test-queue") {
            sharedModelContainer = QueueUITestFixture.makeContainer()
            PlayerService.shared.setModelContainer(sharedModelContainer)
            return
        }
        if ProcessInfo.processInfo.arguments.contains("--ui-test-status") {
            sharedModelContainer = StatusUITestFixture.makeContainer()
            PlayerService.shared.setModelContainer(sharedModelContainer)
            return
        }
        if ProcessInfo.processInfo.arguments.contains("--ui-test-refresh") {
            sharedModelContainer = PodcastsUITestFixture.makeContainer()
            return
        }
        #endif
        // Order matters: the generation-1 store must be exported and moved
        // aside before the generation-2 container opens on the same path.
        Log.signposter.withIntervalSignpost("LocalStoreGeneration.prepare") {
            LocalStoreGeneration.prepare()
        }
        let container = Log.signposter.withIntervalSignpost("ModelContainer.init") {
            LocalStoreGeneration.makeContainer()
        }
        sharedModelContainer = container

        Log.signposter.withIntervalSignpost("NoadcastApp.wire") {
            // Singletons created before any query touches the store, so a
            // concurrent context never races the main context to insert them.
            _ = AppSettings.current(in: container.mainContext)
            _ = SyncCursor.current(in: container.mainContext)
            PlayerService.shared.setModelContainer(container)
            // Creates the background URLSession early: a background relaunch
            // delivers download events as soon as it exists.
            DownloadManager.shared.configure(container: container)
            DeviceStateRestoreService.shared.configure(container: container)
            SyncService.shared.configure(container: container)
            _ = NetworkMonitor.shared
        }

        Task {
            await DownloadManager.shared.reconcile()
            await SyncService.shared.launch()
        }
    }

    var body: some Scene {
        WindowGroup {
            #if DEBUG
            if Self.isUnitTesting {
                Color.clear
            } else if ProcessInfo.processInfo.arguments.contains("--ui-test-queue") {
                QueueView(
                    subscription: QueueUITestFixture.subscription,
                    isUITestFixture: true,
                    fixtureAudioFilename: QueueUITestFixture.audioFilename,
                    fixtureReleasePending: { QueueUITestFixture.releasePending }
                )
            } else if ProcessInfo.processInfo.arguments.contains("--ui-test-status") {
                StatusView(subscription: StatusUITestFixture.subscription, isUITestFixture: true)
            } else if ProcessInfo.processInfo.arguments.contains("--ui-test-refresh") {
                PodcastsView(refreshAction: PodcastsUITestFixture.refresh, serverIsConfigured: { true })
            } else {
                ContentView()
            }
            #else
            ContentView()
            #endif
        }
        .modelContainer(sharedModelContainer)
        .backgroundTask(.appRefresh(SyncService.backgroundRefreshTaskIdentifier)) {
            await SyncService.shared.performBackgroundRefresh()
        }
    }
}
