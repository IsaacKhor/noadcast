import SwiftUI
import SwiftData

struct ShowNotesView: View {
    @Environment(\.modelContext) private var context
    @Environment(\.dismiss) private var dismiss
    @Environment(\.colorScheme) private var colorScheme
    let episode: Episode

    @State private var toast: String?
    @State private var showTranscript = false
    /// Rendered show-notes HTML. Parsed lazily in `.task` so the sheet's
    /// appearance animation isn't blocked by NSAttributedString's HTML
    /// parser (which uses WebKit and must run on the main thread).
    @State private var renderedNotes: AttributedString?

    private let player = PlayerService.shared

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    Text(episode.title)
                        .font(.title2.bold())
                    HStack(spacing: 6) {
                        if let podcastTitle = episode.podcast?.title {
                            Text(podcastTitle)
                        }
                        if let date = episode.publishedAt {
                            Text("·")
                            Text(date.formatted(date: .abbreviated, time: .omitted))
                        }
                    }
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    metadataBlock
                    if !episode.transcript.isEmpty {
                        Button {
                            showTranscript = true
                        } label: {
                            Label("Show Transcript", systemImage: "text.alignleft")
                        }
                        .buttonStyle(.bordered)
                        .padding(.top, 4)
                    }
                    Divider()
                    if let renderedNotes {
                        Text(renderedNotes).textSelection(.enabled)
                    } else {
                        // Placeholder while the HTML parser runs on the next
                        // main-thread tick — keeps the sheet animation smooth.
                        Text(episode.episodeDescription ?? "No show notes.")
                            .foregroundStyle(.secondary)
                            .redacted(reason: .placeholder)
                    }
                }
                .padding()
            }
            .task {
                // Yield once so the sheet's slide-in animation gets a frame
                // to start before the (heavy, main-thread-only) HTML parse
                // begins. Without this delay the parse blocks the runloop
                // for ~100–500 ms on long show notes and the sheet "snaps"
                // in instead of animating.
                try? await Task.sleep(for: .milliseconds(80))
                if Task.isCancelled { return }
                renderedNotes = rendered(episode.episodeDescription ?? "No show notes.")
            }
            .navigationTitle("Show Notes")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Done") { dismiss() }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        queueAction()
                    } label: {
                        Label("Add to Queue", systemImage: "text.badge.plus")
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        redownloadAction()
                    } label: {
                        Label("Re-download & analyze", systemImage: "icloud.and.arrow.down")
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        reanalyzeAction()
                    } label: {
                        Label("Re-analyze", systemImage: "sparkles")
                    }
                    .disabled(!episode.hasLocalFile)
                }
            }
            .sheet(isPresented: $showTranscript) {
                TranscriptView(
                    segments: episode.transcript,
                    adRegions: adRegionsForEpisode,
                    onSeek: { time in
                        seek(to: time)
                        showTranscript = false
                    }
                )
            }
            .overlay(alignment: .bottom) {
                if let toast {
                    Text(toast)
                        .font(.subheadline)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 8)
                        .background(.regularMaterial, in: Capsule())
                        .padding(.bottom, 24)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                }
            }
            .animation(.spring(duration: 0.3), value: toast)
        }
    }

    // MARK: - Metadata

    @ViewBuilder
    private var metadataBlock: some View {
        let rows = metadataRows()
        if !rows.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(rows, id: \.label) { row in
                    HStack {
                        Text(row.label).foregroundStyle(.secondary)
                        Spacer()
                        Text(row.value).monospacedDigit()
                    }
                }
            }
            .font(.subheadline)
            .padding(.top, 4)
        }
    }

    private func metadataRows() -> [(label: String, value: String)] {
        var rows: [(label: String, value: String)] = []
        if let duration = episode.duration, duration > 0 {
            rows.append(("Duration", TimeFormatting.longDuration(duration)))
        }
        if let bytes = episode.fileSizeBytes, bytes > 0 {
            rows.append(("File size", TimeFormatting.fileSize(bytes)))
        }
        if !episode.transcript.isEmpty {
            var words = 0
            var chars = 0
            for seg in episode.transcript {
                words += seg.text.split(whereSeparator: \.isWhitespace).count
                chars += seg.text.count
            }
            // Locale-aware number formatting (e.g. "5,234" in en, "5 234" in fr).
            let value = "\(words.formatted()) words (\(chars.formatted()) chars)"
            rows.append(("Transcript", value))

            // Ad-percentage row — total prefers `episode.duration` (from the
            // RSS feed) and falls back to the last transcript segment's end
            // time. Only shown when the transcript exists (i.e. analysis has
            // run); 0 % is meaningful info too.
            let total = (episode.duration ?? 0) > 0
                ? episode.duration!
                : (episode.transcript.map(\.endSeconds).max() ?? 0)
            if total > 0 {
                let activeAds = episode.adMarkers.filter { !$0.isDeleted }
                let adSeconds = activeAds.reduce(0.0) { $0 + ($1.endSeconds - $1.startSeconds) }
                let pct = adSeconds / total * 100
                let pctString = String(format: "%.1f%%", pct)
                let adsValue: String
                if adSeconds > 0 {
                    adsValue = "\(pctString) (\(TimeFormatting.longDuration(adSeconds)))"
                } else {
                    adsValue = "0%"
                }
                rows.append(("Ads", adsValue))
            }
        }
        return rows
    }

    // MARK: - Transcript helpers

    /// Ad regions belonging to *this* episode — used to highlight ad lines
    /// in the transcript view. Always built from the episode's own markers
    /// (not the player's `adRegions`) so we render them correctly even when
    /// the episode isn't currently loaded in the player.
    private var adRegionsForEpisode: [AdRegion] {
        episode.adMarkers
            .filter { !$0.isDeleted }
            .map { AdRegion(startSeconds: $0.startSeconds, endSeconds: $0.endSeconds, kind: $0.kind) }
            .sorted { $0.startSeconds < $1.startSeconds }
    }

    /// Tap on a transcript line seeks to that time. If this episode is the
    /// one currently loaded, we just seek. Otherwise we load it first
    /// (which sets it as the new "Now Playing") and seek there.
    private func seek(to time: Double) {
        if player.currentEpisodeID == episode.persistentModelID {
            player.seek(to: time)
        } else if episode.hasLocalFile {
            let settings = AppSettings.current(in: context)
            player.load(episode: episode, settings: settings)
            player.seek(to: time)
        }
    }

    // MARK: - Toolbar actions

    private func queueAction() {
        let added = SubscriptionService.shared.addToQueue(episode, in: context)
        showToast(added ? "Added to queue" : "Already in queue")
    }

    private func redownloadAction() {
        SubscriptionService.shared.redownloadAndReprocess(episode, in: context)
        showToast("Re-downloading and re-analyzing…")
    }

    private func reanalyzeAction() {
        SubscriptionService.shared.reanalyzeEpisode(episode, in: context)
        showToast("Re-analyzing existing audio…")
    }

    private func showToast(_ message: String) {
        toast = message
        Task {
            try? await Task.sleep(for: .seconds(2))
            await MainActor.run { toast = nil }
        }
    }

    // MARK: - HTML rendering

    /// Wraps the raw RSS-supplied HTML in a small style block so the rendered
    /// notes look like native iOS content. `NSAttributedString`'s HTML
    /// renderer honors a limited subset of CSS — sticking to properties it
    /// actually applies (font, color, margin, line-height, border-left).
    private func rendered(_ raw: String) -> AttributedString {
        let isDark = colorScheme == .dark
        let bodyColor = isDark ? "#F2F2F7" : "#1C1C1E"
        let mutedColor = isDark ? "#A1A1A6" : "#6E6E73"
        let linkColor = isDark ? "#0A84FF" : "#007AFF"
        let quoteBorder = isDark ? "#48484A" : "#D1D1D6"
        let codeBg = isDark ? "#2C2C2E" : "#F2F2F7"

        let css = """
        body {
            font-family: -apple-system, system-ui, sans-serif;
            font-size: 17px;
            line-height: 1.5;
            color: \(bodyColor);
            margin: 0;
            padding: 0;
        }
        p { margin: 0 0 0.85em 0; }
        a { color: \(linkColor); text-decoration: none; }
        ul, ol { margin: 0.4em 0 0.85em 0; padding-left: 1.4em; }
        li { margin-bottom: 0.3em; }
        h1, h2, h3, h4, h5 {
            font-weight: 600;
            margin: 1.1em 0 0.4em 0;
            color: \(bodyColor);
        }
        h1 { font-size: 1.4em; }
        h2 { font-size: 1.25em; }
        h3 { font-size: 1.12em; }
        blockquote {
            border-left: 3px solid \(quoteBorder);
            margin: 0 0 0.85em 0;
            padding: 0 0 0 0.9em;
            color: \(mutedColor);
        }
        hr { border: 0; border-top: 1px solid \(quoteBorder); margin: 1em 0; }
        code, pre {
            font-family: ui-monospace, SFMono-Regular, monospace;
            font-size: 0.92em;
            background: \(codeBg);
        }
        pre { padding: 0.6em; border-radius: 6px; overflow-x: auto; }
        img { max-width: 100%; height: auto; }
        small { color: \(mutedColor); }
        """

        let html = """
        <!DOCTYPE html><html><head><meta charset="utf-8"><style>\(css)</style></head><body>\(raw)</body></html>
        """

        if let data = html.data(using: .utf8),
           let ns = try? NSAttributedString(
               data: data,
               options: [
                   .documentType: NSAttributedString.DocumentType.html,
                   .characterEncoding: String.Encoding.utf8.rawValue
               ],
               documentAttributes: nil
           ) {
            return AttributedString(ns)
        }
        return AttributedString(raw)
    }
}
