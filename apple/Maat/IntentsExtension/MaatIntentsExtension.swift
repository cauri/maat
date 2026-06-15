import AppIntents

// The App Intents extension principal (#83). Its presence is what lets the launch-free intents in this
// target run out of the app's process — Siri / Shortcuts / Spotlight execute them here so e.g. "what's
// the top story on Maat" answers in-place without launching the app. The extension's Info.plist points
// `EXPrincipalClass` at the generated `$(PRODUCT_MODULE_NAME).MaatIntentsExtension` (see project.yml).
struct MaatIntentsExtension: AppIntentsExtension {}
