//! The extension point. A widget is anything that produces dashboard data:
//!
//! - **polled**: the scheduler calls [`Widget::refresh`] when
//!   [`Widget::next_refresh`] says so (weather, the frog of the day);
//! - **pushed / stateful**: a client POSTs to `/v1/widgets/<id>` and
//!   [`Widget::handle`] turns that into new output (a weather station sending
//!   readings, a TODO list taking "add"/"done" commands);
//! - **static**: neither; its output is fixed at start (the clock).
//!
//! Every output carries two shapes of the same thing:
//! `data`, structured JSON for real clients (the phone, the web page), and
//! `view`, flat display strings for clients that can only bind text (Kodi's
//! skin engine). Keeping both next to the code that understands the data means
//! no client re-implements formatting.

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Duration;

use serde_json::Value;

use crate::assets::AssetStore;
use crate::clock::Clock;
use crate::error::{Error, ErrorKind, Result};
use crate::net::Fetch;
use crate::state::StateDir;

/// Flat `key -> text` pairs; the store prefixes them with the widget id.
pub type View = BTreeMap<String, String>;

/// `view!{"temp" => t, "place" => p}`
#[macro_export]
macro_rules! view {
    ($($key:expr => $value:expr),* $(,)?) => {{
        let mut view = $crate::widget::View::new();
        $(view.insert(String::from($key), $value.to_string());)*
        view
    }};
}

#[derive(Debug, Clone, PartialEq)]
pub struct Output {
    pub data: Value,
    pub view: View,
}

impl Output {
    pub fn new(data: Value, view: View) -> Self {
        Self { data, view }
    }
}

/// What `refresh` found.
#[derive(Debug, PartialEq)]
pub enum Refresh {
    Changed(Output),
    /// Checked; nothing new (the frog is still today's frog).
    Unchanged,
}

/// Services a widget may use. Borrowed per call: widgets hold no globals.
pub struct Ctx {
    pub http: Arc<dyn Fetch>,
    pub clock: Arc<dyn Clock>,
    pub assets: Arc<AssetStore>,
    pub state: Arc<StateDir>,
}

pub trait Widget: Send + Sync {
    /// Stable type name ("weather"); clients pick a renderer by it.
    fn kind(&self) -> &'static str;

    /// Output available without I/O at start: fixed data or a saved copy.
    fn initial(&self, _ctx: &Ctx) -> Option<Output> {
        None
    }

    /// When to call `refresh` next. `None`: never polled.
    fn next_refresh(&self, _ctx: &Ctx) -> Option<Duration> {
        None
    }

    fn refresh(&self, _ctx: &Ctx) -> Result<Refresh> {
        Ok(Refresh::Unchanged)
    }

    /// Input from a client (`POST /v1/widgets/<id>` with a JSON body).
    fn handle(&self, _ctx: &Ctx, _input: Value) -> Result<Output> {
        Err(Error::new(ErrorKind::Unsupported, format!("{} widgets take no input", self.kind())))
    }
}

/// A widget's config `params` (missing = `{}`), errors naming the kind.
pub fn params<T: serde::de::DeserializeOwned>(kind: &str, params: &Value) -> Result<T> {
    let value = if params.is_null() { Value::Object(Default::default()) } else { params.clone() };
    serde_json::from_value(value).map_err(|e| Error::invalid(format!("{kind} params: {e}")))
}

/// Builds a widget from its config `params`. Registered per kind in
/// [`crate::widgets::KINDS`]; adding a widget type never touches existing ones.
pub type Factory = fn(&Value) -> Result<Box<dyn Widget>>;
