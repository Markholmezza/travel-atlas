// detect_people.swift — flag photos that show people, using Apple's Vision framework (macOS 12+).
//
// Usage:
//   swiftc -O tools/detect_people.swift -o /tmp/detect_people
//   /tmp/detect_people <photos-dir> [--min-height 0.10] [--scene 0.30] > review/detections.json
//
// <photos-dir> is laid out like photos/ in this project: <city>/<file>.jpg.
// Detectors, each run on the whole frame and again on overlapping tiles (2×2 and 3×3), because
// Vision scales the input down and misses mid-sized people in a full-frame pass:
//   VNDetectFaceRectanglesRequest   faces, frontal or in profile
//   VNDetectHumanRectanglesRequest  whole-body figures (upperBodyOnly = false)
//   VNDetectHumanBodyPoseRequest    body joints, which also catch people seen from behind or in silhouette
// Plus VNClassifyImageRequest on the whole frame, for scene labels such as "people" or "crowd".
// Verdict: "remove" when there is a face, or a figure taller than --min-height of the frame;
// otherwise "keep". Body-pose boxes also fire on statues, animals and rocks, so a pose-only figure
// counts only when something else agrees (a people-like scene label at or above --scene, a face,
// or a human rectangle); unconfirmed ones are kept but reported, as are smaller figures (distant
// crowds), so a reviewer can look at them.
// Boxes are normalised to the upright image, origin at the top left (ready for CSS overlays).

import Foundation
import ImageIO
import Vision

struct Box: Codable {
    let x: Double, y: Double, w: Double, h: Double
    let confidence: Double
    let source: String          // face / human / pose
    var joints: Int? = nil      // pose only: joints recognised, and their mean confidence
    var jointConfidence: Double? = nil
}

struct Label: Codable {
    let label: String
    let confidence: Double
}

struct Detection: Codable {
    let city: String
    let file: String
    let width: Int
    let height: Int
    let faces: [Box]
    let figures: [Box]
    let smallFigures: Int
    let unconfirmedFigures: Int
    let scene: [Label]
    let verdict: String
    let reasons: [String]
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(1)
}

func option(_ name: String, _ fallback: Double, _ args: inout [String]) -> Double {
    guard let i = args.firstIndex(of: name) else { return fallback }
    guard i + 1 < args.count, let v = Double(args[i + 1]) else { fail("\(name) needs a number") }
    args.removeSubrange(i...(i + 1))
    return v
}

var args = Array(CommandLine.arguments.dropFirst())
let minHeight = option("--min-height", 0.10, &args)
let sceneMin = option("--scene", 0.30, &args)
guard args.count == 1 else { fail("usage: detect_people <photos-dir> [--min-height 0.10] [--scene 0.30]") }
let root = URL(fileURLWithPath: args[0], isDirectory: true)
let imageExts: Set<String> = ["jpg", "jpeg", "png", "heic", "tif", "tiff"]
let peopleLabels: Set<String> = ["people", "adult", "child", "teen", "baby", "crowd"]
let minPoseJoints = 4
let minJointConfidence: Float = 0.2

// Whole frame, then 2×2 tiles (60% of each side) and 3×3 tiles (40%), all overlapping.
let regions: [CGRect] = [CGRect(x: 0, y: 0, width: 1, height: 1)]
    + [0.0, 0.4].flatMap { x in [0.0, 0.4].map { y in CGRect(x: x, y: y, width: 0.6, height: 0.6) } }
    + [0.0, 0.3, 0.6].flatMap { x in [0.0, 0.3, 0.6].map { y in CGRect(x: x, y: y, width: 0.4, height: 0.4) } }

/// A Vision rect inside `roi` (both bottom-left origin) → a frame box with top-left origin.
func box(_ r: CGRect, in roi: CGRect, _ confidence: Float, _ source: String) -> Box {
    let x = roi.minX + r.minX * roi.width, y = roi.minY + r.minY * roi.height
    let w = r.width * roi.width, h = r.height * roi.height
    return Box(x: Double(x), y: Double(1 - (y + h)), w: Double(w), h: Double(h), confidence: Double(confidence), source: source)
}

/// Bounding box of the recognised joints, padded a little for the head top and feet the joints don't reach.
func poseBox(_ obs: VNHumanBodyPoseObservation, in roi: CGRect) -> Box? {
    guard let points = try? obs.recognizedPoints(.all) else { return nil }
    let good = points.values.filter { $0.confidence >= minJointConfidence }
    guard good.count >= minPoseJoints else { return nil }
    let xs = good.map { $0.location.x }, ys = good.map { $0.location.y }
    var r = CGRect(x: xs.min()!, y: ys.min()!, width: xs.max()! - xs.min()!, height: ys.max()! - ys.min()!)
    r = r.insetBy(dx: -r.width * 0.15, dy: -r.height * 0.12).intersection(CGRect(x: 0, y: 0, width: 1, height: 1))
    var b = box(r, in: roi, obs.confidence, "pose")
    b.joints = good.count
    b.jointConfidence = Double(good.map(\.confidence).reduce(0, +)) / Double(good.count)
    return b
}

