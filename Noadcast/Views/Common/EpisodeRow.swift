import SwiftUI
import SwiftData

/// Visual variants of `EpisodeRow`. Pick `.withPodcast` for lists that mix
/// episodes from multiple podcasts (Queue, Downloads, Latest) and
/// `.episodeOnly` for a single podcast's own episode list.
enum EpisodeRowStyle {
    case withPodcast
    case episodeOnly
}

/// The single canonical episode row used everywhere. Always shows:
///   * episode title
///   * date · duration · download-status icon · ads-detected count
///   * a thin progress bar for in-progress processing or partial playback
///   * a trailing affordance supplied by the caller (`StandardEpisodeAction`
///     for most lists; Queue / Downloads pass custom ones).
///
/// Tapping the title area opens `ShowNotesView`. Swipe actions are *not*
/// declared here — callers add their own per-list swipes via
/// `.swipeActions` on the row.
struct EpisodeRow<Trailing: View>: View {
    @Environment(\.modelContext) private var context
    @Bindable var episode: Episode
    let style: EpisodeRowStyle
    /// When `false` (the default), neither the in-progress processing bar
    /// nor the partial-playback bar is rendered. Crucially, the row also
    /// won't *read* `processingProgress`, `processingCurrent`,
    /// `processingTotal`, or `playbackPosition`, so SwiftData's
    /// Observation doesn't track them — per-byte upload progress and
    /// 0.5s playback ticks no longer re-render the row. Only
    /// `DownloadsView` opts in; that's the one place those bars belong.
    var showProgress: Bool = false
    @ViewBuilder var trailing: () -> Trailing

    @State private var showNotes = false

