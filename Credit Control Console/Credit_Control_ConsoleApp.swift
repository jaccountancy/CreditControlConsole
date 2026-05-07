//
//  Credit_Control_ConsoleApp.swift
//  Credit Control Console
//
//  Created by Jay Wilson on 07/05/2026.
//

import SwiftUI
import SwiftData

@main
struct Credit_Control_ConsoleApp: App {
    var sharedModelContainer: ModelContainer = {
        let schema = Schema([
            Item.self,
        ])
        let modelConfiguration = ModelConfiguration(schema: schema, isStoredInMemoryOnly: false)

        do {
            return try ModelContainer(for: schema, configurations: [modelConfiguration])
        } catch {
            fatalError("Could not create ModelContainer: \(error)")
        }
    }()

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
        .modelContainer(sharedModelContainer)
    }
}
