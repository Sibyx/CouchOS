//! dashd.json: where to listen, whom to notify, which widgets in which order.
//! Without a file, dashd runs the default Bratislava dashboard.

use std::collections::HashSet;
use std::path::Path;

use serde::Deserialize;
use serde_json::{Value, json};

use crate::assets::AssetStore;
use crate::error::{Error, Result};

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct Config {
    #[serde(default = "default_bind")]
    pub bind: String,
    /// Kodi's JSON-RPC over HTTP; `null` to not notify Kodi.
    #[serde(default = "default_kodi")]
    pub kodi_rpc: Option<String>,
    #[serde(default = "default_widgets")]
    pub widgets: Vec<WidgetSpec>,
}

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct WidgetSpec {
    pub id: String,
    pub kind: String,
    #[serde(default)]
    pub params: Value,
}

fn default_bind() -> String {
    "0.0.0.0:8766".into()
}

fn default_kodi() -> Option<String> {
    Some("http://127.0.0.1:8080/jsonrpc".into())
}

fn default_widgets() -> Vec<WidgetSpec> {
    serde_json::from_value(json!([
        {"id": "clock", "kind": "clock"},
        {"id": "weather", "kind": "weather",
         "params": {"place": "Bratislava · Staré Mesto", "latitude": 48.1437, "longitude": 17.1066}},
        {"id": "frog", "kind": "frog"},
    ]))
    .expect("default widgets")
}

impl Config {
    pub fn load(path: Option<&Path>) -> Result<Self> {
        let config: Self = match path {
            Some(path) => {
                let bytes = std::fs::read(path).map_err(|e| Error::internal(format!("{}: {e}", path.display())))?;
                serde_json::from_slice(&bytes).map_err(|e| Error::invalid(format!("{}: {e}", path.display())))?
            }
            None => serde_json::from_value(json!({}))?,
        };
        config.validate()?;
        Ok(config)
    }

    fn validate(&self) -> Result<()> {
        let mut seen = HashSet::new();
        for w in &self.widgets {
            // Ids end up in URLs, file names and Kodi property names.
            if !AssetStore::valid_name(&w.id) || w.id.contains('.') {
                return Err(Error::invalid(format!("widget id {:?}: use letters, digits, - and _", w.id)));
            }
            if !seen.insert(&w.id) {
                return Err(Error::invalid(format!("widget id {:?} is used twice", w.id)));
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_build_and_ids_are_checked() {
        let config = Config::load(None).unwrap();
        assert_eq!(config.widgets.len(), 3);
        for w in &config.widgets {
            crate::widgets::build(&w.kind, &w.params).unwrap();
        }
        let twice: Config = serde_json::from_value(json!({"widgets": [{"id": "a", "kind": "clock"}, {"id": "a", "kind": "clock"}]})).unwrap();
        assert!(twice.validate().is_err());
        let dotted: Config = serde_json::from_value(json!({"widgets": [{"id": "a.b", "kind": "clock"}]})).unwrap();
        assert!(dotted.validate().is_err());
    }
}
