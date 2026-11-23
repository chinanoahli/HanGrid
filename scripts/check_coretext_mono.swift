// Run on macOS with: swift scripts/check_coretext_mono.swift FONT.ttf [...]
// Exits with failure if any input is unreadable or lacks CoreText's mono trait.
import Foundation
import CoreText

let paths = Array(CommandLine.arguments.dropFirst())
var failed = paths.isEmpty
if paths.isEmpty {
    print("Usage: swift scripts/check_coretext_mono.swift FONT.ttf [...]")
}
for path in paths {
    let url = URL(fileURLWithPath: path)
    guard let descriptors = CTFontManagerCreateFontDescriptorsFromURL(url as CFURL)
            as? [CTFontDescriptor], !descriptors.isEmpty else {
        print("ERROR: cannot read \(path)")
        failed = true
        continue
    }
    for descriptor in descriptors {
        let font = CTFontCreateWithFontDescriptor(descriptor, 20, nil)
        let mono = CTFontGetSymbolicTraits(font).contains(.traitMonoSpace)
        print("\(mono ? "PASS" : "FAIL") mono=\(mono) \(path) [\(CTFontCopyPostScriptName(font))]")
        if !mono { failed = true }
    }
}
exit(failed ? 1 : 0)
