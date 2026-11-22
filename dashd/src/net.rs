//! Outbound HTTP behind a trait, so widgets never see the HTTP library and
//! tests can answer with canned JSON.

use std::io::Read;
use std::time::Duration;

use serde_json::Value;

use crate::error::{Error, Result};

pub trait Fetch: Send + Sync {
    fn get_json(&self, url: &str) -> Result<Value>;
    /// The body, refused beyond `limit` bytes.
    fn get_bytes(&self, url: &str, limit: u64) -> Result<Vec<u8>>;
    fn post_json(&self, url: &str, body: &Value) -> Result<()>;
}

/// One shared agent: connection reuse and rustls (no OpenSSL on the Pi).
pub struct HttpClient {
    agent: ureq::Agent,
}

impl HttpClient {
    pub fn new(user_agent: &str, timeout: Duration) -> Self {
        let agent = ureq::AgentBuilder::new()
            .timeout_connect(Duration::from_secs(5))
            .timeout(timeout)
            .user_agent(user_agent)
            .build();
        Self { agent }
    }

    fn call(&self, request: ureq::Request) -> Result<ureq::Response> {
        let url = request.url().to_owned();
        request.call().map_err(|err| upstream(&url, err))
    }
}

fn upstream(url: &str, err: ureq::Error) -> Error {
    // Strip the query: it is long and says nothing useful in a log line.
    let host = url.split('?').next().unwrap_or(url);
    match err {
        ureq::Error::Status(code, _) => Error::upstream(format!("{host}: HTTP {code}")),
        ureq::Error::Transport(t) => Error::upstream(format!("{host}: {t}")),
    }
}

impl Fetch for HttpClient {
    fn get_json(&self, url: &str) -> Result<Value> {
        let response = self.call(self.agent.get(url))?;
        Ok(response.into_json()?)
    }

    fn get_bytes(&self, url: &str, limit: u64) -> Result<Vec<u8>> {
        let response = self.call(self.agent.get(url))?;
        let mut body = Vec::with_capacity(256 * 1024);
        response.into_reader().take(limit + 1).read_to_end(&mut body)?;
        if body.len() as u64 > limit {
            return Err(Error::upstream(format!("{url}: larger than {limit} bytes")));
        }
        Ok(body)
    }

    fn post_json(&self, url: &str, body: &Value) -> Result<()> {
        self.agent.post(url).send_json(body).map_err(|err| upstream(url, err))?;
        Ok(())
    }
}
