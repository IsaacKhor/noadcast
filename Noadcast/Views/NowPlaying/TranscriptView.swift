import SwiftUI

struct TranscriptView: View {
    let segments: [TranscriptSegment]
    let adRegions: [AdRegion]
    let onSeek: (Double) -> Void

    var body: some View {
        NavigationStack {
            List(segments.sorted { $0.startSeconds < $1.startSeconds }) { seg in
                let inAd = adRegions.contains {
                    seg.startSeconds < $0.endSeconds && seg.endSeconds > $0.startSeconds
                }
                Button {
                    onSeek(seg.startSeconds)
                } label: {
                    HStack(alignment: .top, spacing: 8) {
                        Text(TimeFormatting.timestamp(seg.startSeconds))
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                            .frame(width: 56, alignment: .leading)
                        if inAd {
                            Text("AD")
                                .font(.caption2.bold())
                                .foregroundStyle(.white)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 2)
                                .background(.orange, in: Capsule())
                        }
                        Text(seg.text)
                            .foregroundStyle(inAd ? .orange : .primary)
                        Spacer()
                    }
                }
                .buttonStyle(.plain)
                .listRowBackground(inAd ? Color.orange.opacity(0.12) : Color.clear)
            }
            .navigationTitle("Transcript")
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}
