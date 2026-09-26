import SwiftUI
import SwiftData
import os

struct ContentView: View {
    @Environment(\.modelContext) private var context
    @Environment(\.scenePhase) private var scenePhase
    @State private var showFullPlayer = false
    @State private var showServerSetup = false
    @AppStorage("ServerSetupPromptDismissed") private var serverSetupPromptDismissed = false

    private let player = PlayerService.shared
    private let sync = SyncService.shared

    var body: some View {
        TabView {
            Tab("Queue", systemImage: "list.bullet") {
                QueueView()
            }
            Tab("Podcasts", systemImage: "rectangle.stack.fill") {
                PodcastsView()
            }
            Tab("Downloads", systemImage: "arrow.down.circle") {
                DownloadsView()
            }
            Tab("Settings", systemImage: "gear") {
                SettingsView()
            }
        }
        .tabViewBottomAccessory {
            MiniPlayerBar(onTap: {
                if player.currentEpisodeID != nil {
                    showFullPlayer = true
                }
            })
        }
        .sheet(isPresented: $showFullPlayer) {
            NowPlayingView()
        }
        .sheet(isPresented: $showServerSetup, onDismiss: {
            serverSetupPromptDismissed = true
        }) {
            NavigationStack {
                ServerSetupView()
                    .toolbar {
                        ToolbarItem(placement: .confirmationAction) {
                            Button("Done") { showServerSetup = false }
                        }
                    }
            }
        }
        .overlay(alignment: .top) {
            if sync.authFailed {
                authBanner
            }
        }
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .active:
                sync.appDidBecomeActive()
            case .background:
                sync.appDidEnterBackground()
            case .inactive:
                break
            @unknown default:
                break
            }
        }
        .task {
            let taskState = Log.signposter.beginInterval("ContentView.task")
            defer { Log.signposter.endInterval("ContentView.task", taskState) }
            Log.signposter.withIntervalSignpost("AppSettings.current") {
                _ = AppSettings.current(in: context)
            }
            PlayerService.shared.restoreLastPlayedEpisode(context: context)

            if !APIConfiguration.isConfigured, !serverSetupPromptDismissed {
                showServerSetup = true
            }

            // Backfill artwork for podcasts whose cache is missing (or whose
            // previous attempt failed). `cache(for:)` is a no-op when the file
            // is already on disk, so this is cheap against the whole library.
            Task {
                await ArtworkService.shared.backfillAllPodcasts(context: context)
            }
        }
    }

    /// Shown while the server rejects the token (401); polling and sync
    /// stop until the token is fixed, so there is no retry storm.
    private var authBanner: some View {
        Button {
            showServerSetup = true
        } label: {
            Label("Server rejected the access token. Tap to update it.", systemImage: "exclamationmark.triangle.fill")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(.white)
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(Color.red.opacity(0.9), in: Capsule())
        }
        .buttonStyle(.plain)
        .padding(.top, 4)
        .transition(.move(edge: .top).combined(with: .opacity))
    }
}

#Preview {
    ContentView()
        .modelContainer(for: [
            Podcast.self, Episode.self, AdMarker.self,
            QueueItem.self, AppSettings.self,
            UsageHistoryDay.self, SyncCursor.self
        ], inMemory: true)
}
