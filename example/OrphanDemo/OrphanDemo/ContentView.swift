import SwiftUI

/// Every string below exists in Localizable.xcstrings as a Manual entry with an English and a
/// Spanish value. To see the check in action, add a line such as `Text("Orphan test string")`,
/// build, and commit.
struct ContentView: View {
    @State private var count = 3

    var body: some View {
        NavigationStack {
            List {
                Text("Welcome back")
                Text("Items: \(count)")
                Button("Add item") { count += 1 }
            }
            .navigationTitle("Settings")
        }
    }
}

#Preview {
    ContentView()
}
