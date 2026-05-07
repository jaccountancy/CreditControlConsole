//
//  Item.swift
//  Credit Control Console
//
//  Created by Jay Wilson on 07/05/2026.
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
