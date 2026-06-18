import AppIntents

// The App Intents extension principal (#83). Its presence is what lets the launch-free intents in this
// target run out of the app's process — Siri / Shortcuts / Spotlight execute them here so e.g. "what's
// the top story on Maat" answers in-place without launching the app. `@main` is the ExtensionKit entry
// point: it emits the `__swift5_entry` section installd's ExtensionKit validator requires (error 73,
// AppexBundleUnexpectedSwiftSectionInExecutable, without it). It replaces the PluginKit-style
// `EXPrincipalClass`, which is why the Info.plist no longer declares one.
@main
struct MaatIntentsExtension: AppIntentsExtension {}
