//
//  Item.swift
//  Noadcast
//
//  Created by Isaac Khor on 2026.05.15.
//

import Foundation
import SwiftData

@Model
final class Item {
    var timestamp: Date
    
    init(timestamp: Date) {
        self.timestamp = timestamp
    }
}
