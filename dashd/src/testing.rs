//! Test doubles: canned HTTP answers, a fixed clock, throwaway directories.

use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};

use serde_json::Value;

use crate::assets::AssetStore;
use crate::clock::{Clock, Date, LocalNow};
use crate::error::{Error, Result};
use crate::net::Fetch;
use crate::state::StateDir;
use crate::widget::Ctx;

/// Answers by URL substring, first match wins; anything else is a 404.
#[derive(Default)]
pub struct FakeFetch {
    json: Vec<(&'static str, Value)>,
    bytes: Vec<(&'static str, &'static [u8])>,
}

impl FakeFetch {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn json(mut self, pattern: &'static str, body: Value) -> Self {
        self.json.push((pattern, body));
        self
    }

    pub fn bytes(mut self, pattern: &'static str, body: &'static [u8]) -> Self {
        self.bytes.push((pattern, body));
        self
    }
}

impl Fetch for FakeFetch {
    fn get_json(&self, url: &str) -> Result<Value> {
        self.json.iter().find(|(p, _)| url.contains(p)).map(|(_, v)| v.clone()).ok_or_else(|| Error::upstream(format!("{url}: HTTP 404")))
    }

    fn get_bytes(&self, url: &str, _limit: u64) -> Result<Vec<u8>> {
        self.bytes.iter().find(|(p, _)| url.contains(p)).map(|(_, b)| b.to_vec()).ok_or_else(|| Error::upstream(format!("{url}: HTTP 404")))
    }

    fn post_json(&self, _url: &str, _body: &Value) -> Result<()> {
        Ok(())
    }
}

/// Stopped at one moment. `FixedClock::on(date)` is 23:00 CET that day;
/// `FixedClock(LocalNow::at(...))` any other wall-clock time.
pub struct FixedClock(pub LocalNow);

impl FixedClock {
    pub fn on(date: Date) -> Self {
        Self(LocalNow::at(date, 23 * 3600, 3600))
    }
}

impl Clock for FixedClock {
    fn now(&self) -> LocalNow {
        self.0
    }
}

static DIRS: AtomicU32 = AtomicU32::new(0);

pub fn ctx_at(fetch: FakeFetch, date: Date) -> Ctx {
    ctx_with_clock(fetch, FixedClock::on(date))
}

pub fn ctx_with_clock(fetch: FakeFetch, clock: FixedClock) -> Ctx {
    let n = DIRS.fetch_add(1, Ordering::Relaxed);
    let root = std::env::temp_dir().join(format!("dashd-test-{}-{n}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    Ctx {
        http: Arc::new(fetch),
        clock: Arc::new(clock),
        assets: Arc::new(AssetStore::open(root.join("assets")).unwrap()),
        state: Arc::new(StateDir::open(root.join("state")).unwrap()),
    }
}
