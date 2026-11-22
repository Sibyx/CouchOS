//! The clock is drawn by each client from its own time source (Kodi's
//! `$INFO[System.Time]` costs nothing). The widget only places it on the
//! dashboard and says which timezone it shows.

use serde::Deserialize;
use serde_json::{Value, json};

use crate::error::Result;
use crate::widget::{self, Ctx, Output, View, Widget};

#[derive(Deserialize)]
struct Params {
    #[serde(default = "default_timezone")]
    timezone: String,
}

fn default_timezone() -> String {
    "Europe/Bratislava".into()
}

pub struct Clock {
    timezone: String,
}

pub fn build(params: &Value) -> Result<Box<dyn Widget>> {
    let p: Params = widget::params("clock", params)?;
    Ok(Box::new(Clock { timezone: p.timezone }))
}

impl Widget for Clock {
    fn kind(&self) -> &'static str {
        "clock"
    }

    fn initial(&self, _ctx: &Ctx) -> Option<Output> {
        Some(Output::new(json!({"timezone": self.timezone}), View::new()))
    }
}
