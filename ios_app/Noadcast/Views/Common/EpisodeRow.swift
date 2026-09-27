import SwiftUI
import SwiftData

/// Visual variants of `EpisodeRow`. Pick `.withPodcast` for lists that mix
/// episodes from multiple podcasts (Queue, Status, Latest) and
/// `.episodeOnly` for a single podcast's own episode list.
enum EpisodeRowStyle {
    case withPodcast
    case episodeOnly
}

/// The single canonical episode row used everywhere. Always shows:
///   * episode title
///   * date · duration · one glyph per status axis (on this device / on
///     the server) · ads-detected count
///   * a thin progress bar for in-progress work or partial playback
///   * a trailing affordance supplied by the caller (`StandardEpisodeAction`
///     for most lists; Queue / Status pass custom ones).
///
/// Tapping the title area opens `ShowNotesView`. Swipe actions are *not*
/// declared here — callers add their own per-list swipes via
/// `.swipeActions` on the row.
struct EpisodeRow<Trailing: View>: View {
    @Environment(\.modelContext) private var context
    @Bindable var episode: Episode
    let style: EpisodeRowStyle
    /// When `false` (the default), neither the progress bars nor the job
    /// status are rendered. Crucially, the row also won't *read*
    /// `downloadProgress`, `downloadedBytes`, `playbackPosition`, or the
    /// in-memory job progress, so Observation doesn't subscribe the row to
    /// them — download bytes, job polls, and 0.25 s playback ticks don't
    /// re-render every list. Only `StatusView` opts in.
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
            deviceBadge
            serverBadge
            adBadge
        }
        .font(.caption2)
        .foregroundStyle(.secondary)
    }

    /// Device axis: on this iPhone ✓, downloading ↓, waiting ⏱, failed ⚠.
    @ViewBuilder
    private var deviceBadge: some View {
        if episode.isMarkedDownloaded {
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(.green)
                .accessibilityLabel("Downloaded")
        } else {
            switch episode.downloadState {
            case .downloading:
                Image(systemName: "arrow.down.circle")
                    .foregroundStyle(.tint)
                    .accessibilityLabel("Downloading")
            case .queued:
                Image(systemName: "clock")
                    .accessibilityLabel("Waiting to download")
            case .failed:
                Image(systemName: "exclamationmark.circle")
                    .foregroundStyle(.orange)
                    .accessibilityLabel("Download failed")
            case .idle, .downloaded:
                EmptyView()
            }
        }
    }

    /// Server axis: fetching, transcribing, analysing, or failed. Nothing
    /// for resting states — the ad badge covers "ready".
    @ViewBuilder
    private var serverBadge: some View {
        switch episode.serverState {
        case .downloadPending, .downloading:
            Image(systemName: "icloud.and.arrow.down")
                .foregroundStyle(.tint)
                .accessibilityLabel("Server is fetching the audio")
        case .transcribePending, .transcribing:
            Image(systemName: "waveform")
                .foregroundStyle(.tint)
                .accessibilityLabel("Server is transcribing")
        case .classifyPending, .classifying:
            Image(systemName: "sparkles")
                .foregroundStyle(.tint)
                .accessibilityLabel("Server is detecting ads")
        case .failed:
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.red)
                .accessibilityLabel("Server processing failed")
        case .discovered, .downloaded, .transcribed, .ready, .unknown:
            EmptyView()
        }
    }

    @ViewBuilder
    private var adBadge: some View {
        if episode.activeAdMarkerCount > 0 {
            Text("·")
            Text("\(episode.activeAdMarkerCount) ad\(episode.activeAdMarkerCount == 1 ? "" : "s")")
                .foregroundStyle(.orange)
        }
    }

    /// Linear progress bar reused for:
    ///   * a download to this device — bytes from `downloadProgress`;
    ///   * a server job — progress from `/jobs/active` (in memory, never
    ///     SwiftData), with its status text;
    ///   * partially played — `playbackPosition / duration`.
    /// Plus a static error footer for failures on either axis.
    @ViewBuilder
    private var progressLine: some View {
        // Guard each progress branch on `showProgress` *before* it reads
        // any ticking property — short-circuiting keeps Observation from
        // subscribing the row to those writes outside the Status tab.
        if showProgress, episode.isBusy {
            if episode.downloadState.isActive {
                deviceDownloadProgress
            } else {
                serverJobProgress
            }
        } else if let failure = failureMessage {
            // Static error footer — safe to show in all tabs; it doesn't
            // re-render at frame rate the way the progress bars do.
            Text(failure)
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

    @ViewBuilder
    private var deviceDownloadProgress: some View {
        VStack(alignment: .leading, spacing: 2) {
            if episode.downloadState == .downloading, (episode.downloadTotalBytes ?? 0) > 0 {
                ProgressView(value: max(0, min(1, episode.downloadProgress)))
                    .progressViewStyle(.linear)
            } else {
                ProgressView()
                    .progressViewStyle(.linear)
            }
            HStack(spacing: 6) {
                Text(deviceDownloadLabel)
                if let detail = TimeFormatting.progressDetail(for: episode) {
                    Text("·")
                    Text(detail).monospacedDigit()
                }
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var serverJobProgress: some View {
        let job = SyncService.shared.activeJobs[episode.serverID]
        VStack(alignment: .leading, spacing: 2) {
            if let fraction = job?.fraction {
                ProgressView(value: fraction)
                    .progressViewStyle(.linear)
            } else {
                ProgressView()
                    .progressViewStyle(.linear)
            }
            HStack(spacing: 6) {
                Text(job?.statusText ?? episode.serverState.label)
                if let job, let detail = TimeFormatting.progressDetail(for: job) {
                    Text("·")
                    Text(detail).monospacedDigit()
                }
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
        }
    }

    private var deviceDownloadLabel: String {
        switch episode.downloadState {
        case .queued:
            episode.audioState.isPresent ? "Waiting to download" : "Waiting for your server…"
        case .downloading:
            "Downloading to this iPhone…"
        case .idle, .downloaded, .failed:
            ""
        }
    }

    private var failureMessage: String? {
        if episode.downloadState == .failed {
            return episode.downloadError ?? "Download failed."
        }
        if episode.serverState == .failed {
            return episode.serverError ?? "Processing failed on the server."
        }
        return nil
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

/// The trailing affordance for most lists: play (downloaded, streamable, or
/// preparable on the server), a spinner (in flight), retry (failed),
/// download (streaming not allowed here), or a disabled glyph (offline).
struct StandardEpisodeAction: View {
    @Environment(\.modelContext) private var context
    @Bindable var episode: Episode
    @AppStorage(StreamingPolicy.storageKey) private var streamingPolicy: StreamingPolicy = StreamingPolicy.defaultValue

    private let player = PlayerService.shared
    private let network = NetworkMonitor.shared

    var body: some View {
        switch action {
        case .play:
            Button(action: play) {
                Image(systemName: "play.circle.fill").font(.title2)
            }
            .buttonStyle(.plain)
        case .inProgress:
            ProgressView()
        case .retry:
            Button {
                SubscriptionService.shared.retry(episode, in: context)
            } label: {
                Image(systemName: "arrow.clockwise.circle").font(.title2)
            }
            .buttonStyle(.plain)
        case .download:
            Button {
                SubscriptionService.shared.download(episode, in: context)
            } label: {
                Image(systemName: "arrow.down.circle").font(.title2)
            }
            .buttonStyle(.plain)
        case .unavailable:
            Image(systemName: "icloud.slash")
                .font(.title2)
                .foregroundStyle(.tertiary)
                .accessibilityLabel("Not available offline")
        }
    }

    private var action: EpisodeRowAction {
        PlaybackSourceResolver.rowAction(
            isDownloaded: episode.isMarkedDownloaded,
            downloadState: episode.downloadState,
            serverState: episode.serverState,
            audioState: episode.audioState,
            isServerConfigured: APIConfiguration.isConfigured,
            isOnline: network.isOnline,
            isWiFi: network.isWiFi,
            streamingPolicy: streamingPolicy
        )
    }

    private func play() {
        let settings = AppSettings.current(in: context)
        let target = episode
        Task {
            await player.load(episode: target, settings: settings, autoPlay: true)
        }
    }
}
