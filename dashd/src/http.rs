//! The API. No auth: like couchd, it serves the trusted home LAN.
//!
//! ```text
//! GET  /v1/health
//! GET  /v1/dashboard            every widget: {version, widgets: [{id, kind, updated_at, stale, error, data}]}
//! GET  /v1/views/tv             flat display strings for Kodi: {version, properties: {"weather.temp": "2°", ...}}
//! GET  /v1/widgets/{id}         one widget's envelope
//! POST /v1/widgets/{id}         input for pushed/stateful widgets (JSON body), 405 for the others
//! POST /v1/widgets/{id}/refresh poll it now
//! GET  /v1/assets/{name}        files widgets serve (the frog photo)
//! ```
//!
//! JSON answers carry `ETag: "<version>"`; `If-None-Match` gets a bare 304.

use std::fs::File;
use std::io::{Cursor, Read};
use std::path::PathBuf;
use std::sync::Arc;
use std::thread;

use serde_json::{Value, json};
use tiny_http::{Header, Method, Request, Response, Server, StatusCode};

use crate::error::{Error, Result};
use crate::hub::Hub;
use crate::log;
use crate::scheduler::Trigger;
use crate::store::Snapshot;

const MAX_BODY: u64 = 64 * 1024;
const WORKERS: usize = 2;

#[derive(Debug)]
pub enum Reply {
    Json(Snapshot),
    Body(u16, Vec<u8>),
    NotModified,
    File(PathBuf),
    NoContent,
}

fn error_reply(err: &Error) -> Reply {
    Reply::Body(err.http_status(), json!({"error": err.message}).to_string().into_bytes())
}

fn small(value: Value) -> Reply {
    Reply::Body(200, value.to_string().into_bytes())
}

pub struct Api {
    pub hub: Arc<Hub>,
    pub trigger: Trigger,
}

impl Api {
    /// Routing, free of sockets so it can be tested directly.
    pub fn route(&self, method: &str, path: &str, if_none_match: Option<&str>, body: &[u8]) -> Reply {
        let parts: Vec<&str> = path.split('?').next().unwrap_or("").split('/').filter(|p| !p.is_empty()).collect();
        let snapshot = |s: Option<Snapshot>| match s {
            Some(s) if if_none_match == Some(etag(s.version).as_str()) => Reply::NotModified,
            Some(s) => Reply::Json(s),
            None => error_reply(&Error::not_found("no such widget")),
        };
        let store = &self.hub.store;
        let result: Result<Reply> = match (method, parts.as_slice()) {
            ("OPTIONS", _) => Ok(Reply::NoContent),
            ("GET", ["v1", "health"]) => Ok(small(json!({"ok": true, "version": env!("CARGO_PKG_VERSION")}))),
            ("GET", ["v1", "dashboard"]) => Ok(snapshot(Some(store.dashboard()))),
            ("GET", ["v1", "views", "tv"]) => Ok(snapshot(Some(store.tv()))),
            ("GET", ["v1", "widgets", id]) => Ok(snapshot(store.widget(id))),
            ("POST", ["v1", "widgets", id]) => serde_json::from_slice::<Value>(body)
                .map_err(|e| Error::invalid(format!("body must be JSON: {e}")))
                .and_then(|input| self.hub.handle(id, input))
                .map(|()| snapshot(store.widget(id))),
            ("POST", ["v1", "widgets", id, "refresh"]) => self.hub.slots.iter().position(|s| s.id == *id).map_or_else(
                || Err(Error::not_found(format!("no widget {id:?}"))),
                |i| {
                    self.trigger.refresh(i);
                    Ok(small(json!({"queued": true})))
                },
            ),
            ("GET", ["v1", "assets", name]) => self.hub.ctx.assets.path(name).map(Reply::File),
            _ => Err(Error::not_found(format!("no route for {method} {path}"))),
        };
        result.unwrap_or_else(|e| error_reply(&e))
    }
}

fn etag(version: u64) -> String {
    format!("\"{version}\"")
}

fn header(name: &str, value: &str) -> Header {
    Header::from_bytes(name.as_bytes(), value.as_bytes()).expect("valid header")
}

fn content_type(path: &std::path::Path) -> &'static str {
    match path.extension().and_then(|e| e.to_str()) {
        Some("jpg" | "jpeg") => "image/jpeg",
        Some("png") => "image/png",
        Some("svg") => "image/svg+xml",
        Some("json") => "application/json",
        _ => "application/octet-stream",
    }
}

