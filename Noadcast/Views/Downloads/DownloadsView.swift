import SwiftUI
import SwiftData

/// Tab showing every episode currently being processed (downloading,
/// transcribing, or having ads detected) and every episode whose audio is
/// still on disk. Failed jobs surface here too with a retry affordance.
struct DownloadsView: View {
    @Environment(\.modelContext) private var context
    @State private var showCancelAllConfirm = false

    // Three separate predicate-filtered queries instead of one fetch-all +
    // in-memory filter. Each predicate runs in SQLite so the rows that
    // never appear on screen (the typical case: thousands of un-downloaded
    // episodes across many podcasts) aren't faulted into memory.
    @Query(
        filter: #Predicate<Episode> { $0.isInProgress },
        sort: \.publishedAt,
        order: .reverse
    )
    private var inProgress: [Episode]

    @Query(
        filter: #Predicate<Episode> { $0.processingStateRaw == "failed" },
        sort: \.publishedAt,
        order: .reverse
    )
    private var failed: [Episode]

    @Query(
        filter: #Predicate<Episode> { $0.localFilename != nil },
        sort: \.fileSizeBytes,
        order: .reverse
    )
    private var downloaded: [Episode]

    private var pipeline = ProcessingPipeline.shared

    private var totalBytes: Int64 {
        downloaded.reduce(0) { $0 + ($1.fileSizeBytes ?? 0) }
    }

    var body: some View {
        NavigationStack {
            Group {
                if inProgress.isEmpty && downloaded.isEmpty && failed.isEmpty {
                    ContentUnavailableView {
                        Label("Nothing downloaded", systemImage: "arrow.down.circle")
                    } description: {
                        Text("Queued episodes are downloaded and analysed automatically.")
                    }
                } else {
                    List {
                        if !inProgress.isEmpty {
                            Section("In progress") {
                                ForEach(inProgress) { episode in
                                    InProgressRow(episode: episode)
                                }
                            }
                        }

                        if !failed.isEmpty {
                            Section("Failed") {
                                ForEach(failed) { episode in
                                    FailedRow(episode: episode)
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
                        }

                        if !downloaded.isEmpty {
                            Section("Downloaded") {
                                ForEach(downloaded) { episode in
                                    DownloadedRow(episode: episode)
                                }
                                .onDelete(perform: deleteDownloaded)
                            }
                        }
                    }
                }
            }
            .navigationTitle("Downloads")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if !inProgress.isEmpty {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button("Cancel All", role: .destructive) {
                            showCancelAllConfirm = true
                        }
                    }
                }
            }
            .confirmationDialog(
                "Cancel \(inProgress.count) in-progress download\(inProgress.count == 1 ? "" : "s")?",
                isPresented: $showCancelAllConfirm,
                titleVisibility: .visible
            ) {
                Button("Cancel All", role: .destructive) { cancelAllInProgress() }
                Button("Keep Going", role: .cancel) { }
            }
        }
    }

    private func cancelAllInProgress() {
        for episode in inProgress {
            pipeline.cancel(episodeID: episode.persistentModelID)
        }
    }

    private func deleteDownloaded(at offsets: IndexSet) {
        let toDelete = offsets.map { downloaded[$0] }
        for episode in toDelete {
            // Unified delete: removes the file *and* any QueueItem pointing
            // at this episode, and unloads the player if it's currently
            // playing this one.
            SubscriptionService.shared.deleteEpisodeContent(episode, in: context, save: false)
        }
        try? context.save()
    }
}

// MARK: - Rows

private struct InProgressRow: View {
    @Bindable var episode: Episode

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(episode.title).font(.subheadline.bold()).lineLimit(2)
                    Text(episode.podcast?.title ?? "")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer()
                Button {
                    ProcessingPipeline.shared.cancel(episodeID: episode.persistentModelID)
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(.secondary)
                        .font(.title3)
                }
                .buttonStyle(.plain)
            }

            ProgressView(value: episode.processingProgress)

            HStack {
                Label(stageLabel, systemImage: stageSymbol)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Spacer()
                if let detail = TimeFormatting.progressDetail(for: episode) {
                    Text(detail)
                        .font(.caption2.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.vertical, 2)
    }

    private var stageLabel: String {
        switch episode.processingState {
        case .downloading: "Downloading"
        case .transcribing: "Transcribing"
        case .detectingAds: "Detecting ads"
        default: ""
        }
    }

    private var stageSymbol: String {
        switch episode.processingState {
        case .downloading: "arrow.down.circle"
        case .transcribing: "waveform"
        case .detectingAds: "sparkles"
        default: "circle"
        }
    }
}

private struct FailedRow: View {
    @Bindable var episode: Episode

    var body: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 2) {
                Text(episode.title).font(.subheadline.bold()).lineLimit(2)
                Text(episode.podcast?.title ?? "")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                if let err = episode.processingError {
                    Text(err)
                        .font(.caption2)
                        .foregroundStyle(.red)
                        .lineLimit(3)
                }
            }
            Spacer()
            Button {
                ProcessingPipeline.shared.process(episode: episode)
            } label: {
                Image(systemName: "arrow.clockwise.circle")
                    .font(.title3)
            }
            .buttonStyle(.plain)
        }
    }
}

private struct DownloadedRow: View {
    let episode: Episode

    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(episode.title).font(.subheadline).lineLimit(2)
                Text(episode.podcast?.title ?? "")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Text(TimeFormatting.fileSize(episode.fileSizeBytes ?? 0))
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
        }
    }
}
