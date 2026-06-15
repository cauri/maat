import Foundation
import NaturalLanguage

// On-device text similarity used by re-rank (#53) and semantic search (#53). Tries Apple's sentence
// embeddings (NaturalLanguage, present on-device without Apple Intelligence) and degrades to lexical
// overlap when an embedding model isn't available for the text's language.

enum Semantics {
    static func similarity(_ a: String, _ b: String) -> Double {
        if let e = embeddingSimilarity(a, b) { return e }
        return lexicalOverlap(a, b)
    }

    static func embeddingSimilarity(_ a: String, _ b: String) -> Double? {
        guard let emb = NLEmbedding.sentenceEmbedding(for: .english),
              let va = emb.vector(for: a),
              let vb = emb.vector(for: b)
        else { return nil }
        return cosine(va, vb)
    }

    static func cosine(_ a: [Double], _ b: [Double]) -> Double {
        guard a.count == b.count, !a.isEmpty else { return 0 }
        var dot = 0.0, na = 0.0, nb = 0.0
        for i in a.indices {
            dot += a[i] * b[i]
            na += a[i] * a[i]
            nb += b[i] * b[i]
        }
        guard na > 0, nb > 0 else { return 0 }
        return dot / (sqrt(na) * sqrt(nb))
    }

    static func lexicalOverlap(_ a: String, _ b: String) -> Double {
        let sa = tokens(a), sb = tokens(b)
        guard !sa.isEmpty, !sb.isEmpty else { return 0 }
        let inter = Double(sa.intersection(sb).count)
        let union = Double(sa.union(sb).count)
        return union == 0 ? 0 : inter / union
    }

    static func tokens(_ s: String) -> Set<String> {
        Set(
            s.lowercased()
                .split { !$0.isLetter && !$0.isNumber }
                .map(String.init)
                .filter { $0.count > 2 }
        )
    }
}
