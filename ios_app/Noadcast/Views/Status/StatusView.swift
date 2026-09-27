import SwiftUI
import SwiftData

/// Tab showing every episode with work in flight — on the server (fetch,
/// transcribe, analyse) or on the way to this device — every failure on
/// either axis with a retry, and every episode whose audio is on this
/// device.
struct StatusView: View {
    @Environment(\.modelContext) private var context
    @State private var showCancelAllConfirm = false
    private let subscription: SubscriptionService
    private let isUITestFixture: Bool

    init(subscription: SubscriptionService = .shared, isUITestFixture: Bool = false) {
        self.subscription = subscription
        self.isUITestFixture = isUITestFixture
    }

    // One SwiftData query covers the whole tab. `isBusy` is the denormalized
    // "server job active OR device download active" flag, so the predicate
    // stays a plain SQL `WHERE`.
    @Query(
        filter: #Predicate<Episode> {
            (!$0.isPlayed && (
                $0.isBusy
                    || $0.serverStateRaw == "failed"
                    || $0.downloadStateRaw == "failed"
            )) || $0.localFilename != nil
        },
        sort: \.publishedAt,
        order: .reverse
    )
    private var visibleEpisodes: [Episode]

    private let sync = SyncService.shared

    // Observe the fields that determine section membership. Query membership
    // can stay identical as a download moves from queued to failed or ready.
    // Fine-grained progress is still read only by EpisodeRow.
    private var inProgressEpisodes: [Episode] {
        visibleEpisodes.filter { !$0.isPlayed && $0.isBusy }
    }

    private var failedEpisodes: [Episode] {
        visibleEpisodes.filter { episode in
            !episode.isPlayed && !episode.isBusy && (episode.downloadState == .failed || episode.serverState == .failed)
        }
    }

    private var downloadedEpisodes: [Episode] {
        visibleEpisodes
            .filter(\.isMarkedDownloaded)
            .sorted { ($0.fileSizeBytes ?? 0) > ($1.fileSizeBytes ?? 0) }
    }

    private var totalBytes: Int64 {
        downloadedEpisodes.reduce(0) { $0 + ($1.fileSizeBytes ?? 0) }
    }

    var body: some View {
        NavigationStack {
            Group {
                if visibleEpisodes.isEmpty {
                    ContentUnavailableView {
                        Label("Nothing in progress", systemImage: "checkmark.circle")
                    } description: {
                        Text("Active jobs, failures, and audio on this iPhone appear here.")
                    }
                } else {
                    List {
                        if !inProgressEpisodes.isEmpty {
                            Section("In progress") {
                                ForEach(inProgressEpisodes) { episode in
                                    EpisodeRow(episode: episode, style: .withPodcast, showProgress: true) {
                                        if episode.downloadState.isActive {
                                            Button {
                                                subscription.cancelDownload(episode)
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
                                    .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                                        markPlayedButton(for: episode)
                                    }
                                }
                            }
                        }

                        if !failedEpisodes.isEmpty {
                            Section("Failed") {
                                ForEach(failedEpisodes) { episode in
                                    EpisodeRow(episode: episode, style: .withPodcast, showProgress: true) {
                                        Button {
                                            subscription.retry(episode, in: context)
                                        } label: {
                                            Image(systemName: "arrow.clockwise.circle")
                                                .font(.title2)
                                        }
                                        .buttonStyle(.plain)
                                        .accessibilityLabel("Retry")
                                    }
                                    .listRowInsets(.init(top: 8, leading: 16, bottom: 8, trailing: 16))
                                    .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                                        markPlayedButton(for: episode)
                                    }
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
                                    .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                                        if !episode.isPlayed {
                                            markPlayedButton(for: episode)
                                        }
                                        Button("Remove download", systemImage: "trash", role: .destructive) {
                                            subscription.deleteEpisodeContent(episode, in: context)
                                        }
                                        .accessibilityLabel("Remove download for \(episode.title)")
                                    }
                                }
                            }
                        }
                    }
                    .listStyle(.plain)
                }
            }
            .navigationTitle("Status")
            .navigationBarTitleDisplayMode(.inline)
            .refreshable {
                if !isUITestFixture { await sync.syncNow(.pullToRefresh) }
            }
            .onAppear {
                // 2 s job-progress polling while this tab is on screen.
                if !isUITestFixture { sync.setStatusTabVisible(true) }
            }
            .onDisappear {
                if !isUITestFixture { sync.setStatusTabVisible(false) }
            }
            .toolbar {
                if inProgressEpisodes.contains(where: { $0.downloadState.isActive }) {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button("Cancel downloads", role: .destructive) {
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
                Button("Cancel downloads", role: .destructive) { cancelAllDownloads() }
                Button("Keep Going", role: .cancel) { }
            }
        }
    }

    private var activeDeviceDownloads: [Episode] {
        inProgressEpisodes.filter { $0.downloadState.isActive }
    }

    private func cancelAllDownloads() {
        for episode in activeDeviceDownloads {
            subscription.cancelDownload(episode)
        }
    }

    private func markPlayedButton(for episode: Episode) -> some View {
        Button("Mark played", systemImage: "checkmark.circle") {
            subscription.deleteEpisodeContent(episode, in: context, markAsPlayed: true)
        }
        .tint(.green)
        .accessibilityLabel("Mark \(episode.title) played and stop its downloads and analysis")
    }
}
