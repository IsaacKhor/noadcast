import SwiftUI
import SwiftData

struct PodcastDetailView: View {
    @Environment(\.modelContext) private var context
    @Bindable var podcast: Podcast
    @Query private var sortedEpisodes: [Episode]
    @State private var defaultSpeed: Double = 1.0
    @State private var toggleError: String?
    @State private var summaryText: String?

    init(podcast: Podcast) {
        self.podcast = podcast
        // SwiftData translates this predicate to a SQL `WHERE` + `ORDER BY`,
        // so a 500-episode archive doesn't fault every row's `publishedAt`
        // through main-thread accessors just to sort the list. The scalar
        // `podcastServerID` avoids a relationship join.
        let podcastServerID = podcast.serverID
        self._sortedEpisodes = Query(
            filter: #Predicate<Episode> { $0.podcastServerID == podcastServerID },
            sort: \Episode.publishedAt,
            order: .reverse
        )
    }

    var body: some View {
        List {
            // Podcast header — artwork + author + episode count. Sits in
            // the same list as the episodes so the whole thing scrolls
            // together (matches Pocket Casts / Overcast).
            Section {
                VStack(alignment: .leading, spacing: 12) {
                    HStack(spacing: 12) {
                        CachedArtworkImage(url: podcast.cachedArtworkDisplayURL, size: 80)
                            .frame(width: 80, height: 80)
                            .clipShape(RoundedRectangle(cornerRadius: 8))

                        VStack(alignment: .leading, spacing: 2) {
                            if let author = podcast.author, !author.isEmpty {
                                Text(author).font(.caption).foregroundStyle(.secondary)
                            }
                            Text("\(podcast.episodeCount) episodes")
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
                            if let lastFetched = podcast.lastFetched {
                                Text("Refreshed \(TimeFormatting.refreshTimestamp(lastFetched))")
                                    .font(.caption2)
                                    .foregroundStyle(.tertiary)
                            }
                            if let fetchError = podcast.lastFetchError, !fetchError.isEmpty {
                                Text("Last fetch failed: \(fetchError)")
                                    .font(.caption2)
                                    .foregroundStyle(.red)
                                    .lineLimit(2)
                            }
                        }
                        Spacer(minLength: 0)
                    }
                    if let summaryText, !summaryText.isEmpty {
                        Text(summaryText).font(.subheadline).foregroundStyle(.secondary)
                    }
                }
                .listRowInsets(.init(top: 16, leading: 16, bottom: 12, trailing: 16))
            }

            Section {
                Toggle("Auto-download new episodes", isOn: $podcast.autoDownloadEnabled)
                    .listRowInsets(.init(top: 6, leading: 16, bottom: 6, trailing: 16))
                Toggle("Detect & skip ads", isOn: adAnalysisBinding)
                    .listRowInsets(.init(top: 6, leading: 16, bottom: 6, trailing: 16))
                    .disabled(!APIConfiguration.isConfigured)
                Picker("Playback speed", selection: speedBinding) {
                    Text("Default (\(PlaybackSpeed.label(for: defaultSpeed)))").tag(Double?.none)
                    ForEach(PlaybackSpeed.options, id: \.self) { rate in
                        Text(PlaybackSpeed.label(for: rate)).tag(Optional(rate))
                    }
                }
                .pickerStyle(.menu)
                .listRowInsets(.init(top: 6, leading: 16, bottom: 6, trailing: 16))
            } header: {
                Text("Settings")
            } footer: {
                Text("Ad detection runs on your server; the global Detect & skip ads switch in Settings must also be on. Auto-download adds newly published episodes to your Queue and downloads them to this iPhone under your download settings.")
            }

            Section("Episodes") {
                ForEach(sortedEpisodes) { episode in
                    EpisodeRow(episode: episode, style: .episodeOnly)
                        .listRowInsets(.init(top: 8, leading: 16, bottom: 8, trailing: 16))
                        .swipeActions(edge: .leading) {
                            Button {
                                SubscriptionService.shared.addToQueue(episode, in: context)
                            } label: {
                                Label("Queue", systemImage: "text.badge.plus")
                            }
                            .tint(.blue)
                        }
                        .swipeActions(edge: .trailing) {
                            if !episode.isMarkedDownloaded {
                                Button {
                                    SubscriptionService.shared.download(episode, in: context)
                                } label: {
                                    Label("Download", systemImage: "arrow.down.circle")
                                }
                                .tint(.green)
                            }
                        }
                }
            }
        }
        .listStyle(.plain)
        .navigationTitle(podcast.title)
        .navigationBarTitleDisplayMode(.inline)
        .onAppear { refreshSettingsSnapshot() }
        .task(id: podcast.summary) {
            summaryText = nil
            guard let raw = podcast.summary, !raw.isEmpty else { return }
            // Like show notes, parse RSS HTML after navigation starts, then
            // retain the result until the server changes the summary.
            try? await Task.sleep(for: .milliseconds(80))
            guard !Task.isCancelled else { return }
            summaryText = RSSSummaryText.plainText(raw)
        }
        .refreshable {
            await SubscriptionService.shared.refresh(podcast: podcast, in: context)
            refreshSettingsSnapshot()
        }
        .alert("Couldn't change ad detection", isPresented: .constant(toggleError != nil), actions: {
            Button("OK") { toggleError = nil }
        }, message: {
            Text(toggleError ?? "")
        })
    }

    /// Server-owned: applied optimistically, rolled back if the PATCH fails.
    private var adAnalysisBinding: Binding<Bool> {
        Binding(
            get: { podcast.adAnalysisEnabled },
            set: { enabled in
                let target = podcast
                Task {
                    do {
                        try await SyncService.shared.setPodcastAdAnalysis(target, enabled: enabled)
                    } catch {
                        toggleError = error.localizedDescription
                    }
                }
            }
        )
    }

    private func refreshSettingsSnapshot() {
        defaultSpeed = AppSettings.current(in: context).defaultPlaybackSpeed
    }

    private var speedBinding: Binding<Double?> {
        Binding(
            get: { podcast.customPlaybackSpeed },
            set: { podcast.customPlaybackSpeed = $0 }
        )
    }
}
