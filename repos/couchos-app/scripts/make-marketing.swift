// Builds CouchOS's sharing and App Store visuals from raw phone screenshots,
// following docs/BRAND.md.
//
//   swift /abs/path/repos/couchos-app/scripts/make-marketing.swift /abs/path/repos/couchos-app
//
// Input:  marketing/raw/NN.png, screenshots straight from an iPhone 17 Pro
//         (1206 x 2622), taken with:
//         xcrun devicectl device capture screenshot --device <udid> --destination .../marketing/raw/NN.png
// Output (marketing/out/):
//   appstore-NN.png     1206 x 2622  App Store screenshots (6.3" slot), captioned
//   social-card.png     1200 x 630   link previews (Open Graph / Twitter), TestFlight invite posts
//   appstore-header.png 3840 x 1646  App Store product page header (safe area 1646 x 661)
//   appstore-search-results.png 3840 x 2560  App Store search results (safe area 2168 x 1030)
// Creative-asset sizes and safe areas: developer.apple.com/help/app-store-connect/
// reference/app-information/creative-assets-specifications/
// Every output is opaque RGB. Missing raw screenshots are skipped.

import CoreGraphics
import CoreText
import Foundation
import ImageIO
import UniformTypeIdentifiers

// MARK: brand

func rgb(_ hex: UInt32, _ alpha: CGFloat = 1) -> CGColor {
    CGColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255, alpha: alpha)
}
let ink = 0x101214 as UInt32, panel = 0x1A1D21 as UInt32, line = 0x2A2E34 as UInt32
let text = 0xE8E6E3 as UInt32, muted = 0x8B9098 as UInt32, amber = 0xE5A00D as UInt32

/// One screen of the App Store set: the raw file and its two-line caption.
let screens: [(file: String, title: String, subtitle: String)] = [
    ("01.png", "Your Plex library,\nin your hand", "Browse on the phone. Watch on the TV."),
    ("02.png", "Play on TV\nin one tap", "Resume right where you left off."),
    ("03.png", "A remote that\nfeels right", "Swipe, tap, type. No pointing at the TV."),
    ("04.png", "Every library,\nevery poster", "Sort it, filter it, pick tonight's film."),
    ("05.png", "Find anything,\nfast", "Search as you type. Accents optional."),
    ("06.png", "TV, sound\nand speakers", "Power, volume and Bluetooth in one place."),
]

// MARK: drawing helpers (CoreGraphics: origin bottom-left)

let space = CGColorSpace(name: CGColorSpace.sRGB)!

func canvas(_ w: Int, _ h: Int) -> CGContext {
    CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0, space: space,
              bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
}

func background(_ ctx: CGContext, _ w: CGFloat, _ h: CGFloat, glow: CGPoint, radius: CGFloat) {
    let gradient = CGGradient(colorsSpace: space, colors: [rgb(ink), rgb(panel)] as CFArray, locations: [0, 1])!
    ctx.drawLinearGradient(gradient, start: .zero, end: CGPoint(x: 0, y: h), options: [])
    let halo = CGGradient(colorsSpace: space, colors: [rgb(amber, 0.22), rgb(amber, 0)] as CFArray, locations: [0, 1])!
    ctx.drawRadialGradient(halo, startCenter: glow, startRadius: 0, endCenter: glow, endRadius: radius, options: [])
}

func font(_ size: CGFloat, bold: Bool) -> CTFont {
    let base = CTFontCreateUIFontForLanguage(bold ? .emphasizedSystem : .system, size, nil)!
    return base
}

enum Align { case left, center }

