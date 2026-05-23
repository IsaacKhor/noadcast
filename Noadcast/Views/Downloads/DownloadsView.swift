import SwiftUI
import SwiftData

/// Tab showing every episode currently being processed (downloading,
/// transcribing, or having ads detected) and every episode whose audio is
/// still on disk. Failed jobs surface here too with a retry affordance.
struct DownloadsView: View {
    @Environment(\.modelContext) private var context
    @State private var showCancelAllConfirm = false
    @State private var inProgressEpisodes: [Episode] = []
    @State private var failedEpisodes: [Episode] = []
    @State private var downloadedEpisodes: [Episode] = []
    @State private var totalBytes: Int64 = 0

    // One SwiftData query covers the whole tab. The previous three live
    // `Episode` queries all invalidated on processing updates, which made
    // download progress noisier than it needed to be while scrolling.
    @Query(
        filter: #Predicate<Episode> {
            $0.isInProgress || $0.processingStateRaw == "failed" || $0.localFilename != nil
        },
        sort: \.publishedAt,
        order: .reverse
    )
    private var visibleEpisodes: [Episode]

    private var pipeline = ProcessingPipeline.shared

    var body: some View {
        NavigationStack {
            Group {
                if visibleEpisodes.isEmpty {
                    ContentUnavailableView {
                        Label("Nothing downloaded", systemImage: "arrow.down.circle")
                    } description: {
                        Text("Queued episodes are downloaded and analysed automatically.")
                    }
                } else {
                    List {
                        if !inProgressEpisodes.isEmpty {
                            Section("In progress") {
                                ForEach(inProgressEpisodes) { episode in
                                    EpisodeRow(episode: episode, style: .withPodcast, showProgress: true) {
                                        Button {
                                            pipeline.cancel(episodeID: episode.persistentModelID)
                                        } label: {
                                            Image(systemName: "xmark.circle.fill")
                                                .foregroundStyle(.secondary)
                                                .font(.title3)
                                        }
                                        .buttonStyle(.plain)
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
                                            pipeline.process(episode: episode)
                                        } label: {
                                            Image(systemName: "arrow.clockwise.circle")
                                                .font(.title2)
                                        }
                                        .buttonStyle(.plain)
                                    }
                                    .listRowInsets(.init(top: 8, leading: 16, bottom: 8, trailing: 16))
                                }
                            }
                        }

                        Section {
                            HStack {
                                Text("Total")
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
            .onAppear { refreshSections() }
            .onChange(of: visibleEpisodes) { _, _ in refreshSections() }
            .toolbar {
                if !inProgressEpisodes.isEmpty {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button("Cancel All", role: .destructive) {
                            showCancelAllConfirm = true
                        }
                    }
                }
            }
            .confirmationDialog(
                "Cancel \(inProgressEpisodes.count) in-progress download\(inProgressEpisodes.count == 1 ? "" : "s")?",
                isPresented: $showCancelAllConfirm,
                titleVisibility: .visible
            ) {
                Button("Cancel All", role: .destructive) { cancelAllInProgress() }
                Button("Keep Going", role: .cancel) { }
            }
        }
    }

    private func refreshSections() {
        inProgressEpisodes = visibleEpisodes.filter(\.isInProgress)
        failedEpisodes = visibleEpisodes.filter { $0.processingState == .failed }
        downloadedEpisodes = visibleEpisodes
            .filter(\.isMarkedDownloaded)
            .sorted { ($0.fileSizeBytes ?? 0) > ($1.fileSizeBytes ?? 0) }
        totalBytes = downloadedEpisodes.reduce(0) { $0 + ($1.fileSizeBytes ?? 0) }
    }

    private func cancelAllInProgress() {
        for episode in inProgressEpisodes {
            pipeline.cancel(episodeID: episode.persistentModelID)
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
