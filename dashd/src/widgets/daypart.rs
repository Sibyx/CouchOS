//! Time of day: which **mode** the dashboard is in right now, by the local
//! clock (the Pi's Europe/Bratislava) and the sun.
//!
//! A mode is a named bundle of display settings; the TV applies them:
//!   layout  `full` (everything) or `clock` (only the time, big, on black)
//!   dim     0..=90 %: a veil drawn over the whole screen
//!   tint    the veil's colour ("#RRGGBB", black by default; warm = amber)
//!
//! Rules pick the mode. They run in order and a later rule that holds wins
//! (as the duck's do); none holding = `default`. A rule is
//!   {"mode": "night", "from": "23:00", "to": "06:30", "days": ["fri", "sat"]}
//! `from`/`to` are "HH:MM", "sunrise", "sunset", "sunset+30", "sunrise-15"
//! (minutes). A window past midnight belongs to the day it starts on, so
//! "fri" 23:00-08:00 is Friday night into Saturday morning. `days` are
//! mon..sun, "weekdays", "weekend"; none = every day. Sun times come from
//! `latitude`/`longitude` (computed here, no API); on a day without a
//! sunrise or sunset, rules anchored to it don't hold.
//!
//! Anything can set a mode for a while (the phone's "Good night"):
//! ```text
//! POST /v1/widgets/<id>  {"op": "set", "mode": "night"}                 until the schedule next changes
//!                        {"op": "set", "mode": "day", "minutes": 90}    for 90 minutes
//!                        {"op": "auto"}                                 back to the schedule
//! ```
//! The override is kept across restarts. dashd wakes exactly at the next
//! change (and at least hourly, which also absorbs DST shifts).
//!
//! A new setting = a field on `Mode`, a key in `output`'s view, and its use
//! in the skin (and the skin contract test). A new layout = a `Layout`
//! variant and a group in the skin shown for it.

use std::collections::BTreeMap;
use std::sync::Mutex;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::clock::{Date, LocalNow};
use crate::error::{Error, Result};
use crate::sun;
use crate::widget::{self, Ctx, Output, Refresh, View, Widget};

const DAY: i64 = 86_400;
/// Wake at least this often even without a change due (DST, a stuck clock).
const RECHECK_MAX: i64 = 3600;
const MAX_DIM: u8 = 90;
const MAX_OVERRIDE_MINUTES: u64 = 7 * 24 * 60;

/// What the TV skin can draw. Each one is a group in the skin XML.
#[derive(Deserialize, Serialize, Clone, Copy, Debug, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Layout {
    Full,
    Clock,
}

impl Layout {
    fn name(self) -> &'static str {
        match self {
            Self::Full => "full",
            Self::Clock => "clock",
        }
    }
}

#[derive(Deserialize, Serialize, Clone, Debug, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Mode {
    #[serde(default = "full")]
    pub layout: Layout,
    #[serde(default)]
    pub dim: u8,
    #[serde(default = "black")]
    pub tint: String,
}

fn full() -> Layout {
    Layout::Full
}
fn black() -> String {
    "#000000".into()
}

impl Mode {
    fn new(layout: Layout, dim: u8, tint: &str) -> Self {
        Self { layout, dim, tint: tint.into() }
    }

    /// The veil as Kodi's colordiffuse wants it, "AARRGGBB"; "" = none.
    fn veil(&self) -> String {
        if self.dim == 0 {
            return String::new();
        }
        let alpha = (u32::from(self.dim) * 255 + 50) / 100;
        format!("{alpha:02X}{}", self.tint.trim_start_matches('#').to_ascii_uppercase())
    }

    fn check(&self, name: &str) -> Result<()> {
        if self.dim > MAX_DIM {
            return Err(Error::invalid(format!("daypart mode {name:?}: dim is 0..={MAX_DIM} (%)")));
        }
        let hex = self.tint.strip_prefix('#').unwrap_or("");
        if hex.len() != 6 || !hex.chars().all(|c| c.is_ascii_hexdigit()) {
            return Err(Error::invalid(format!("daypart mode {name:?}: tint {:?} is not #RRGGBB", self.tint)));
        }
        Ok(())
    }
}