/// Draws text inside `rect`, top-aligned. Returns the height used.
@discardableResult
func draw(_ string: String, _ ctx: CGContext, in rect: CGRect, size: CGFloat, bold: Bool,
          color: CGColor, align: Align, lineHeight: CGFloat = 1.08) -> CGFloat {
    var alignment: CTTextAlignment = align == .center ? .center : .left
    var lineMultiple = lineHeight
    let style = withUnsafeBytes(of: &alignment) { alignmentBytes in
        withUnsafeBytes(of: &lineMultiple) { multipleBytes in
            let settings = [
                CTParagraphStyleSetting(spec: .alignment, valueSize: alignmentBytes.count, value: alignmentBytes.baseAddress!),
                CTParagraphStyleSetting(spec: .lineHeightMultiple, valueSize: multipleBytes.count, value: multipleBytes.baseAddress!),
            ]
            return CTParagraphStyleCreate(settings, settings.count)
        }
    }
    let attributed = NSAttributedString(string: string, attributes: [
        NSAttributedString.Key(kCTFontAttributeName as String): font(size, bold: bold),
        NSAttributedString.Key(kCTForegroundColorAttributeName as String): color,
        NSAttributedString.Key(kCTParagraphStyleAttributeName as String): style,
    ])
    let setter = CTFramesetterCreateWithAttributedString(attributed)
    let fit = CTFramesetterSuggestFrameSizeWithConstraints(setter, CFRange(), nil,
                                                           CGSize(width: rect.width, height: .greatestFiniteMagnitude), nil)
    let box = CGRect(x: rect.minX, y: rect.maxY - fit.height, width: rect.width, height: fit.height)
    let frame = CTFramesetterCreateFrame(setter, CFRange(), CGPath(rect: box, transform: nil), nil)
    CTFrameDraw(frame, ctx)
    return fit.height
}

/// A screenshot as a rounded "screen" with a hairline border and a soft shadow.
func phone(_ image: CGImage, _ ctx: CGContext, in rect: CGRect, radius: CGFloat) {
    let path = CGPath(roundedRect: rect, cornerWidth: radius, cornerHeight: radius, transform: nil)
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -rect.width * 0.02), blur: rect.width * 0.08, color: rgb(0x000000, 0.6))
    ctx.addPath(path)
    ctx.setFillColor(rgb(ink))
    ctx.fillPath()
    ctx.restoreGState()
    ctx.saveGState()
    ctx.addPath(path)
    ctx.clip()
    ctx.draw(image, in: rect)
    ctx.restoreGState()
    ctx.addPath(path)
    ctx.setStrokeColor(rgb(line))
    ctx.setLineWidth(max(2, rect.width * 0.004))
    ctx.strokePath()
}

func load(_ url: URL) -> CGImage? {
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
    return CGImageSourceCreateImageAtIndex(source, 0, nil)
}

func save(_ ctx: CGContext, _ url: URL) {
    let dest = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil)!
    CGImageDestinationAddImage(dest, ctx.makeImage()!, nil)
    guard CGImageDestinationFinalize(dest) else { fatalError("could not write \(url.path)") }
    print("wrote \(url.path) (\(ctx.width) x \(ctx.height))")
}

// MARK: outputs

/// 1206 x 2622: caption on top, the screen below, bleeding off the bottom edge.
func appStoreShot(_ shot: CGImage, title: String, subtitle: String) -> CGContext {
    let w: CGFloat = 1206, h: CGFloat = 2622
    let ctx = canvas(Int(w), Int(h))
    background(ctx, w, h, glow: CGPoint(x: w / 2, y: h - 260), radius: 900)
    let used = draw(title, ctx, in: CGRect(x: 90, y: 0, width: w - 180, height: h - 190),
                    size: 96, bold: true, color: rgb(text), align: .center)
    draw(subtitle, ctx, in: CGRect(x: 90, y: 0, width: w - 180, height: h - 190 - used - 34),
         size: 46, bold: false, color: rgb(muted), align: .center)
    let width: CGFloat = 930
    let height = width * CGFloat(shot.height) / CGFloat(shot.width)
    let top = h - 760
    phone(shot, ctx, in: CGRect(x: (w - width) / 2, y: top - height, width: width, height: height), radius: 64)
    return ctx
}

