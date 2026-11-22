// Draws the CouchOS app icon (and the README mark) from code, so the icon
// is reproducible and follows docs/BRAND.md exactly.
//
//   swift /abs/path/repos/couchos-app/scripts/make-icon.swift /abs/path/repos/couchos-app
//
// Writes, all 1024 x 1024, opaque RGB (App Store Connect rejects an icon
// with an alpha channel):
//   CouchOS/Assets.xcassets/AppIcon.appiconset/AppIcon.png         default
//   CouchOS/Assets.xcassets/AppIcon.appiconset/AppIcon-Dark.png    iOS dark icons
//   CouchOS/Assets.xcassets/AppIcon.appiconset/AppIcon-Tinted.png  iOS tinted icons (grayscale)
//   docs/images/icon.png                                           README / web

import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

struct RGB {
    let r: CGFloat, g: CGFloat, b: CGFloat
    init(_ hex: UInt32) {
        r = CGFloat((hex >> 16) & 0xFF) / 255
        g = CGFloat((hex >> 8) & 0xFF) / 255
        b = CGFloat(hex & 0xFF) / 255
    }
    func cg(_ alpha: CGFloat = 1) -> CGColor { CGColor(srgbRed: r, green: g, blue: b, alpha: alpha) }
}

// Brand tokens (docs/BRAND.md).
let ink = RGB(0x101214)
let panel = RGB(0x1A1D21)
let amber = RGB(0xE5A00D)
let amberLight = RGB(0xF2B940)
let amberDeep = RGB(0xB87E00)

struct Palette {
    var backgroundTop: RGB
    var backgroundBottom: RGB
    var glow: RGB?
    var arm: RGB
    var seat: RGB
    var back: RGB
    var seam: RGB
}

let standard = Palette(backgroundTop: panel, backgroundBottom: ink, glow: amber,
                       arm: amberLight, seat: amber, back: amberDeep, seam: ink)
let dark = Palette(backgroundTop: RGB(0x000000), backgroundBottom: RGB(0x000000), glow: amber,
                   arm: amberLight, seat: amber, back: amberDeep, seam: RGB(0x000000))
// Tinted icons are grayscale: the system colours them, light means strong.
let tinted = Palette(backgroundTop: RGB(0x000000), backgroundBottom: RGB(0x000000), glow: nil,
                     arm: RGB(0xFFFFFF), seat: RGB(0xE6E6E6), back: RGB(0xB3B3B3), seam: RGB(0x000000))

func rounded(_ ctx: CGContext, _ rect: CGRect, _ radius: CGFloat, _ color: CGColor) {
    ctx.addPath(CGPath(roundedRect: rect, cornerWidth: radius, cornerHeight: radius, transform: nil))
    ctx.setFillColor(color)
    ctx.fillPath()
}

func draw(_ p: Palette, size: Int = 1024) -> CGImage {
    let space = CGColorSpace(name: CGColorSpace.sRGB)!
    // noneSkipLast: RGB with no alpha channel.
    let ctx = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0,
                        space: space, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
    let s = CGFloat(size) / 1024
    ctx.scaleBy(x: s, y: s)

    // Background: a quiet vertical gradient, lighter at the top (CG's y is up).
    let gradient = CGGradient(colorsSpace: space, colors: [p.backgroundBottom.cg(), p.backgroundTop.cg()] as CFArray,
                              locations: [0, 1])!
    ctx.drawLinearGradient(gradient, start: CGPoint(x: 0, y: 0), end: CGPoint(x: 0, y: 1024), options: [])

    // The TV's light falling on the couch.
    if let glow = p.glow {
        let halo = CGGradient(colorsSpace: space, colors: [glow.cg(0.30), glow.cg(0)] as CFArray, locations: [0, 1])!
        ctx.drawRadialGradient(halo, startCenter: CGPoint(x: 512, y: 760), startRadius: 0,
                               endCenter: CGPoint(x: 512, y: 760), endRadius: 430, options: [])
    }

    // The couch, centred: back, base, two seat cushions, arms, legs.
    ctx.translateBy(x: 0, y: 44)
    rounded(ctx, CGRect(x: 290, y: 300, width: 40, height: 50), 12, p.back.cg())     // left leg
    rounded(ctx, CGRect(x: 694, y: 300, width: 40, height: 50), 12, p.back.cg())     // right leg
    rounded(ctx, CGRect(x: 292, y: 460, width: 440, height: 220), 66, p.back.cg())   // backrest
    rounded(ctx, CGRect(x: 252, y: 338, width: 520, height: 74), 30, p.back.cg())    // base
    rounded(ctx, CGRect(x: 304, y: 392, width: 416, height: 118), 34, p.seat.cg())   // seat
    ctx.setFillColor(p.seam.cg())
    ctx.fill(CGRect(x: 506, y: 392, width: 12, height: 118))                          // between cushions
    rounded(ctx, CGRect(x: 212, y: 338, width: 128, height: 254), 60, p.arm.cg())    // left arm
    rounded(ctx, CGRect(x: 684, y: 338, width: 128, height: 254), 60, p.arm.cg())    // right arm

    return ctx.makeImage()!
}

func write(_ image: CGImage, to url: URL) {
    try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    let dest = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil)!
    CGImageDestinationAddImage(dest, image, nil)
    guard CGImageDestinationFinalize(dest) else { fatalError("could not write \(url.path)") }
    print("wrote \(url.path)")
}

guard CommandLine.arguments.count == 2 else {
    print("usage: swift make-icon.swift /abs/path/repos/couchos-app")
    exit(2)
}
let root = URL(fileURLWithPath: CommandLine.arguments[1])
let set = root.appending(path: "CouchOS/Assets.xcassets/AppIcon.appiconset")
write(draw(standard), to: set.appending(path: "AppIcon.png"))
write(draw(dark), to: set.appending(path: "AppIcon-Dark.png"))
write(draw(tinted), to: set.appending(path: "AppIcon-Tinted.png"))
write(draw(standard, size: 512), to: root.appending(path: "docs/images/icon.png"))
