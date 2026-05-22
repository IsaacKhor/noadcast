import SwiftUI
import UIKit

struct TranscriptView: View {
    let segments: [TranscriptSegment]
    let adRegions: [AdRegion]
    let onSeek: (Double) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var didCopy = false

    private var sortedSegments: [TranscriptSegment] {
        segments.sorted { $0.startSeconds < $1.startSeconds }
    }

    var body: some View {
        NavigationStack {
            Group {
                if sortedSegments.isEmpty {
                    ContentUnavailableView {
                        Label("No transcript saved", systemImage: "text.alignleft")
                    } description: {
                        Text("This episode was analyzed without storing transcript text.")
                    }
                } else {
                    List(sortedSegments) { seg in
                        let region = adRegions.first {
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
                                if let region {
                                    Text(region.kind.label.uppercased())
                                        .font(.caption2.bold())
                                        .foregroundStyle(.white)
                                        .padding(.horizontal, 6)
                                        .padding(.vertical, 2)
                                        .background(region.kind.tint, in: Capsule())
                                }
                                Text(seg.text)
                                    .foregroundStyle(region?.kind.tint ?? .primary)
                                    .textSelection(.enabled)
                                Spacer()
                            }
                        }
                        .buttonStyle(.plain)
                        .listRowBackground(region.map { $0.kind.tint.opacity(0.12) } ?? Color.clear)
                    }
                }
            }
            .navigationTitle("Transcript")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Done") { dismiss() }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        copyAllToClipboard()
                    } label: {
                        Image(systemName: didCopy ? "checkmark" : "doc.on.doc")
                            .symbolEffect(.bounce, value: didCopy)
                    }
                    .disabled(sortedSegments.isEmpty)
                    .accessibilityLabel("Copy transcript")
                }
            }
        }
    }

    private func copyAllToClipboard() {
        let plainText = sortedSegments
            .map { "[\(TimeFormatting.timestamp($0.startSeconds))] \($0.text)" }
            .joined(separator: "\n")
        UIPasteboard.general.string = plainText
        didCopy = true
        Task {
            try? await Task.sleep(for: .seconds(1.5))
            await MainActor.run { didCopy = false }
        }
    }
}