/// 1200 x 630: mark, name, line and link on the left; the app on the right.
func socialCard(_ icon: CGImage, shots: [CGImage]) -> CGContext {
    let w: CGFloat = 1200, h: CGFloat = 630
    let ctx = canvas(Int(w), Int(h))
    background(ctx, w, h, glow: CGPoint(x: 260, y: h), radius: 640)
    let iconSize: CGFloat = 150
    let iconRect = CGRect(x: 80, y: h - 80 - iconSize, width: iconSize, height: iconSize)
    ctx.saveGState()
    ctx.addPath(CGPath(roundedRect: iconRect, cornerWidth: iconSize * 0.225, cornerHeight: iconSize * 0.225, transform: nil))
    ctx.clip()
    ctx.draw(icon, in: iconRect)
    ctx.restoreGState()
    var y = iconRect.minY - 36
    y -= draw("CouchOS", ctx, in: CGRect(x: 80, y: 0, width: 600, height: y), size: 84, bold: true, color: rgb(text), align: .left)
    y -= 14
    y -= draw("Browse Plex on your iPhone.\nPlay it on the TV.", ctx, in: CGRect(x: 80, y: 0, width: 600, height: y),
              size: 38, bold: false, color: rgb(muted), align: .left, lineHeight: 1.15)
    draw("couchos.dubec.dev", ctx, in: CGRect(x: 80, y: 0, width: 600, height: 92), size: 30, bold: true,
         color: rgb(amber), align: .left)
    // Phones, tilted toward the text, cropped by the card's bottom edge.
    let width: CGFloat = 300
    for (index, shot) in shots.prefix(2).enumerated().reversed() {
        let height = width * CGFloat(shot.height) / CGFloat(shot.width)
        let x: CGFloat = index == 0 ? 720 : 900
        let top: CGFloat = index == 0 ? h - 50 : h - 120
        phone(shot, ctx, in: CGRect(x: x, y: top - height, width: width, height: height), radius: 34)
    }
    return ctx
}

/// Width of one line of text as `draw` would set it.
func measure(_ string: String, size: CGFloat, bold: Bool) -> CGFloat {
    var widest: CGFloat = 0
    for line in string.components(separatedBy: "\n") {
        let attributed = NSAttributedString(string: line, attributes: [
            NSAttributedString.Key(kCTFontAttributeName as String): font(size, bold: bold),
        ])
        let width = CTLineGetTypographicBounds(CTLineCreateWithAttributedString(attributed), nil, nil, nil)
        widest = max(widest, CGFloat(width))
    }
    return widest
}

