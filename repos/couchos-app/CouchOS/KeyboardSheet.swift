import SwiftUI

/// Opens automatically when Kodi asks for text (Input.OnInputRequested).
/// Every keystroke is mirrored to the TV; Return confirms.
struct KeyboardSheet: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    let request: AppModel.KeyboardRequest
    @State private var text = ""
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            VStack(spacing: 16) {
                TextField(request.title, text: $text)
                    .textFieldStyle(.roundedBorder)
                    .font(.title3)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .submitLabel(.send)
                    .focused($focused)
                    .onChange(of: text) { _, value in model.sendText(value, done: false) }
                    .onSubmit {
                        model.sendText(text, done: true)
                        dismiss()
                    }
                Spacer()
            }
            .padding()
            .navigationTitle(request.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Send") {
                        model.sendText(text, done: true)
                        dismiss()
                    }
                }
            }
        }
        .presentationDetents([.height(220)])
        .onAppear {
            text = request.text
            focused = true
        }
    }
}
