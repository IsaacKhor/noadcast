import SwiftUI

/// Sheet showing each detected skippable segment (ad, intro, or outro).
/// Reached by tapping the segments summary in `NowPlayingView`.
struct AdsTranscriptView: View {
    let ads: [AdMarker]
    let onSeek: (Double) -> Void

    private var sortedAds: [AdMarker] {
        ads.filter { !$0.isDeleted }.sorted { $0.startSeconds < $1.startSeconds }
    }

    var body: some View {
        NavigationStack {
            Group {
                if sortedAds.isEmpty {
                    ContentUnavailableView {
                        Label("Nothing to skip", systemImage: "speaker.slash")
                    } description: {
                        Text("No intro, outro, or ads detected — or the episode hasn't finished processing yet.")
                    }
                } else {
                    List {
                        ForEach(sortedAds) { ad in
                            Section {
                                Text("Tap to jump to the start of this segment.")
                                    .font(.subheadline)
                                    .foregroundStyle(.secondary)
                            } header: {
                                AdHeader(ad: ad, onSeek: onSeek)
                            }
                        }
                    }
                }
            }
            .navigationTitle("Skip Segments")
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}

private struct AdHeader: View {
    let ad: AdMarker
    let onSeek: (Double) -> Void

    private var fallbackTitle: String {
        switch ad.kind {
        case .ad: "Advertisement"
        case .intro: "Intro"
        case .outro: "Outro"
        }
    }

    var body: some View {
        Button {
            onSeek(ad.startSeconds)
        } label: {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: "speaker.slash.fill")
                    .foregroundStyle(ad.kind.tint)
                Text(ad.kind.label.uppercased())
                    .font(.caption2.bold())
                    .foregroundStyle(.white)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1)
                    .background(ad.kind.tint, in: Capsule())
                Text(ad.summary.isEmpty ? fallbackTitle : ad.summary)
                    .font(.subheadline.bold())
                    .foregroundStyle(.primary)
                Spacer()
                Text("\(TimeFormatting.timestamp(ad.startSeconds))–\(TimeFormatting.timestamp(ad.endSeconds))")
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            .textCase(nil)
        }
        .buttonStyle(.plain)
    }
}