/// The modes every dashboard knows; `modes` in the params adds to them or
/// replaces one by name.
fn builtin_modes() -> BTreeMap<String, Mode> {
    BTreeMap::from([
        ("day".to_owned(), Mode::new(Layout::Full, 0, "#000000")),
        // Darker and a little warmer once the sun is down.
        ("evening".to_owned(), Mode::new(Layout::Full, 20, "#1A0C00")),
        // Only the time, dim: a TV in a dark room shouldn't light it up.
        ("night".to_owned(), Mode::new(Layout::Clock, 65, "#000000")),
    ])
}

fn builtin_rules() -> Vec<RuleSpec> {
    vec![
        RuleSpec { mode: "evening".into(), from: "sunset+30".into(), to: "23:00".into(), days: vec![] },
        RuleSpec { mode: "night".into(), from: "23:00".into(), to: "06:30".into(), days: vec![] },
    ]
}

/// A moment of a day: a clock time or one relative to the sun.
#[derive(Debug, Clone, Copy, PartialEq)]
enum At {
    Clock(i64),
    Sunrise(i64),
    Sunset(i64),
}

impl At {
    fn parse(text: &str) -> Result<Self> {
        let bad = || Error::invalid(format!("daypart time {text:?}: use \"HH:MM\", \"sunrise\", \"sunset\" or e.g. \"sunset+30\""));
        let t = text.trim();
        for (word, make) in [("sunrise", At::Sunrise as fn(i64) -> At), ("sunset", At::Sunset)] {
            if let Some(rest) = t.strip_prefix(word) {
                let minutes: i64 = match rest.trim() {
                    "" => 0,
                    r if r.starts_with('+') || r.starts_with('-') => r.replace(' ', "").parse().map_err(|_| bad())?,
                    _ => return Err(bad()),
                };
                if minutes.abs() > 12 * 60 {
                    return Err(bad());
                }
                return Ok(make(minutes * 60));
            }
        }
        let (h, m) = t.split_once(':').ok_or_else(bad)?;
        let (h, m): (i64, i64) = (h.parse().map_err(|_| bad())?, m.parse().map_err(|_| bad())?);
        if !(0..24).contains(&h) || !(0..60).contains(&m) || t.len() != 5 {
            return Err(bad());
        }
        Ok(At::Clock(h * 3600 + m * 60))
    }

    /// Seconds after `date`'s local midnight; `None` when the sun doesn't
    /// rise or set that day.
    fn resolve(self, date: Date, place: Place) -> Option<i64> {
        match self {
            At::Clock(s) => Some(s),
            At::Sunrise(d) => place.sun(date).map(|(rise, _)| rise + d),
            At::Sunset(d) => place.sun(date).map(|(_, set)| set + d),
        }
    }
}

#[derive(Debug, Clone, Copy)]
struct Place {
    latitude: f64,
    longitude: f64,
    utc_offset: i32,
}

impl Place {
    fn sun(self, date: Date) -> Option<(i64, i64)> {
        sun::rise_and_set(date, self.latitude, self.longitude, self.utc_offset)
    }
}

fn parse_days(days: &[String]) -> Result<u8> {
    let mut mask = 0u8;
    for day in days {
        mask |= match day.to_ascii_lowercase().as_str() {
            "mon" => 1,
            "tue" => 2,
            "wed" => 4,
            "thu" => 8,
            "fri" => 16,
            "sat" => 32,
            "sun" => 64,
            "weekdays" => 31,
            "weekend" => 96,
            other => return Err(Error::invalid(format!("daypart day {other:?}: use mon..sun, weekdays, weekend"))),
        };
    }
    Ok(mask)
}

#[derive(Deserialize, Clone, Debug)]
#[serde(deny_unknown_fields)]
struct RuleSpec {
    mode: String,
    from: String,
    to: String,
    #[serde(default)]
    days: Vec<String>,
}

#[derive(Debug, Clone)]
struct Rule {
    mode: String,
    from: At,
    to: At,
    /// Bit 0 = Monday ... bit 6 = Sunday; 0 = every day.
    days: u8,
}

impl Rule {
    fn on(&self, date: Date) -> bool {
        self.days == 0 || self.days & (1 << date.weekday()) != 0
    }

