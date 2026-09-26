import SwiftUI
import SwiftData

/// Tab showing every episode with work in flight — on the server (fetch,
/// transcribe, analyse) or on the way to this device — every failure on
/// either axis with a retry, and every episode whose audio is on this
/// device.
struct DownloadsView: View {
    @Environment(\.modelContext) private var context
    @State private var showCancelAllConfirm = false
    @State private var inProgressEpisodes: [Episode] = []
    @State private var failedEpisodes: [Episode] = []
    @State private var downloadedEpisodes: [Episode] = []
    @State private var totalBytes: Int64 = 0

    // One SwiftData query covers the whole tab. `isBusy` is the denormalized
    // "server job active OR device download active" flag, so the predicate
    // stays a plain SQL `WHERE`.
    @Query(
        filter: #Predicate<Episode> {
            $0.isBusy
                || $0.serverStateRaw == "failed"
                || $0.downloadStateRaw == "failed"
                || $0.localFilename != nil
        },
        sort: \.publishedAt,
        order: .reverse
    )
    private var visibleEpisodes: [Episode]

    private let sync = SyncService.shared

    var body: some View {
        NavigationStack {
            Group {
                if visibleEpisodes.isEmpty {
                    ContentUnavailableView {
                        Label("Nothing downloaded", systemImage: "arrow.down.circle")
                    } description: {
                        Text("Queued episodes are downloaded automatically, following your download settings. Your server's work on new episodes also shows up here.")
                    }
                } else {
                    List {
                        if !inProgressEpisodes.isEmpty {
                            Section("In progress") {
                                ForEach(inProgressEpisodes) { episode in
                                    EpisodeRow(episode: episode, style: .withPodcast, showProgress: true) {
                                        if episode.downloadState.isActive {
                                            Button {
                                                SubscriptionService.shared.cancelDownload(episode)
                                            } label: {
                                                Image(systemName: "xmark.circle.fill")
                                                    .foregroundStyle(.secondary)
                                                    .font(.title3)
                                            }
                                            .buttonStyle(.plain)
                                            .accessibilityLabel("Cancel download")
                                        } else {
                                            StandardEpisodeAction(episode: episode)
                                        }
                                    }
                                    .listRowInsets(.init(top: 8, leading: 16, bottom: 8, trailing: 16))
                                }
                            }
                        }

                        if !failedEpisodes.isEmpty {
                            Section("Failed") {
                                ForEach(failedEpisodes) { episode in
                                    EpisodeRow(episode: episode, style: .withPodcast, showProgress: true) {
                                        Button {
                                            SubscriptionService.shared.retry(episode, in: context)
                                        } label: {
                                            Image(systemName: "arrow.clockwise.circle")
                                                .font(.title2)
                                        }
                                        .buttonStyle(.plain)
                                        .accessibilityLabel("Retry")
                                    }
                                    .listRowInsets(.init(top: 8, leading: 16, bottom: 8, trailing: 16))
                                }
                            }
                        }

                        Section {
                            HStack {
                                Text("On this iPhone")
                                Spacer()
                                Text(TimeFormatting.fileSize(totalBytes))
                                    .foregroundStyle(.secondary)
                                    .monospacedDigit()
                            }
                            .listRowInsets(.init(top: 10, leading: 16, bottom: 10, trailing: 16))
                        }

                        if !downloadedEpisodes.isEmpty {
                            Section("Downloaded") {
                                ForEach(downloadedEpisodes) { episode in
                                    EpisodeRow(episode: episode, style: .withPodcast, showProgress: true) {
                                        Text(TimeFormatting.fileSize(episode.fileSizeBytes ?? 0))
                                            .font(.caption.monospacedDigit())
                                            .foregroundStyle(.secondary)
                                    }
                                    .listRowInsets(.init(top: 8, leading: 16, bottom: 8, trailing: 16))
                                }
                                .onDelete(perform: deleteDownloaded)
                            }
                        }
                    }
                    .listStyle(.plain)
                }
            }
            .navigationTitle("Downloads")
            .navigationBarTitleDisplayMode(.inline)
            .refreshable {
                await sync.syncNow(.pullToRefresh)
            }
            .onAppear {
                refreshSections()
                // 2 s job-progress polling while this tab is on screen.
                sync.setDownloadsTabVisible(true)
            }
            .onDisappear {
                sync.setDownloadsTabVisible(false)
            }
            .onChange(of: visibleEpisodes) { _, _ in refreshSections() }
            .toolbar {
                if inProgressEpisodes.contains(where: { $0.downloadState.isActive }) {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button("Cancel All", role: .destructive) {
                            showCancelAllConfirm = true
                        }
                    }
                }
            }
            .confirmationDialog(
                "Cancel \(activeDeviceDownloads.count) download\(activeDeviceDownloads.count == 1 ? "" : "s") to this iPhone?",
                isPresented: $showCancelAllConfirm,
                titleVisibility: .visible
            ) {
                Button("Cancel All", role: .destructive) { cancelAllDownloads() }
                Button("Keep Going", role: .cancel) { }
            }
        }
    }

    private var activeDeviceDownloads: [Episode] {
        inProgressEpisodes.filter { $0.downloadState.isActive }
    }

    private func refreshSections() {
        inProgressEpisodes = visibleEpisodes.filter(\.isBusy)
        failedEpisodes = visibleEpisodes.filter { episode in
            !episode.isBusy && (episode.downloadState == .failed || episode.serverState == .failed)
        }
        downloadedEpisodes = visibleEpisodes
            .filter(\.isMarkedDownloaded)
            .sorted { ($0.fileSizeBytes ?? 0) > ($1.fileSizeBytes ?? 0) }
        totalBytes = downloadedEpisodes.reduce(0) { $0 + ($1.fileSizeBytes ?? 0) }
    }

    private func cancelAllDownloads() {
        for episode in activeDeviceDownloads {
            SubscriptionService.shared.cancelDownload(episode)
        }
    }

    private func deleteDownloaded(at offsets: IndexSet) {
        let toDelete = offsets.map { downloadedEpisodes[$0] }
        for episode in toDelete {
            // Unified delete: removes the file *and* any QueueItem pointing
            // at this episode, and unloads the player if it's currently
            // playing this one.
            SubscriptionService.shared.deleteEpisodeContent(episode, in: context, save: false)
        }
        try? context.save()
    }
}
