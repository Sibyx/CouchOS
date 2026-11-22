//! Built-in widget kinds. A new kind is a module plus one line in [`KINDS`].

pub mod clock;
pub mod frog;
pub mod weather;

use serde_json::Value;

use crate::error::{Error, Result};
use crate::widget::{Factory, Widget};

pub const KINDS: &[(&str, Factory)] = &[
    ("clock", clock::build),
    ("weather", weather::build),
    ("frog", frog::build),
];

pub fn build(kind: &str, params: &Value) -> Result<Box<dyn Widget>> {
    let (_, factory) = KINDS
        .iter()
        .find(|(k, _)| *k == kind)
        .ok_or_else(|| Error::invalid(format!("unknown widget kind {kind:?} (known: {:?})", KINDS.iter().map(|k| k.0).collect::<Vec<_>>())))?;
    factory(params)
}