    /// The window that starts on `date`, as seconds after its midnight;
    /// `to` past midnight runs into the next day.
    fn window(&self, date: Date, place: Place) -> Option<(i64, i64)> {
        if !self.on(date) {
            return None;
        }
        let from = self.from.resolve(date, place)?;
        let mut to = self.to.resolve(date, place)?;
        if to <= from {
            to += DAY;
        }
        Some((from, to))
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Params {
    /// The state file (the override); another daypart widget needs another.
    #[serde(default = "default_name")]
    name: String,
    #[serde(default = "default_latitude")]
    latitude: f64,
    #[serde(default = "default_longitude")]
    longitude: f64,
    /// The mode when no rule holds.
    #[serde(default = "default_mode")]
    default: String,
    /// Added to the built-in modes (day, evening, night), or replacing one.
    #[serde(default)]
    modes: BTreeMap<String, Mode>,
    /// Replaces the built-in rules (evening from sunset+30, night 23:00-06:30).
    #[serde(default)]
    rules: Option<Vec<RuleSpec>>,
    /// Run after the rules, so they win.
    #[serde(default)]
    extra_rules: Vec<RuleSpec>,
}

fn default_name() -> String {
    "mode".into()
}
fn default_latitude() -> f64 {
    48.1437
}
fn default_longitude() -> f64 {
    17.1066
}
fn default_mode() -> String {
    "day".into()
}

#[derive(Serialize, Deserialize, Debug, Clone, Default, PartialEq)]
struct State {
    #[serde(default)]
    forced: Option<Forced>,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
struct Forced {
    mode: String,
    until: u64,
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum Op {
    Set {
        mode: String,
        #[serde(default)]
        minutes: Option<u64>,
    },
    Auto,
}

pub struct Daypart {
    name: String,
    latitude: f64,
    longitude: f64,
    default: String,
    modes: BTreeMap<String, Mode>,
    rules: Vec<Rule>,
    state: Mutex<State>,
}

pub fn build(params: &Value) -> Result<Box<dyn Widget>> {
    Ok(Box::new(Daypart::new(widget::params("daypart", params)?)?))
}

/// What is shown now and until when.
struct Now<'a> {
    name: &'a str,
    mode: &'a Mode,
    forced: bool,
    /// When this mode ends (unix) and which comes next; `None`: never.
    next: Option<(u64, &'a str)>,
}

impl Daypart {
    fn new(p: Params) -> Result<Self> {
        let mut modes = builtin_modes();
        modes.extend(p.modes);
        for (name, mode) in &modes {
            if name.is_empty() || !name.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-') {
                return Err(Error::invalid(format!("daypart mode {name:?}: use a-z, 0-9, - and _")));
            }
            mode.check(name)?;
        }
        let known = |name: &str| {
            if modes.contains_key(name) {
                Ok(())
            } else {
                Err(Error::invalid(format!("daypart: no mode {name:?} (known: {:?})", modes.keys().collect::<Vec<_>>())))
            }
        };
        known(&p.default)?;
        let mut rules = vec![];
        for spec in p.rules.unwrap_or_else(builtin_rules).into_iter().chain(p.extra_rules) {
            known(&spec.mode)?;
            rules.push(Rule { from: At::parse(&spec.from)?, to: At::parse(&spec.to)?, days: parse_days(&spec.days)?, mode: spec.mode });
        }
        if !(-90.0..=90.0).contains(&p.latitude) || !(-180.0..=180.0).contains(&p.longitude) {
            return Err(Error::invalid("daypart: latitude/longitude out of range"));
        }
        Ok(Self {
            name: p.name,
            latitude: p.latitude,
            longitude: p.longitude,
            default: p.default,
            modes,
            rules,
            state: Mutex::new(State::default()),
        })
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|p| p.into_inner())
    }

    fn place(&self, now: &LocalNow) -> Place {
        Place { latitude: self.latitude, longitude: self.longitude, utc_offset: now.utc_offset }
    }

    /// The scheduled mode at `s` seconds after `date`'s midnight (any `s`:
    /// tomorrow is `s` past 86 400).
    fn scheduled(&self, date: Date, s: i64, place: Place) -> &str {
        let day = s.div_euclid(DAY);
        let (date, s) = (date.add_days(day), s - day * DAY);
        let mut mode = self.default.as_str();
        for rule in &self.rules {
            // A window that started today, or yesterday and runs past midnight.
            let holds = [(0, s), (-1, s + DAY)].into_iter().any(|(back, x)| {
                rule.window(date.add_days(back), place).is_some_and(|(from, to)| from <= x && x < to)
            });
            if holds {
                mode = &rule.mode;
            }
        }
        mode
    }

    /// The first moment after `s` (same frame as `scheduled`) when the
    /// scheduled mode changes, within the next two days.
    fn next_change(&self, date: Date, s: i64, place: Place) -> Option<(i64, &str)> {
        let current = self.scheduled(date, s, place);
        let mut edges: Vec<i64> = (-1..=2)
            .flat_map(|day| {
                let start = date.add_days(day);
                self.rules.iter().filter_map(move |r| r.window(start, place)).flat_map(move |(f, t)| [f + day * DAY, t + day * DAY])
            })
            .filter(|&edge| edge > s)
            .collect();
        edges.sort_unstable();
        edges.dedup();
        edges.into_iter().find_map(|edge| {
            let mode = self.scheduled(date, edge, place);
            (mode != current).then_some((edge, mode))
        })
    }

    fn now<'a>(&'a self, now: &LocalNow, state: &'a State) -> Now<'a> {
        let place = self.place(now);
        let s = i64::from(now.seconds_of_day);
        let at_unix = |edge: i64| now.unix.saturating_add_signed(edge - s);
        let forced = state.forced.as_ref().filter(|f| f.until > now.unix);
        if let Some((forced, (name, mode))) = forced.and_then(|f| Some((f, self.modes.get_key_value(f.mode.as_str())?))) {
            let then = s + (forced.until - now.unix) as i64;
            return Now { name, mode, forced: true, next: Some((forced.until, self.scheduled(now.date, then, place))) };
        }
        let name = self.scheduled(now.date, s, place);
        let next = self.next_change(now.date, s, place).map(|(edge, mode)| (at_unix(edge), mode));
        Now { name, mode: &self.modes[name], forced: false, next }
    }

    fn output(&self, ctx: &Ctx, state: &State) -> Output {
        let clock = ctx.clock.now();
        let now = self.now(&clock, state);
        let data = json!({
            "mode": now.name,
            "layout": now.mode.layout,
            "dim": now.mode.dim,
            "tint": now.mode.tint,
            "source": if now.forced { "override" } else { "schedule" },
            "until": now.next.map(|(at, _)| at),
            "next": now.next.map(|(_, mode)| mode),
            "modes": self.modes,
        });
        let mut view = View::new();
        view.insert("name".into(), now.name.to_owned());
        view.insert("layout".into(), now.mode.layout.name().to_owned());
        view.insert("veil".into(), now.mode.veil());
        Output::new(data, view)
    }

    /// How long until the mode can change.
    fn wait(&self, ctx: &Ctx) -> Duration {
        let clock = ctx.clock.now();
        let state = self.lock();
        let until = self.now(&clock, &state).next.map_or(RECHECK_MAX, |(at, _)| at.saturating_sub(clock.unix) as i64);
        // One second late, so the clock is past the edge when it wakes.
        Duration::from_secs((until.clamp(0, RECHECK_MAX) + 1) as u64)
    }
}

impl Widget for Daypart {
    fn kind(&self) -> &'static str {
        "daypart"
    }

    fn initial(&self, ctx: &Ctx) -> Option<Output> {
        let mut state = self.lock();
        *state = ctx.state.load(&self.name).unwrap_or_default();
        if state.forced.as_ref().is_some_and(|f| !self.modes.contains_key(&f.mode)) {
            state.forced = None; // a mode the config no longer has
        }
        Some(self.output(ctx, &state))
    }

    fn next_refresh(&self, ctx: &Ctx) -> Option<Duration> {
        Some(self.wait(ctx))
    }

    fn refresh(&self, ctx: &Ctx) -> Result<Refresh> {
        let state = self.lock();
        // Unchanged output is dropped by the store: no notification.
        Ok(Refresh::Changed(self.output(ctx, &state)))
    }

    fn handle(&self, ctx: &Ctx, input: Value) -> Result<Output> {
        let op: Op = serde_json::from_value(input).map_err(|e| Error::invalid(format!("daypart: {e}")))?;
        let clock = ctx.clock.now();
        let mut state = self.lock();
        let next = match op {
            Op::Auto => State { forced: None },
            Op::Set { mode, minutes } => {
                if !self.modes.contains_key(&mode) {
                    return Err(Error::invalid(format!("daypart: no mode {mode:?}")));
                }
                let until = match minutes {
                    Some(0) => return Err(Error::invalid("daypart: minutes must be at least 1")),
                    Some(m) => clock.unix + m.min(MAX_OVERRIDE_MINUTES) * 60,
                    // Until the schedule next changes (else a day).
                    None => {
                        let free = State::default();
                        self.now(&clock, &free).next.map_or(clock.unix + DAY as u64, |(at, _)| at)
                    }
                };
                State { forced: Some(Forced { mode, until }) }
            }
        };
        ctx.state.save(&self.name, &next)?;
        *state = next;
        Ok(self.output(ctx, &state))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing::{FakeFetch, FixedClock, ctx_with_clock};

    // 2026-11-22 is a Sunday; Bratislava's sunset that day is about 16:05.
    const SUNDAY: Date = Date { year: 2026, month: 11, day: 22 };

    fn hm(h: u32, m: u32) -> u32 {
        h * 3600 + m * 60
    }

    fn ctx(date: Date, seconds: u32) -> Ctx {
        ctx_with_clock(FakeFetch::new(), FixedClock(LocalNow::at(date, seconds, 3600)))
    }

    fn widget(params: Value) -> Daypart {
        Daypart::new(widget::params("daypart", &params).unwrap()).unwrap()
    }

    fn mode_at(w: &Daypart, date: Date, seconds: u32) -> String {
        let ctx = ctx(date, seconds);
        w.initial(&ctx).unwrap().data["mode"].as_str().unwrap().to_owned()
    }

    #[test]
    fn the_default_day_evening_and_night() {
        let w = widget(json!(null));
        assert_eq!(mode_at(&w, SUNDAY, hm(12, 0)), "day");
        assert_eq!(mode_at(&w, SUNDAY, hm(16, 20)), "day"); // sunset + 15
        assert_eq!(mode_at(&w, SUNDAY, hm(16, 45)), "evening");
        assert_eq!(mode_at(&w, SUNDAY, hm(22, 59)), "evening");
        assert_eq!(mode_at(&w, SUNDAY, hm(23, 0)), "night");
        assert_eq!(mode_at(&w, SUNDAY, hm(3, 0)), "night");
        assert_eq!(mode_at(&w, SUNDAY, hm(6, 29)), "night");
        assert_eq!(mode_at(&w, SUNDAY, hm(6, 30)), "day");
    }

    #[test]
    fn the_tv_view_carries_layout_and_veil() {
        let w = widget(json!(null));
        let night = w.initial(&ctx(SUNDAY, hm(1, 0))).unwrap();
        assert_eq!(night.view["name"], "night");
        assert_eq!(night.view["layout"], "clock");
        assert_eq!(night.view["veil"], "A6000000"); // 65 %
        let day = w.initial(&ctx(SUNDAY, hm(10, 0))).unwrap();
        assert_eq!(day.view["layout"], "full");
        assert_eq!(day.view["veil"], "");
        assert_eq!(w.initial(&ctx(SUNDAY, hm(20, 0))).unwrap().view["veil"], "331A0C00");
    }

    #[test]
    fn wakes_exactly_at_the_next_change() {
        let w = widget(json!(null));
        let ctx = ctx(SUNDAY, hm(22, 40));
        w.initial(&ctx);
        assert_eq!(w.next_refresh(&ctx), Some(Duration::from_secs(20 * 60 + 1)));
        let out = w.initial(&ctx).unwrap();
        assert_eq!(out.data["next"], "night");
        assert_eq!(out.data["until"], ctx.clock.now().unix + 20 * 60);
        // Mid-afternoon: the next change is the evening, more than an hour off.
        let ctx = self::ctx(SUNDAY, hm(12, 0));
        assert_eq!(w.next_refresh(&ctx), Some(Duration::from_secs(3601)));
        // Night: the next change is 06:30 tomorrow... capped at an hour.
        assert_eq!(w.initial(&self::ctx(SUNDAY, hm(23, 30))).unwrap().data["next"], "day");
    }

    #[test]
    fn days_pick_the_night_a_window_starts_on() {
        // Late Friday and Saturday nights: 00:30-09:00 after them.
        let w = widget(json!({"extra_rules": [
            {"mode": "day", "from": "23:00", "to": "00:30", "days": ["fri", "sat"]},
            {"mode": "night", "from": "00:30", "to": "09:00", "days": ["sat", "sun"]},
        ]}));
        let friday = Date { year: 2026, month: 11, day: 20 };
        let saturday = friday.add_days(1);
        assert_eq!(mode_at(&w, friday, hm(23, 30)), "day");
        assert_eq!(mode_at(&w, saturday, hm(0, 15)), "day"); // Friday's window, past midnight
        assert_eq!(mode_at(&w, saturday, hm(0, 45)), "night");
        assert_eq!(mode_at(&w, saturday, hm(8, 0)), "night");
        assert_eq!(mode_at(&w, saturday, hm(9, 0)), "day");
        // A weekday keeps the plain schedule.
        let monday = friday.add_days(3);
        assert_eq!(mode_at(&w, monday, hm(23, 30)), "night");
        assert_eq!(mode_at(&w, monday.add_days(1), hm(8, 0)), "day");
    }

    #[test]
    fn an_override_holds_until_the_schedule_changes_or_its_minutes_run_out() {
        let w = widget(json!(null));
        let ctx = ctx(SUNDAY, hm(21, 0));
        w.initial(&ctx);
        let out = w.handle(&ctx, json!({"op": "set", "mode": "night"})).unwrap();
        assert_eq!(out.data["mode"], "night");
        assert_eq!(out.data["source"], "override");
        // Until 23:00, when the schedule goes to night anyway.
        assert_eq!(out.data["until"], ctx.clock.now().unix + 2 * 3600);
        // A restart keeps it (same state dir).
        let again = widget(json!(null));
        assert_eq!(again.initial(&ctx).unwrap().data["mode"], "night");
        let out = w.handle(&ctx, json!({"op": "set", "mode": "day", "minutes": 30})).unwrap();
        assert_eq!((out.data["mode"].as_str(), out.data["next"].as_str()), (Some("day"), Some("evening")));
        assert_eq!(w.next_refresh(&ctx), Some(Duration::from_secs(30 * 60 + 1)));
        let out = w.handle(&ctx, json!({"op": "auto"})).unwrap();
        assert_eq!((out.data["mode"].as_str(), out.data["source"].as_str()), (Some("evening"), Some("schedule")));
    }

    #[test]
    fn bad_config_and_input_are_refused() {
        let bad = |params: Value| widget::params::<Params>("daypart", &params).and_then(Daypart::new).is_err();
        assert!(bad(json!({"rules": [{"mode": "nope", "from": "22:00", "to": "06:00"}]})));
        assert!(bad(json!({"rules": [{"mode": "night", "from": "25:00", "to": "06:00"}]})));
        assert!(bad(json!({"rules": [{"mode": "night", "from": "sunset*2", "to": "06:00"}]})));
        assert!(bad(json!({"rules": [{"mode": "night", "from": "22:00", "to": "06:00", "days": ["someday"]}]})));
        assert!(bad(json!({"modes": {"dark": {"dim": 95}}})));
        assert!(bad(json!({"modes": {"dark": {"tint": "orange"}}})));
        assert!(bad(json!({"modes": {"dark": {"layout": "cinema"}}})));
        assert!(bad(json!({"default": "nope"})));
        let w = widget(json!({"modes": {"movie": {"dim": 40, "tint": "#101020"}}}));
        let ctx = ctx(SUNDAY, hm(12, 0));
        assert!(w.handle(&ctx, json!({"op": "set", "mode": "nope"})).is_err());
        assert!(w.handle(&ctx, json!({"op": "set", "mode": "night", "minutes": 0})).is_err());
        assert!(w.handle(&ctx, json!({"op": "explode"})).is_err());
        assert_eq!(w.handle(&ctx, json!({"op": "set", "mode": "movie"})).unwrap().view["veil"], "66101020");
    }

    #[test]
    fn times_parse() {
        assert_eq!(At::parse("06:30").unwrap(), At::Clock(hm(6, 30) as i64));
        assert_eq!(At::parse("sunset").unwrap(), At::Sunset(0));
        assert_eq!(At::parse("sunset+30").unwrap(), At::Sunset(1800));
        assert_eq!(At::parse("sunrise - 15").unwrap(), At::Sunrise(-900));
        assert!(At::parse("6:30").is_err());
        assert!(At::parse("sunset30").is_err());
    }
}
