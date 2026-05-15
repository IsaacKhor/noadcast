import SwiftUI
import SwiftData

struct PodcastDetailView: View {
    @Environment(\.modelContext) private var context
    @Bindable var podcast: Podcast
    @Query private var settingsList: [AppSettings]
    @Query private var sortedEpisodes: [Episode]

    init(podcast: Podcast) {
        self.podcast = podcast
        // SwiftData translates this predicate to a SQL `WHERE` + `ORDER BY`,
        // so a 500-episode archive doesn't fault every row's `publishedAt`
        // through main-thread accessors just to sort the list.
        let feedURL = podcast.feedURL
        self._sortedEpisodes = Query(
            filter: #Predicate<Episode> { $0.podcast?.feedURL == feedURL },
            sort: \Episode.publishedAt,
            order: .reverse
        )
    }

    private var defaultSpeed: Double {
        settingsList.first?.defaultPlaybackSpeed ?? 1.0
    }

    var body: some View {
        List {
            Section {
                HStack(spacing: 12) {
                    AsyncImage(url: podcast.artworkURL) { phase in
                        switch phase {
                        case .success(let image): image.resizable()
                        default: Color.gray.opacity(0.2)
                        }
                    }
                    .frame(width: 80, height: 80)
                    .clipShape(RoundedRectangle(cornerRadius: 8))

                    VStack(alignment: .leading) {
                        Text(podcast.author ?? "").font(.caption).foregroundStyle(.secondary)
                        Text("\(sortedEpisodes.count) episodes")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                }
                if let summary = podcast.summary, !summary.isEmpty {
                    Text(summary).font(.subheadline)
                }
            }

            if let lastFetched = podcast.lastFetched {
                Section {
                    HStack {
                        Label("Last refresh", systemImage: "arrow.clockwise")
                            .foregroundStyle(.secondary)
                        Spacer()
                        Text(TimeFormatting.refreshTimestamp(lastFetched))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }

            Section {
                Toggle("Auto-download new episodes", isOn: $podcast.autoDownloadEnabled)
                Toggle("Detect & skip ads", isOn: $podcast.aiProcessingEnabled)
                HStack {
                    Text("Playback speed")
                    Spacer()
                    Picker("Speed", selection: speedBinding) {
                        Text("Default (\(PlaybackSpeed.label(for: defaultSpeed)))").tag(Double?.none)
                        ForEach(PlaybackSpeed.options, id: \.self) { rate in
                            Text(PlaybackSpeed.label(for: rate)).tag(Optional(rate))
                        }
                    }
                    .pickerStyle(.menu)
                }
            } header: {
                Text("Settings")
            } footer: {
                Text("When ad detection is off, this podcast's new episodes are downloaded but not transcribed, and no ads will be marked or skipped during playback.")
            }

            Section("Episodes") {
                ForEach(sortedEpisodes) { episode in
                    EpisodeRow(episode: episode)
                }
            }
        }
        .navigationTitle(podcast.title)
        .navigationBarTitleDisplayMode(.inline)
        .refreshable {
            try? await SubscriptionService.shared.refresh(podcast: podcast, in: context)
        }
    }

    private var speedBinding: Binding<Double?> {
        Binding(
            get: { podcast.customPlaybackSpeed },
            set: { podcast.customPlaybackSpeed = $0 }
        )
    }
}

struct EpisodeRow: View {
    @Environment(\.modelContext) private var context
    @Bindable var episode: Episode
    @State private var showNotes = false
    let player = PlayerService.shared
    let pipeline = ProcessingPipeline.shared

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                // Tap the title/date area to read the show notes. The action
                // button on the trailing edge handles its own tap so play /
                // download isn't accidentally consumed.
                Button {
                    showNotes = true
                } label: {
                    VStack(alignment: .leading) {
                        Text(episode.title).font(.subheadline.bold()).lineLimit(2)
                        if let date = episode.publishedAt {
                            Text(date.formatted(date: .abbreviated, time: .omitted))
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                actionButton
            }
            if episode.processingState != .ready, episode.processingState != .new {
                processingFooter
            } else if !episode.adMarkers.filter({ !$0.isDeleted }).isEmpty {
                Label(
                    "\(episode.adMarkers.filter { !$0.isDeleted }.count) ads detected",
                    systemImage: "speaker.slash"
                )
                .font(.caption2)
                .foregroundStyle(.orange)
            }
        }
        .swipeActions(edge: .leading) {
            Button {
                addToQueue()
            } label: {
                Label("Queue", systemImage: "text.badge.plus")
            }
            .tint(.blue)
        }
        .sheet(isPresented: $showNotes) {
            ShowNotesView(episode: episode)
        }
    }

    @ViewBuilder
    private var actionButton: some View {
        if episode.processingState == .ready, episode.hasLocalFile {
            Button {
                play()
            } label: {
                Image(systemName: "play.circle.fill").font(.title)
            }
            .buttonStyle(.plain)
        } else if pipeline.isProcessing(episodeID: episode.persistentModelID) {
            ProgressView()
        } else {
            Button {
                pipeline.process(episode: episode)
            } label: {
                Image(systemName: "arrow.down.circle").font(.title)
            }
            .buttonStyle(.plain)
        }
    }

    private var processingFooter: some View {
        VStack(alignment: .leading, spacing: 2) {
            ProgressView(value: episode.processingProgress)
            HStack(spacing: 6) {
                Text(label(for: episode.processingState))
                if let detail = TimeFormatting.progressDetail(for: episode) {
                    Text("·")
                    Text(detail).monospacedDigit()
                }
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
            if let err = episode.processingError {
                Text(err).font(.caption2).foregroundStyle(.red)
            }
        }
    }

    private func label(for state: EpisodeProcessingState) -> String {
        switch state {
        case .downloading: "Downloading…"
        case .downloaded: "Downloaded"
        case .transcribing: "Transcribing…"
        case .detectingAds: "Detecting ads…"
        case .ready: "Ready"
        case .failed: "Failed"
        case .new: ""
        }
    }

    private func play() {
        let settings = AppSettings.current(in: context)
        player.load(episode: episode, settings: settings)
        player.play()
    }

    private func addToQueue() {
        let descriptor = FetchDescriptor<QueueItem>(
            sortBy: [SortDescriptor(\QueueItem.position)]
        )
        let existing = (try? context.fetch(descriptor)) ?? []
        guard !existing.contains(where: { $0.episode == episode }) else { return }
        let position = (existing.last?.position ?? -1) + 1
        let item = QueueItem(position: position, episode: episode)
        context.insert(item)
        try? context.save()
        SubscriptionService.shared.processQueuedEpisodes(context: context)
    }
}
