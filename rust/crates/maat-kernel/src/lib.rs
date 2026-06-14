//! `maat-kernel` — the deterministic spine.
//!
//! Events are the source of truth; all state is a **pure fold over the event log**
//! (PLAN §2.4). No I/O, no nondeterminism — that lives in the Python agent rim.
//! This is a seed: the real `Event` model and folds grow as the veracity pipeline
//! takes shape (PLAN §4). What matters now is the contract and its core invariant
//! — *same event log ⇒ same derived state* — which CI enforces from day one.

#![forbid(unsafe_code)]

/// Placeholder event. The real event enum grows with the pipeline (claims extracted,
/// classified, clustered, scored, resolved-against-primary-truth, …).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Event {
    /// A no-op marker, used only to anchor the fold contract until real events land.
    Noop,
}

/// A pure fold: derive state by folding events. Deterministic and replayable.
pub trait Fold {
    type State: Default;

    /// Apply a single event to the running state.
    fn apply(state: Self::State, event: &Event) -> Self::State;

    /// Replay an event log from the initial state — the only path to current state.
    fn replay(events: &[Event]) -> Self::State {
        events
            .iter()
            .fold(Self::State::default(), |s, e| Self::apply(s, e))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Counter;
    impl Fold for Counter {
        type State = u64;
        fn apply(state: u64, event: &Event) -> u64 {
            match event {
                Event::Noop => state + 1,
            }
        }
    }

    #[test]
    fn replay_counts_events() {
        let log = vec![Event::Noop, Event::Noop, Event::Noop];
        assert_eq!(Counter::replay(&log), 3);
    }

    #[test]
    fn replay_is_deterministic() {
        // The core event-sourcing invariant: same log ⇒ same derived state.
        let log = vec![Event::Noop, Event::Noop];
        assert_eq!(Counter::replay(&log), Counter::replay(&log));
    }
}
