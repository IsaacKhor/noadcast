import SwiftUI
import SwiftData

struct QueueView: View {
    @Environment(\.modelContext) private var context
    @Query(sort: \QueueItem.position) private var items: [QueueItem]

    private var player = PlayerService.shared
    private var pipeline = ProcessingPipeline.shared

    /// Queue minus whichever item (if any) is currently loaded in the
    /// player — rendered in its own header section instead. Cached in
    /// `@State` and refreshed via `refreshPending()` only when the inputs
    /// change, so a body re-eval doesn't re-filter the array.
    @State private var pendingItems: [QueueItem] = []
    @State private var pendingDuration: Double = 0

    /// The episode the player is currently loaded on, if any. Looked up by
    /// `PersistentIdentifier` so we don't fault every Episode just to render
    /// the Now Playing header.
    private var currentEpisode: Episode? {
        guard let id = player.currentEpisodeID else { return nil }
        return context.model(for: id) as? Episode
    }

    private func refreshPending() {
        let playingID = player.currentEpisodeID
        let pending: [QueueItem]
        if let id = playingID {
            pending = items.filter { $0.episode?.persistentModelID != id }
        } else {
            pending = items
        }

        pendingItems = pending
        pendingDuration = pending.reduce(0) { total, item in
            guard let episode = item.episode,
                  let duration = episode.duration,
                  duration > 0 else { return total }
            let remaining = episode.playbackPosition > 0 ? duration - episode.playbackPosition : duration
            return total + max(0, remaining)
        }
    }

