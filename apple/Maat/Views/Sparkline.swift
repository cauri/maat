import SwiftUI

/// A tiny reputation-trajectory line (BRIEF §6.4 — reputation is a value with history). Values are
/// expected in 0...1 but it auto-scales to its own min/max so small movements stay visible.
struct Sparkline: View {
    var values: [Double]
    var color: Color

    var body: some View {
        GeometryReader { geo in
            Path { path in
                let pts = points(in: geo.size)
                guard let first = pts.first else { return }
                path.move(to: first)
                for pt in pts.dropFirst() { path.addLine(to: pt) }
            }
            .stroke(color, style: StrokeStyle(lineWidth: 1.5, lineCap: .round, lineJoin: .round))
        }
    }

    private func points(in size: CGSize) -> [CGPoint] {
        guard values.count > 1 else {
            let mid = CGPoint(x: size.width / 2, y: size.height / 2)
            return values.isEmpty ? [] : [CGPoint(x: 0, y: size.height / 2), mid]
        }
        let lo = values.min() ?? 0
        let hi = values.max() ?? 1
        let span = max(hi - lo, 0.0001)
        let stepX = size.width / CGFloat(values.count - 1)
        return values.enumerated().map { index, value in
            CGPoint(
                x: CGFloat(index) * stepX,
                y: size.height - CGFloat((value - lo) / span) * size.height
            )
        }
    }
}