    var body: some View {
        HStack(spacing: 12) {
            Button {
                showNotes = true
            } label: {
                HStack(spacing: 12) {
                    if style == .withPodcast {
                        CachedArtworkImage(url: episode.podcastArtworkDisplayURL, size: 44)
                            .frame(width: 44, height: 44)
                            .clipShape(RoundedRectangle(cornerRadius: 6))
                    }
                    VStack(alignment: .leading, spacing: 3) {
                        if style == .withPodcast, let podcastTitle = episode.podcastTitle {
                            Text(podcastTitle)
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                        Text(episode.title)
                            .font(.subheadline.bold())
                            .lineLimit(2)
                        detailLine
                        progressLine
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            trailing()
        }
        .sheet(isPresented: $showNotes) {
            ShowNotesView(episode: episode)
        }
    }

    @ViewBuilder
    private var detailLine: some View {
        HStack(spacing: 6) {
            if let date = episode.publishedAt {
                Text(date.formatted(date: .abbreviated, time: .omitted))
            }
            if let duration = episode.duration, duration > 0 {
                Text("·")
                Text(TimeFormatting.timestamp(duration)).monospacedDigit()
            }
            statusBadge
            adBadge
        }
        .font(.caption2)
        .foregroundStyle(.secondary)
    }

    /// One-glyph hint for "where is this episode in the pipeline":
    /// downloaded ✓, downloading ↓ (filled when downloaded-but-not-yet-analyzed),
    /// failed ⚠, in-progress spinner-y arrow. Nothing for `.new` — the row's
    /// progress bar fills in the detail there.
    @ViewBuilder
    private var statusBadge: some View {
        switch episode.processingState {
        case .ready:
            if episode.isMarkedDownloaded {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            }
        case .downloaded:
            Image(systemName: "arrow.down.circle.fill").foregroundStyle(.tint)
        case .downloading, .transcribing, .detectingAds:
            Image(systemName: "arrow.down.circle").foregroundStyle(.tint)
        case .uploading:
            Image(systemName: "arrow.up.circle").foregroundStyle(.tint)
        case .failed:
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.red)
        case .new:
            EmptyView()
        }
    }

    @ViewBuilder
    private var adBadge: some View {
        if episode.processingState == .ready, episode.activeAdMarkerCount > 0 {
            Text("·")
            Text("\(episode.activeAdMarkerCount) ad\(episode.activeAdMarkerCount == 1 ? "" : "s")")
                .foregroundStyle(.orange)
        }
    }

    /// Linear progress bar reused for two cases plus an error footer for
    /// failed jobs:
    ///   * actively processing (download / transcribe / ad-detect) — uses
    ///     `processingProgress` reset to 0 at each stage transition. The
    ///     stage label + a unit-appropriate detail (`12 MB / 50 MB`,
    ///     `12:34 / 45:00`, `Chunk 3 of 12`) sits below the bar.
    ///   * partially played — `playbackPosition / duration`, only when
    ///     listening is in progress (not played-to-end).
    ///   * failed — show the error message.
    @ViewBuilder
    private var progressLine: some View {
        // Guard each progress branch on `showProgress` *before* it reads
        // any ticking property — short-circuiting keeps SwiftData
        // Observation from subscribing the row to those writes outside
        // the Downloads tab.
        if showProgress, episode.isInProgress {
            VStack(alignment: .leading, spacing: 2) {
                ProgressView(value: max(0, min(1, episode.processingProgress)))
                    .progressViewStyle(.linear)
                HStack(spacing: 6) {
                    Text(processingLabel)
                    if let detail = TimeFormatting.progressDetail(for: episode) {
                        Text("·")
                        Text(detail).monospacedDigit()
                    }
                }
                .font(.caption2)
                .foregroundStyle(.secondary)
            }
        } else if episode.processingState == .failed, let err = episode.processingError {
            // Static error footer — safe to show in all tabs; it doesn't
            // re-render at frame rate the way the progress bars do.
            Text(err)
                .font(.caption2)
                .foregroundStyle(.red)
                .lineLimit(3)
        } else if showProgress,
                  let duration = episode.duration,
                  duration > 0,
                  episode.playbackPosition > 0,
                  !episode.isPlayed,
                  episode.playbackPosition < duration {
            ProgressView(value: max(0, min(1, episode.playbackPosition / duration)))
                .progressViewStyle(.linear)
                .tint(.secondary)
        }
    }

    private var processingLabel: String {
        switch episode.processingState {
        case .downloading: "Downloading…"
        case .uploading: "Uploading…"
        case .transcribing: "Transcribing…"
        case .detectingAds: "Analyzing…"
        default: ""
        }
    }
}

/// Convenience initializer for callers that want the default play/download
/// trailing button.
extension EpisodeRow where Trailing == StandardEpisodeAction {
    init(episode: Episode, style: EpisodeRowStyle, showProgress: Bool = false) {
        self.episode = episode
        self.style = style
        self.showProgress = showProgress
        self.trailing = { StandardEpisodeAction(episode: episode) }
    }
}

/// The trailing affordance for most lists: play (ready), spinner (busy),
/// retry (failed), or download (anything else).
struct StandardEpisodeAction: View {
    @Environment(\.modelContext) private var context
    @Bindable var episode: Episode

    private let player = PlayerService.shared
    private let pipeline = ProcessingPipeline.shared

    var body: some View {
        if episode.processingState == .ready, episode.isMarkedDownloaded {
            Button(action: play) {
                Image(systemName: "play.circle.fill").font(.title2)
            }
            .buttonStyle(.plain)
        } else if pipeline.isProcessing(episodeID: episode.persistentModelID) {
            ProgressView()
        } else if episode.processingState == .failed {
            Button { pipeline.process(episode: episode) } label: {
                Image(systemName: "arrow.clockwise.circle").font(.title2)
            }
            .buttonStyle(.plain)
        } else {
            Button { pipeline.process(episode: episode) } label: {
                Image(systemName: "arrow.down.circle").font(.title2)
            }
            .buttonStyle(.plain)
        }
    }

    private func play() {
        let settings = AppSettings.current(in: context)
        player.load(episode: episode, settings: settings)
        player.play()
    }
}