    var body: some View {
        NavigationStack {
            // Always render the list so the "Latest episodes" link is
            // reachable even with an empty queue. Empty state appears as
            // an inline placeholder section.
            queueList
            .navigationTitle("Queue")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    sortMenu
                }
            }
            .task {
                SubscriptionService.shared.processQueuedEpisodes(context: context)
            }
            .onAppear { refreshPending() }
            .onChange(of: items) { _, _ in refreshPending() }
            .onChange(of: player.currentEpisodeID) { _, _ in refreshPending() }
        }
    }

    private var queueList: some View {
        // Plain List (no edit mode) so swipeActions remain functional.
        // Drag-to-reorder still works via long-press on the row; the
        // drag-handle glyph at the row's trailing edge is a visual cue.
        // `.plain` style + per-row insets give the edge-to-edge layout
        // other podcast apps use (Pocket Casts / Overcast).
        List {
            Section {
                NavigationLink {
                    LatestEpisodesView()
                } label: {
                    Label("Latest episodes", systemImage: "clock.arrow.circlepath")
                }
                .listRowInsets(.init(top: 10, leading: 16, bottom: 10, trailing: 16))
            }

            if let episode = currentEpisode {
                Section {
                    EpisodeRow(episode: episode, style: .withPodcast) {
                        Image(systemName: "waveform")
                            .symbolEffect(.variableColor.iterative, options: .repeating)
                            .foregroundStyle(.tint)
                            .font(.title3)
                    }
                    .listRowInsets(.init(top: 8, leading: 16, bottom: 8, trailing: 16))
                } header: {
                    Text("Now Playing")
                }
            }

            if !pendingItems.isEmpty {
                Section {
                    ForEach(pendingItems) { item in
                        if let episode = item.episode {
                            EpisodeRow(episode: episode, style: .withPodcast) {
                                QueueRowTrailing(episode: episode, onPlay: { play(item) })
                            }
                            .listRowInsets(.init(top: 8, leading: 16, bottom: 8, trailing: 16))
                            .swipeActions(edge: .leading, allowsFullSwipe: true) {
                                Button {
                                    moveToTop(item)
                                } label: {
                                    Label("Top", systemImage: "arrow.up.to.line")
                                }
                                .tint(.indigo)
                            }
                            .swipeActions(edge: .trailing, allowsFullSwipe: true) {
                                Button(role: .destructive) { remove(item) } label: {
                                    Label("Remove", systemImage: "trash")
                                }
                            }
                        }
                    }
                    .onMove(perform: move)
                } header: {
                    upNextHeader
                }
            } else if currentEpisode == nil {
                Section {
                    Text("Swipe an episode in Podcasts and tap Queue to add it.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .listRowInsets(.init(top: 10, leading: 16, bottom: 10, trailing: 16))
                }
            }
        }
        .listStyle(.plain)
    }

    private var upNextHeader: some View {
        HStack(spacing: 6) {
            Text("Up Next")
            if pendingDuration > 0 {
                Text("·")
                Text(TimeFormatting.minutesDuration(pendingDuration))
                    .monospacedDigit()
            }
        }
    }

    private var sortMenu: some View {
        Menu {
            Button {
                applySort { lhs, rhs in
                    (lhs.episode?.publishedAt ?? .distantPast) >
                    (rhs.episode?.publishedAt ?? .distantPast)
                }
            } label: {
                Label("Sort by release date — newest first", systemImage: "calendar")
            }
            Button {
                applySort { lhs, rhs in
                    (lhs.episode?.publishedAt ?? .distantPast) <
                    (rhs.episode?.publishedAt ?? .distantPast)
                }
            } label: {
                Label("Sort by release date — oldest first", systemImage: "calendar")
            }
            Button {
                applyGroupByPodcastSort()
            } label: {
                Label("Group by podcast", systemImage: "rectangle.3.group")
            }
        } label: {
            Image(systemName: "arrow.up.arrow.down")
        }
    }

    private func play(_ item: QueueItem) {
        guard let episode = item.episode else { return }
        if episode.processingState != .ready || !episode.isMarkedDownloaded {
            pipeline.process(episode: episode)
            return
        }
        let s = AppSettings.current(in: context)
        player.load(episode: episode, settings: s)
        player.play()
    }

    private func move(from source: IndexSet, to destination: Int) {
        // `pendingItems` excludes the currently-playing item; reorder within
        // that visible list, then map back to the full `items` position
        // numbering (keeping the playing item at its current position).
        var working = pendingItems
        working.move(fromOffsets: source, toOffset: destination)
        var pos = 0
        let playingID = player.currentEpisodeID
        let playing = items.first { $0.episode?.persistentModelID == playingID }
        playing?.position = pos
        if playing != nil { pos += 1 }
        for item in working {
            item.position = pos
            pos += 1
        }
        try? context.save()
    }

    private func remove(_ item: QueueItem) {
        if let episode = item.episode {
            // Unified delete: wipes the audio file too so it doesn't linger
            // in the Downloads tab after being removed from the queue.
            SubscriptionService.shared.deleteEpisodeContent(episode, in: context)
        } else {
            context.delete(item)
            try? context.save()
        }
    }

    private func moveToTop(_ item: QueueItem) {
        // Renumber so this item lands just after the currently-playing one
        // (if there is one) — i.e. it becomes the next to play.
        //
        // `withAnimation` so the position writes and the @Query-driven row
        // reorder ride the same transaction; without it the swipe action's
        // spring-back animation finishes before the row moves, leaving a
        // visible gap where the row used to be.
        withAnimation {
            let playingID = player.currentEpisodeID
            let playing = items.first { $0.episode?.persistentModelID == playingID }
            let rest = items.filter { $0 !== item && $0 !== playing }
            var pos = 0
            if let playing { playing.position = pos; pos += 1 }
            item.position = pos
            pos += 1
            for other in rest {
                other.position = pos
                pos += 1
            }
            try? context.save()
        }
    }

    private func applySort(by areInIncreasingOrder: (QueueItem, QueueItem) -> Bool) {
        // Keep the currently-playing item pinned at position 0 so it remains
        // "next to be auto-advanced past". Sort everything else.
        withAnimation {
            let playingID = player.currentEpisodeID
            let playing = items.first { $0.episode?.persistentModelID == playingID }
            let rest = items.filter { $0 !== playing }
            let sorted = rest.sorted(by: areInIncreasingOrder)
            var pos = 0
            if let playing { playing.position = pos; pos += 1 }
            for item in sorted {
                item.position = pos
                pos += 1
            }
            try? context.save()
        }
    }

    /// Group by podcast, with **groups** sorted by their *earliest*
    /// `publishedAt` (ascending), and episodes **within** each group also
    /// sorted by `publishedAt` ascending. Result reads like
    /// `AAAABBCC` where A's oldest queued episode is the oldest overall.
    private func applyGroupByPodcastSort() {
        withAnimation {
            let playingID = player.currentEpisodeID
            let playing = items.first { $0.episode?.persistentModelID == playingID }
            let rest = items.filter { $0 !== playing }

            let groups = Dictionary(grouping: rest) { item -> URL? in
                item.episode?.podcast?.feedURL
            }
            let keysInOrder = groups.keys.sorted { lk, rk in
                let lMin = groups[lk]!.compactMap { $0.episode?.publishedAt }.min() ?? .distantPast
                let rMin = groups[rk]!.compactMap { $0.episode?.publishedAt }.min() ?? .distantPast
                return lMin < rMin
            }
            var pos = 0
            if let playing { playing.position = pos; pos += 1 }
            for key in keysInOrder {
                let group = groups[key]!.sorted {
                    ($0.episode?.publishedAt ?? .distantPast) < ($1.episode?.publishedAt ?? .distantPast)
                }
                for item in group {
                    item.position = pos
                    pos += 1
                }
            }
            try? context.save()
        }
    }
}

/// Queue rows show a play/download button plus the drag-handle glyph (which
/// is purely cosmetic — `.onMove` drives the actual reorder gesture).
private struct QueueRowTrailing: View {
    @Bindable var episode: Episode
    let onPlay: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Button(action: onPlay) {
                Image(systemName: episode.processingState == .ready && episode.isMarkedDownloaded ? "play.circle.fill" : "arrow.down.circle")
                    .font(.title2)
            }
            .buttonStyle(.plain)
            Image(systemName: "line.3.horizontal")
                .font(.body)
                .foregroundStyle(.tertiary)
                .accessibilityHidden(true)
        }
    }
}
