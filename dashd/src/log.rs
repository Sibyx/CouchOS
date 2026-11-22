//! Lines for journald: no timestamps (journald adds them), and the sd-daemon
//! `<N>` level prefix it turns into a priority.

use std::sync::atomic::{AtomicBool, Ordering};

static DEBUG: AtomicBool = AtomicBool::new(false);

pub fn set_debug(on: bool) {
    DEBUG.store(on, Ordering::Relaxed);
}

pub fn info(msg: &str) {
    eprintln!("<6>{msg}");
}

pub fn warn(msg: &str) {
    eprintln!("<4>{msg}");
}

pub fn debug(msg: &str) {
    if DEBUG.load(Ordering::Relaxed) {
        eprintln!("<7>{msg}");
    }
}
