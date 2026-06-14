import Foundation
import SwiftData

// Reader comments, local only (#55, PLAN §6/§10: capture text + story + timestamp, gather-for-now,
// never leaves the phone). Stored in SwiftData; there is no server endpoint for these by design.

@Model
final class Comment {
    var storyID: String
    var storyFact: String
    var text: String
    var createdAt: Date

    init(storyID: String, storyFact: String, text: String, createdAt: Date = .now) {
        self.storyID = storyID
        self.storyFact = storyFact
        self.text = text
        self.createdAt = createdAt
    }
}
