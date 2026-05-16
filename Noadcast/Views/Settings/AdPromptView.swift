import SwiftUI

/// Read-only display of the hardcoded ad-detection system prompt. The
/// prompt is no longer user-configurable; this view exists so curious
/// users can see what's being sent to the model.
struct AdPromptView: View {
    var body: some View {
        Form {
            Section {
                Text("This system prompt is sent to whichever model is configured for ad detection (on-device or cloud) along with the timestamped transcript of each episode.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            Section("Prompt") {
                Text(AdDetectionService.detectionPrompt)
                    .font(.body.monospaced())
                    .textSelection(.enabled)
            }
        }
        .navigationTitle("Detection prompt")
        .navigationBarTitleDisplayMode(.inline)
    }
}
