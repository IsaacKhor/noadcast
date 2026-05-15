import SwiftUI

/// Sheet showing only the transcript lines that fall inside detected ads,
/// grouped by ad. Reached by tapping the "ads detected" summary in
/// `NowPlayingView`.
struct AdsTranscriptView: View {
    let segments: [TranscriptSegment]
    let ads: [AdMarker]
    let onSeek: (Double) -> Void

    private var sortedAds: [AdMarker] {
        ads.filter { !$0.isDeleted }.sorted { $0.startSeconds < $1.startSeconds }
    }

    private func segments(for ad: AdMarker) -> [TranscriptSegment] {
        segments
            .filter { $0.startSeconds < ad.endSeconds && $0.endSeconds > ad.startSeconds }
            .sorted { $0.startSeconds < $1.startSeconds }
    }

    var body: some View {
        NavigationStack {
            Group {
                if sortedAds.isEmpty {
                    ContentUnavailableView {
                        Label("No ads detected", systemImage: "speaker.slash")
                    } description: {
                        Text("This episode looks ad-free, or it hasn't finished processing yet.")
                    }
                } else {
                    List {
                        ForEach(sortedAds) { ad in
                            Section {
                                ForEach(segments(for: ad)) { seg in
                                    Button {
                                        onSeek(seg.startSeconds)
                                    } label: {
                                        HStack(alignment: .top, spacing: 8) {
                                            Text(TimeFormatting.timestamp(seg.startSeconds))
                                                .font(.caption.monospacedDigit())
                                                .foregroundStyle(.secondary)
                                                .frame(width: 56, alignment: .leading)
                                            Text(seg.text)
                                                .foregroundStyle(.primary)
                                            Spacer()
                                        }
                                    }
                                    .buttonStyle(.plain)
                                }
                            } header: {
                                AdHeader(ad: ad, onSeek: onSeek)
                            }
                        }
                    }
                }
            }
            .navigationTitle("Ads")
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}

private struct AdHeader: View {
    let ad: AdMarker
    let onSeek: (Double) -> Void

    var body: some View {
        Button {
            onSeek(ad.startSeconds)
        } label: {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: "speaker.slash.fill")
                    .foregroundStyle(.orange)
                Text(ad.summary.isEmpty ? "Advertisement" : ad.summary)
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
