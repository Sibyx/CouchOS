use std::fmt;

/// One error type for the whole daemon: a message and how an HTTP client
/// should see it. Widgets fail often (no network, an API changed) and the
/// failure is only ever logged or shown, so a string is all it needs to carry.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Error {
    pub kind: ErrorKind,
    pub message: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    /// Bad input from a client (400).
    Invalid,
    /// No such widget, asset or route (404).
    NotFound,
    /// The widget exists but does not take input (405).
    Unsupported,
    /// An upstream source failed or answered nonsense (502).
    Upstream,
    /// Disk, config, anything local (500).
    Internal,
}

impl Error {
    pub fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        Self { kind, message: message.into() }
    }

    pub fn invalid(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Invalid, message)
    }

    pub fn not_found(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::NotFound, message)
    }

    pub fn upstream(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Upstream, message)
    }

    pub fn internal(message: impl Into<String>) -> Self {
        Self::new(ErrorKind::Internal, message)
    }

    pub fn http_status(&self) -> u16 {
        match self.kind {
            ErrorKind::Invalid => 400,
            ErrorKind::NotFound => 404,
            ErrorKind::Unsupported => 405,
            ErrorKind::Upstream => 502,
            ErrorKind::Internal => 500,
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(err: std::io::Error) -> Self {
        Self::internal(err.to_string())
    }
}

impl From<serde_json::Error> for Error {
    fn from(err: serde_json::Error) -> Self {
        Self::upstream(format!("unexpected JSON: {err}"))
    }
}

pub type Result<T> = std::result::Result<T, Error>;
