//! The latest output of every widget, and the response bodies built from it.
//!
//! Writes are rare (a few per hour) and reads can be frequent, so bodies are
//! serialized once per change and every read hands out the same `Arc<[u8]>`:
//! a request costs a lock, a refcount bump and a socket write. The version
//! number doubles as the ETag, so an unchanged poll is a bodiless 304.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex, MutexGuard};

use serde_json::{Map, Value, json};

use crate::widget::Output;

#[derive(Debug, Clone)]
pub struct Snapshot {
    pub version: u64,
    pub body: Arc<[u8]>,
}

struct Entry {
    id: String,
    kind: &'static str,
    output: Option<Output>,
    updated_at: Option<u64>,
    error: Option<String>,
}

impl Entry {
    fn envelope(&self) -> Value {
        json!({
            "id": self.id,
            "kind": self.kind,
            "updated_at": self.updated_at,
            // Old data is still shown, marked as such.
            "stale": self.error.is_some() && self.output.is_some(),
            "error": self.error,
            "data": self.output.as_ref().map_or(Value::Null, |o| o.data.clone()),
        })
    }
}

struct Inner {
    version: u64,
    entries: Vec<Entry>,
    dashboard: Arc<[u8]>,
    tv: Arc<[u8]>,
    widgets: BTreeMap<String, Arc<[u8]>>,
}

pub struct Store {
    inner: Mutex<Inner>,
}

impl Store {
    /// Widgets in display order, as `(id, kind)`.
    pub fn new(widgets: impl IntoIterator<Item = (String, &'static str)>) -> Self {
        let entries = widgets
            .into_iter()
            .map(|(id, kind)| Entry { id, kind, output: None, updated_at: None, error: None })
            .collect();
        let empty: Arc<[u8]> = Arc::from(&b"{}"[..]);
        let mut inner = Inner { version: 0, entries, dashboard: empty.clone(), tv: empty, widgets: BTreeMap::new() };
        inner.rebuild();
        Self { inner: Mutex::new(inner) }
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        // A panic elsewhere must not take the dashboard down (panic=abort in
        // release anyway); the data under the lock is always consistent.
        self.inner.lock().unwrap_or_else(|p| p.into_inner())
    }

    /// New output for `id`. Returns the new version, or `None` when nothing
    /// a client sees changed.
    pub fn put(&self, id: &str, output: Output, now: u64) -> Option<u64> {
        let mut inner = self.lock();
        let entry = inner.entries.iter_mut().find(|e| e.id == id)?;
        if entry.output.as_ref() == Some(&output) && entry.error.is_none() {
            return None;
        }
        // updated_at: when the data last changed (or recovered).
        entry.updated_at = Some(now);
        entry.error = None;
        entry.output = Some(output);
        Some(inner.bump())
    }

    /// `id` failed to refresh; its last output stays, marked stale.
    pub fn fail(&self, id: &str, error: &str) -> Option<u64> {
        let mut inner = self.lock();
        let entry = inner.entries.iter_mut().find(|e| e.id == id)?;
        if entry.error.as_deref() == Some(error) {
            return None;
        }
        entry.error = Some(error.to_owned());
        Some(inner.bump())
    }

    pub fn dashboard(&self) -> Snapshot {
        let inner = self.lock();
        Snapshot { version: inner.version, body: inner.dashboard.clone() }
    }

    pub fn tv(&self) -> Snapshot {
        let inner = self.lock();
        Snapshot { version: inner.version, body: inner.tv.clone() }
    }

    pub fn widget(&self, id: &str) -> Option<Snapshot> {
        let inner = self.lock();
        inner.widgets.get(id).map(|body| Snapshot { version: inner.version, body: body.clone() })
    }
}

impl Inner {
    fn bump(&mut self) -> u64 {
        self.version += 1;
        self.rebuild();
        self.version
    }

    fn rebuild(&mut self) {
        let envelopes: Vec<Value> = self.entries.iter().map(Entry::envelope).collect();
        self.widgets = self
            .entries
            .iter()
            .zip(&envelopes)
            .map(|(e, env)| (e.id.clone(), Arc::from(env.to_string().into_bytes())))
            .collect();
        self.dashboard = Arc::from(json!({"version": self.version, "widgets": envelopes}).to_string().into_bytes());

        let mut props = Map::new();
        for entry in &self.entries {
            let stale = entry.error.is_some() && entry.output.is_some();
            props.insert(format!("{}.stale", entry.id), Value::from(if stale { "true" } else { "" }));
            for (key, text) in entry.output.iter().flat_map(|o| &o.view) {
                props.insert(format!("{}.{key}", entry.id), Value::from(text.as_str()));
            }
        }
        self.tv = Arc::from(json!({"version": self.version, "properties": props}).to_string().into_bytes());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn output(temp: &str) -> Output {
        Output::new(json!({"temp": temp}), crate::view! {"temp" => temp})
    }

    fn parse(body: &[u8]) -> Value {
        serde_json::from_slice(body).unwrap()
    }

    #[test]
    fn versions_only_move_on_visible_change() {
        let store = Store::new([("weather".to_owned(), "weather")]);
        assert_eq!(store.put("weather", output("2°"), 10), Some(1));
        assert_eq!(store.put("weather", output("2°"), 20), None);
        assert_eq!(store.put("weather", output("3°"), 30), Some(2));
        assert_eq!(store.put("nope", output("3°"), 30), None);
    }

    #[test]
    fn failure_keeps_data_and_marks_it_stale() {
        let store = Store::new([("weather".to_owned(), "weather")]);
        store.put("weather", output("2°"), 10);
        assert_eq!(store.fail("weather", "timeout"), Some(2));
        assert_eq!(store.fail("weather", "timeout"), None);
        let tv = parse(&store.tv().body);
        assert_eq!(tv["properties"]["weather.temp"], "2°");
        assert_eq!(tv["properties"]["weather.stale"], "true");
        let widget = parse(&store.widget("weather").unwrap().body);
        assert_eq!(widget["stale"], true);
        assert_eq!(widget["data"]["temp"], "2°");
        // Recovery clears it.
        store.put("weather", output("2°"), 30);
        assert_eq!(parse(&store.tv().body)["properties"]["weather.stale"], "");
    }

    #[test]
    fn dashboard_lists_widgets_in_order() {
        let store = Store::new([("clock".to_owned(), "clock"), ("frog".to_owned(), "frog")]);
        let dash = parse(&store.dashboard().body);
        let ids: Vec<_> = dash["widgets"].as_array().unwrap().iter().map(|w| w["id"].clone()).collect();
        assert_eq!(ids, [json!("clock"), json!("frog")]);
    }
}