func iou(_ a: Box, _ b: Box) -> Double {
    let ix = max(0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x)), iy = max(0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    let inter = ix * iy
    return inter / min(a.w * a.h, b.w * b.h)          // overlap relative to the smaller box: a tile's partial view merges into the whole
}

/// Keep the larger box when two boxes cover the same person.
func merge(_ boxes: [Box]) -> [Box] {
    var kept: [Box] = []
    for b in boxes.sorted(by: { $0.w * $0.h > $1.w * $1.h }) where !kept.contains(where: { iou($0, b) > 0.6 }) {
        kept.append(b)
    }
    return kept
}

func detect(_ url: URL, city: String) -> Detection? {
    let name = "\(city)/\(url.lastPathComponent)"
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        FileHandle.standardError.write("skipped (unreadable): \(name)\n".data(using: .utf8)!)
        return nil
    }
    let props = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any]
    let raw = (props?[kCGImagePropertyOrientation] as? UInt32) ?? 1
    let orientation = CGImagePropertyOrientation(rawValue: raw) ?? .up
    let rotated = (5...8).contains(raw)               // 90° orientations swap width and height
    let handler = VNImageRequestHandler(cgImage: image, orientation: orientation, options: [:])

    var faces: [Box] = [], figures: [Box] = [], scene: [Label] = []
    do {
        for roi in regions {
            let f = VNDetectFaceRectanglesRequest(), h = VNDetectHumanRectanglesRequest(), p = VNDetectHumanBodyPoseRequest()
            h.upperBodyOnly = false
            for r in [f, h, p] as [VNImageBasedRequest] { r.regionOfInterest = roi }
            try handler.perform([f, h, p])
            faces += (f.results ?? []).map { box($0.boundingBox, in: roi, $0.confidence, "face") }
            figures += (h.results ?? []).map { box($0.boundingBox, in: roi, $0.confidence, "human") }
            figures += (p.results ?? []).compactMap { poseBox($0, in: roi) }
        }
        let c = VNClassifyImageRequest()
        try handler.perform([c])
        scene = (c.results ?? []).filter { peopleLabels.contains($0.identifier) && Double($0.confidence) >= sceneMin }
            .map { Label(label: $0.identifier, confidence: Double($0.confidence)) }
    } catch {
        FileHandle.standardError.write("skipped (\(error.localizedDescription)): \(name)\n".data(using: .utf8)!)
        return nil
    }
    faces = merge(faces)
    figures = merge(figures)
    let confirmed = !scene.isEmpty || !faces.isEmpty || figures.contains { $0.source == "human" }
    let tallAll = figures.filter { $0.h > minHeight }
    let tall = tallAll.filter { $0.source != "pose" || confirmed }

    var reasons: [String] = []
    if !faces.isEmpty {
        let biggest = faces.map(\.h).max()! * 100
        reasons.append("\(faces.count) face\(faces.count == 1 ? "" : "s") (largest \(String(format: "%.1f", biggest))% of frame height)")
    }
    if !tall.isEmpty {
        let biggest = tall.map(\.h).max()! * 100
        reasons.append("\(tall.count) figure\(tall.count == 1 ? "" : "s") over \(Int(minHeight * 100))% of frame height (largest \(String(format: "%.0f", biggest))%)")
    }
    return Detection(city: city, file: url.lastPathComponent,
                     width: rotated ? image.height : image.width, height: rotated ? image.width : image.height,
                     faces: faces, figures: figures, smallFigures: figures.count - tallAll.count,
                     unconfirmedFigures: tallAll.count - tall.count, scene: scene,
                     verdict: reasons.isEmpty ? "keep" : "remove", reasons: reasons)
}

let fm = FileManager.default
guard let cities = try? fm.contentsOfDirectory(at: root, includingPropertiesForKeys: [.isDirectoryKey]) else {
    fail("cannot read \(root.path)")
}
var results: [Detection] = []
for dir in cities.sorted(by: { $0.lastPathComponent < $1.lastPathComponent })
where (try? dir.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true && !dir.lastPathComponent.hasPrefix(".") {
    let files = (try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)) ?? []
    for f in files.sorted(by: { $0.lastPathComponent.localizedStandardCompare($1.lastPathComponent) == .orderedAscending })
    where imageExts.contains(f.pathExtension.lowercased()) {
        if let d = autoreleasepool(invoking: { detect(f, city: dir.lastPathComponent) }) { results.append(d) }
    }
}

let encoder = JSONEncoder()
encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
FileHandle.standardOutput.write(try encoder.encode(results))
FileHandle.standardOutput.write("\n".data(using: .utf8)!)
let keep = results.filter { $0.verdict == "keep" }
FileHandle.standardError.write(("\(results.count) photos: \(keep.count) keep, \(results.count - keep.count) remove; kept with small figures: "
    + "\(keep.filter { $0.smallFigures > 0 }.count), with an unconfirmed pose-only figure: \(keep.filter { $0.unconfirmedFigures > 0 }.count), "
    + "with a people-like scene label: \(keep.filter { !$0.scene.isEmpty }.count)\n").data(using: .utf8)!)