fn respond(request: Request, reply: Reply) {
    let cors = [
        header("Access-Control-Allow-Origin", "*"),
        header("Access-Control-Allow-Headers", "Content-Type, If-None-Match"),
        header("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
    ];
    let json = header("Content-Type", "application/json");
    let result = match reply {
        Reply::Json(s) => {
            // The shared body itself, no copy per request.
            let len = s.body.len();
            let r = Response::new(StatusCode(200), vec![json], Cursor::new(s.body), Some(len), None)
                .with_header(header("ETag", &etag(s.version)))
                .with_header(header("Cache-Control", "no-cache"));
            send(request, r, cors)
        }
        Reply::Body(status, body) => send(request, Response::from_data(body).with_status_code(status).with_header(json), cors),
        Reply::NotModified => send(request, Response::empty(StatusCode(304)), cors),
        Reply::NoContent => send(request, Response::empty(StatusCode(204)), cors),
        Reply::File(path) => match File::open(&path) {
            // Streamed from disk; names change daily, so cache hard.
            Ok(file) => send(
                request,
                Response::from_file(file)
                    .with_header(header("Content-Type", content_type(&path)))
                    .with_header(header("Cache-Control", "public, max-age=604800, immutable")),
                cors,
            ),
            Err(err) => {
                respond(request, error_reply(&err.into()));
                Ok(())
            }
        },
    };
    if let Err(err) = result {
        log::debug(&format!("http: {err}"));
    }
}

fn send<R: Read>(request: Request, mut response: Response<R>, cors: [Header; 3]) -> std::io::Result<()> {
    for h in cors {
        response.add_header(h);
    }
    request.respond(response)
}

fn serve(server: &Server, api: &Api) {
    while let Ok(mut request) = server.recv() {
        let method = request.method().as_str().to_owned();
        let path = request.url().to_owned();
        let if_none_match = request
            .headers()
            .iter()
            .find(|h| h.field.equiv("If-None-Match"))
            .map(|h| h.value.as_str().to_owned());
        let mut body = Vec::new();
        if *request.method() == Method::Post {
            let _ = request.as_reader().take(MAX_BODY).read_to_end(&mut body);
        }
        let reply = api.route(&method, &path, if_none_match.as_deref(), &body);
        respond(request, reply);
    }
}

pub fn run(bind: &str, api: Api) -> Result<()> {
    let server = Arc::new(Server::http(bind).map_err(|e| Error::internal(format!("listen on {bind}: {e}")))?);
    let api = Arc::new(api);
    log::info(&format!("dashd {} listening on {bind}", env!("CARGO_PKG_VERSION")));
    let workers: Vec<_> = (1..WORKERS)
        .map(|n| {
            let (server, api) = (server.clone(), api.clone());
            thread::Builder::new()
                .name(format!("http-{n}"))
                .stack_size(256 * 1024)
                .spawn(move || serve(&server, &api))
                .expect("spawn http worker")
        })
        .collect();
    serve(&server, &api);
    for w in workers {
        let _ = w.join();
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::clock::Date;
    use crate::hub::Slot;
    use crate::notify::Fanout;
    use crate::testing::{FakeFetch, ctx_at};

    fn api() -> Api {
        let ctx = ctx_at(FakeFetch::new(), Date { year: 2026, month: 11, day: 22 });
        let slots = vec![Slot { id: "clock".into(), widget: crate::widgets::build("clock", &Value::Null).unwrap() }];
        let hub = Arc::new(Hub::new(slots, ctx, Arc::new(Fanout(vec![]))));
        let trigger = crate::scheduler::spawn(hub.clone());
        Api { hub, trigger }
    }

    fn body(reply: Reply) -> (u16, Value) {
        match reply {
            Reply::Json(s) => (200, serde_json::from_slice(&s.body).unwrap()),
            Reply::Body(status, b) => (status, serde_json::from_slice(&b).unwrap()),
            other => panic!("unexpected {other:?}"),
        }
    }

    #[test]
    fn serves_snapshots_with_etags() {
        let api = api();
        let (status, dash) = body(api.route("GET", "/v1/dashboard", None, b""));
        assert_eq!(status, 200);
        assert_eq!(dash["widgets"][0]["data"]["timezone"], "Europe/Bratislava");
        let version = dash["version"].as_u64().unwrap();
        let tag = etag(version);
        assert!(matches!(api.route("GET", "/v1/dashboard", Some(&tag), b""), Reply::NotModified));
        assert!(matches!(api.route("GET", "/v1/views/tv?x=1", Some(&tag), b""), Reply::NotModified));
    }

    #[test]
    fn errors_map_to_statuses() {
        let api = api();
        assert_eq!(body(api.route("GET", "/v1/widgets/nope", None, b"")).0, 404);
        assert_eq!(body(api.route("POST", "/v1/widgets/clock", None, b"{}")).0, 405);
        assert_eq!(body(api.route("POST", "/v1/widgets/clock", None, b"not json")).0, 400);
        assert_eq!(body(api.route("GET", "/v1/assets/..%2Fx", None, b"")).0, 400);
        assert_eq!(body(api.route("GET", "/v1/assets/missing.jpg", None, b"")).0, 404);
        assert_eq!(body(api.route("POST", "/v1/widgets/clock/refresh", None, b"")).0, 200);
        assert_eq!(body(api.route("DELETE", "/v1/dashboard", None, b"")).0, 404);
    }
}
