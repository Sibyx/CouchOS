//! One thread polls every widget. It sleeps until the earliest due time (or a
//! "refresh now" from the API) instead of ticking: idle, dashd uses no CPU.

use std::sync::Arc;
use std::sync::mpsc::{Receiver, RecvTimeoutError, Sender, channel};
use std::thread;
use std::time::{Duration, Instant};

use crate::hub::Hub;

const RETRY_FIRST: Duration = Duration::from_secs(30);
const RETRY_MAX: Duration = Duration::from_secs(15 * 60);

/// Handle for the API: ask for a widget to be polled now.
#[derive(Clone)]
pub struct Trigger(Sender<usize>);

impl Trigger {
    pub fn refresh(&self, slot: usize) {
        let _ = self.0.send(slot);
    }
}

pub fn spawn(hub: Arc<Hub>) -> Trigger {
    let (tx, rx) = channel();
    thread::Builder::new()
        .name("scheduler".into())
        .stack_size(256 * 1024)
        .spawn(move || run(&hub, &rx))
        .expect("spawn scheduler");
    Trigger(tx)
}

/// Retry delay after `failures` failures in a row: 30 s, 1 min, 2 min, ... 15 min.
fn backoff(failures: u32) -> Duration {
    RETRY_FIRST.saturating_mul(1 << failures.saturating_sub(1).min(5)).min(RETRY_MAX)
}

fn run(hub: &Hub, rx: &Receiver<usize>) {
    let now = Instant::now();
    // Everything polled is due at start (initial output may be a saved copy).
    let mut due: Vec<Option<Instant>> =
        hub.slots.iter().map(|s| s.widget.next_refresh(&hub.ctx).map(|_| now)).collect();
    let mut failures = vec![0u32; hub.slots.len()];

    loop {
        let next = due.iter().flatten().min().copied();
        let woken = match next {
            Some(at) => rx.recv_timeout(at.saturating_duration_since(Instant::now())),
            None => rx.recv().map_err(|_| RecvTimeoutError::Disconnected),
        };
        let now = Instant::now();
        let ready: Vec<usize> = match woken {
            Ok(i) if i < due.len() => vec![i],
            Ok(_) => continue,
            Err(RecvTimeoutError::Timeout) => (0..due.len()).filter(|&i| due[i].is_some_and(|t| t <= now)).collect(),
            Err(RecvTimeoutError::Disconnected) => return,
        };
        for i in ready {
            let slot = &hub.slots[i];
            let ok = hub.refresh(slot);
            failures[i] = if ok { 0 } else { failures[i] + 1 };
            let wait = if ok { slot.widget.next_refresh(&hub.ctx) } else { Some(backoff(failures[i])) };
            due[i] = wait.map(|d| Instant::now() + d);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_doubles_then_caps() {
        assert_eq!(backoff(1), Duration::from_secs(30));
        assert_eq!(backoff(2), Duration::from_secs(60));
        assert_eq!(backoff(3), Duration::from_secs(120));
        assert_eq!(backoff(10), RETRY_MAX);
    }
}