/// A creative asset (App Store header, search results): the mark, name and
/// line centred inside the safe area App Store guarantees to show; phones at
/// both edges outside it, as decoration that may be cropped.
func creative(_ icon: CGImage, shots: [CGImage], width w: CGFloat, height h: CGFloat, safe: CGSize) -> CGContext {
    let ctx = canvas(Int(w), Int(h))
    background(ctx, w, h, glow: CGPoint(x: w / 2, y: h * 0.62), radius: w * 0.42)
    let safeRect = CGRect(x: (w - safe.width) / 2, y: (h - safe.height) / 2, width: safe.width, height: safe.height)

    // Edge phones, behind everything, tall enough to be cut by the canvas.
    if !shots.isEmpty {
        let phoneHeight = h * 0.92
        let phoneWidth = phoneHeight * CGFloat(shots[0].width) / CGFloat(shots[0].height)
        let gap = w * 0.03
        let left = shots[0], right = shots.count > 1 ? shots[shots.count > 2 ? 2 : 1] : shots[0]
        phone(left, ctx, in: CGRect(x: safeRect.minX - gap - phoneWidth, y: -h * 0.22, width: phoneWidth, height: phoneHeight),
              radius: phoneWidth * 0.07)
        phone(right, ctx, in: CGRect(x: safeRect.maxX + gap, y: h * 0.3, width: phoneWidth, height: phoneHeight),
              radius: phoneWidth * 0.07)
    }

    // The lock-up: icon left of name + line, the group centred in the safe area.
    let unit = min(safe.height / 661, safe.width / 1646)
    let iconSize = 330 * unit, titleSize = 176 * unit, lineSize = 62 * unit, gap = 64 * unit
    let tagline = "Your Plex library in your hand.\nThe big screen does the rest."
    let textWidth = max(measure("CouchOS", size: titleSize, bold: true), measure(tagline, size: lineSize, bold: false)) + 8
    let groupWidth = iconSize + gap + textWidth
    let x = safeRect.midX - groupWidth / 2
    let iconRect = CGRect(x: x, y: safeRect.midY - iconSize / 2, width: iconSize, height: iconSize)
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -iconSize * 0.04), blur: iconSize * 0.15, color: rgb(0x000000, 0.55))
    ctx.addPath(CGPath(roundedRect: iconRect, cornerWidth: iconSize * 0.225, cornerHeight: iconSize * 0.225, transform: nil))
    ctx.setFillColor(rgb(ink))
    ctx.fillPath()
    ctx.restoreGState()
    ctx.saveGState()
    ctx.addPath(CGPath(roundedRect: iconRect, cornerWidth: iconSize * 0.225, cornerHeight: iconSize * 0.225, transform: nil))
    ctx.clip()
    ctx.draw(icon, in: iconRect)
    ctx.restoreGState()
    let textX = iconRect.maxX + gap
    let textHeight = titleSize * 1.2 + 16 * unit + lineSize * 1.15 * 2
    var y = safeRect.midY + textHeight / 2
    y -= draw("CouchOS", ctx, in: CGRect(x: textX, y: 0, width: textWidth, height: y), size: titleSize, bold: true,
              color: rgb(text), align: .left)
    y -= 16 * unit
    draw(tagline, ctx, in: CGRect(x: textX, y: 0, width: textWidth, height: y), size: lineSize, bold: false,
         color: rgb(muted), align: .left, lineHeight: 1.12)
    return ctx
}

/// 3840 x 1646 (21:9): product page header. Safe area 1646 x 661.
func header(_ icon: CGImage, shots: [CGImage]) -> CGContext {
    creative(icon, shots: shots, width: 3840, height: 1646, safe: CGSize(width: 1646, height: 661))
}

/// 3840 x 2560 (3:2): App Store search results. Safe area 2168 x 1030.
func searchResults(_ icon: CGImage, shots: [CGImage]) -> CGContext {
    creative(icon, shots: shots, width: 3840, height: 2560, safe: CGSize(width: 2168, height: 1030))
}

// MARK: main

guard CommandLine.arguments.count == 2 else {
    print("usage: swift make-marketing.swift /abs/path/repos/couchos-app")
    exit(2)
}
let root = URL(fileURLWithPath: CommandLine.arguments[1])
let raw = root.appending(path: "marketing/raw")
let out = root.appending(path: "marketing/out")
try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
guard let icon = load(root.appending(path: "CouchOS/Assets.xcassets/AppIcon.appiconset/AppIcon.png")) else {
    fatalError("no app icon: run make-icon.swift first")
}

var available: [CGImage] = []
for (index, screen) in screens.enumerated() {
    guard let shot = load(raw.appending(path: screen.file)) else {
        print("skip \(screen.file): not in marketing/raw")
        continue
    }
    available.append(shot)
    save(appStoreShot(shot, title: screen.title, subtitle: screen.subtitle),
         out.appending(path: String(format: "appstore-%02d.png", index + 1)))
}
guard !available.isEmpty else { fatalError("no screenshots in \(raw.path)") }
save(socialCard(icon, shots: available.count > 1 ? [available[0], available[available.count > 2 ? 2 : 1]] : available),
     out.appending(path: "social-card.png"))
save(header(icon, shots: available), out.appending(path: "appstore-header.png"))
save(searchResults(icon, shots: available), out.appending(path: "appstore-search-results.png"))
