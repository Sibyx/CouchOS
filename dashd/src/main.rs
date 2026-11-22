//! dashd: the CouchOS dashboard daemon. Composition root only; the parts are
//! documented in their modules, starting with [`widget`].
//!
//! Environment: DASHD_CONFIG (path to dashd.json, optional), DASHD_DEBUG=1,
//! XDG_STATE_HOME / XDG_CACHE_HOME (default ~/.local/state, ~/.cache).

mod assets;
mod clock;
mod config;
mod duck;
mod error;
mod http;
mod hub;
mod log;
mod net;
mod notify;
mod scheduler;
mod state;
mod store;
mod sun;
mod svg;
#[cfg(test)]
mod testing;
mod widget;
mod widgets;

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use crate::assets::AssetStore;
use crate::clock::SystemClock;
use crate::config::Config;
use crate::error::Result;
use crate::hub::{Hub, Slot};
use crate::net::{Fetch, HttpClient};
use crate::notify::{Fanout, KodiNotifier, Notifier};
use crate::state::StateDir;
use crate::widget::Ctx;

fn xdg(var: &str, fallback: &str) -> PathBuf {
    std::env::var_os(var).map(PathBuf::from).unwrap_or_else(|| {
        PathBuf::from(std::env::var_os("HOME").unwrap_or_else(|| "/tmp".into())).join(fallback)
    })
}

fn run() -> Result<()> {
    log::set_debug(std::env::var_os("DASHD_DEBUG").is_some_and(|v| v == "1"));
    let config = Config::load(std::env::var_os("DASHD_CONFIG").map(PathBuf::from).as_deref())?;

    let http: Arc<dyn Fetch> =
        Arc::new(HttpClient::new(concat!("dashd/", env!("CARGO_PKG_VERSION"), " (CouchOS)"), Duration::from_secs(20)));
    let ctx = Ctx {
        http: http.clone(),
        clock: Arc::new(SystemClock),
        assets: Arc::new(AssetStore::open(xdg("XDG_CACHE_HOME", ".cache").join("dashd"))?),
        state: Arc::new(StateDir::open(xdg("XDG_STATE_HOME", ".local/state").join("dashd"))?),
    };

    let slots = config
        .widgets
        .iter()
        .map(|spec| Ok(Slot { id: spec.id.clone(), widget: widgets::build(&spec.kind, &spec.params)? }))
        .collect::<Result<Vec<_>>>()?;

    // Kodi's own HTTP client: short timeout, it is on localhost.
    let kodi_http: Arc<dyn Fetch> = Arc::new(HttpClient::new("dashd", Duration::from_secs(2)));
    let notifiers: Vec<Box<dyn Notifier>> = config
        .kodi_rpc
        .iter()
        .map(|url| Box::new(KodiNotifier { http: kodi_http.clone(), rpc_url: url.clone() }) as Box<dyn Notifier>)
        .collect();

    let hub = Arc::new(Hub::new(slots, ctx, Arc::new(Fanout(notifiers))));
    let trigger = scheduler::spawn(hub.clone());
    http::run(&config.bind, http::Api { hub, trigger })
}

fn main() {
    if let Err(err) = run() {
        log::warn(&format!("dashd: {err}"));
        std::process::exit(1);
    }
}
