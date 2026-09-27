import UIKit

/// Display-only conversion: preserve the server's original RSS metadata.
/// The HTML importer uses WebKit and must run on the main thread. Call once
/// when the summary changes, rather than while evaluating a view body.
@MainActor
enum RSSSummaryText {
    static func plainText(_ raw: String) -> String {
        // Plain prose keeps its original whitespace and literal comparisons.
        let markup = #"</?[A-Za-z][^>]*>|&(?:#[xX][0-9A-Fa-f]+|#[0-9]+|[A-Za-z][A-Za-z0-9]+);"#
        guard raw.range(of: markup, options: .regularExpression) != nil,
              let data = raw.data(using: .utf8),
              let parsed = try? NSAttributedString(
                data: data,
                options: [
                    .documentType: NSAttributedString.DocumentType.html,
                    .characterEncoding: String.Encoding.utf8.rawValue,
                ],
                documentAttributes: nil
              ) else { return raw }
        return parsed.string
            .replacingOccurrences(of: "\u{2028}", with: "\n")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }
}
