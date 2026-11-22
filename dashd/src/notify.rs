//! Who hears about a change. The TV does not poll: Kodi gets a JSON-RPC
//! notification and the screensaver re-reads /v1/views/tv only then.

use std::sync::Arc;

use serde_json::json;

use crate::log;
use crate::net::Fetch;

pub trait Notifier: Send + Sync {
    fn changed(&self, version: u64);
}

/// `JSONRPC.NotifyAll {"sender": "dashd", "message": "changed"}`: add-ons see
/// it as `onNotification("dashd", "Other.changed", '{"version": n}')`.
/// Kodi not running is normal (restarts, AirPlay hand-offs): ignored.
pub struct KodiNotifier {
    pub http: Arc<dyn Fetch>,
    pub rpc_url: String,
}

impl Notifier for KodiNotifier {
    fn changed(&self, version: u64) {
        let body = json!({
            "jsonrpc": "2.0", "id": 1, "method": "JSONRPC.NotifyAll",
            "params": {"sender": "dashd", "message": "changed", "data": {"version": version}},
        });
        if let Err(err) = self.http.post_json(&self.rpc_url, &body) {
            log::debug(&format!("kodi notify: {err}"));
        }
    }
}

/// Every notifier, in order. More clients (MQTT, SSE) plug in here.
pub struct Fanout(pub Vec<Box<dyn Notifier>>);

impl Notifier for Fanout {
    fn changed(&self, version: u64) {
        for n in &self.0 {
            n.changed(version);
        }
    }
}
