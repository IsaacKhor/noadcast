import SwiftUI

struct AdPromptView: View {
    @Bindable var settings: AppSettings

    var body: some View {
        Form {
            Section {
                Text("This prompt is sent to Apple's on-device language model along with the transcript of each episode. Adjust if it's flagging too many false positives or missing host-read ads.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            Section("Prompt") {
                TextEditor(text: $settings.adDetectionPrompt)
                    .frame(minHeight: 240)
                    .font(.body.monospaced())
            }
            Section {
                Button("Restore default", role: .destructive) {
                    settings.adDetectionPrompt = AppSettings.defaultAdDetectionPrompt
                }
            }
        }
        .navigationTitle("Detection prompt")
        .navigationBarTitleDisplayMode(.inline)
    }
}
