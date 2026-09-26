import Foundation
import SwiftData

/// Singleton sync bookkeeping row. Kept separate from `AppSettings` because
/// it is written on every sync page, and `AppSettings` writes invalidate
/// every settings observer.
@Model
final class SyncCursor {
    /// `instanceId` of the server database the mirror was built from. A
    /// different value means the server was recreated and every id is
    /// meaningless.
    var instanceId: String? = nil
    /// Delta cursor: the `nextSince` of the last fully applied page.
    var nextSince: Int = 0
    var lastSyncAt: Date? = nil
    var lastFullSyncAt: Date? = nil

    init() {}

    /// Fetches the singleton, inserting (and saving) it if missing.
    static func current(in context: ModelContext) -> SyncCursor {
        let hadRow = ((try? context.fetchCount(FetchDescriptor<SyncCursor>())) ?? 0) > 0
        let cursor = fetchOrInsert(in: context)
        if !hadRow {
            try? context.save()
        }
        return cursor
    }

    /// Like `current(in:)` but never saves, so callers can fold the insert
    /// into their own transaction. Collapses accidental duplicates.
    static func fetchOrInsert(in context: ModelContext) -> SyncCursor {
        let existing = (try? context.fetch(FetchDescriptor<SyncCursor>())) ?? []
        if let first = existing.first {
            for duplicate in existing.dropFirst() {
                context.delete(duplicate)
            }
            return first
        }
        let cursor = SyncCursor()
        context.insert(cursor)
        return cursor
    }
}
