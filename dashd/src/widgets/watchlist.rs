//! The movies to watch that are in the Plex library, for the TV.
//!
//! The list is a shared Reminders list on the phone; couchd matches it to
//! its Plex index and pushes only the films the library has, in the list's
//! order, whenever that changes:
//!
//! ```text
//! POST /v1/widgets/<id>  {"op": "set", "items": [{"key": "44330", "title": "The Matrix", "year": 1999,
//!                          "directors": ["Lana Wachowski", "Lilly Wachowski"],
//!                          "ratings": {"imdb": 8.7, "critic": 8.3, "critic_source": "rottentomatoes", ...},
//!                          "poster": "http://127.0.0.1:8765/api/plex/art?...", "watched": false}]}
//! ```
//!
//! Kept across restarts. The TV shows one film at a time on a card (the
//! dashboard rotates through `item1`..`itemN`), each as display strings:
//! title, "1999 · Lana Wachowski, Lilly Wachowski", "IMDb 8.7 · RT 83%"
//! (" · " between parts),
//! and the poster's URL (couchd's artwork cache, which Kodi loads itself).

use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::error::{Error, Result};
use crate::widget::{self, Ctx, Output, View, Widget};
use crate::widgets::board::kodi_text;

/// Films the TV cycles through; more are counted, not shown.
const TV_ITEMS: usize = 10;
const MAX_ITEMS: usize = 200;
const TEXT_CHARS: usize = 120;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Params {
    #[serde(default = "default_name")]
    name: String,
}

fn default_name() -> String {
    "watchlist".into()
}

/// One film, as couchd sends it (fields it adds later are ignored).
#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Default)]
pub struct Movie {
    #[serde(default)]
    pub key: String,
    pub title: String,
    #[serde(default)]
    pub year: Option<u32>,
    #[serde(default)]
    pub directors: Vec<String>,
    #[serde(default)]
    pub ratings: Ratings,
    #[serde(default)]
    pub poster: Option<String>,
    #[serde(default)]
    pub watched: bool,
}

/// Plex's scores, 0-10, and where each comes from.
#[derive(Serialize, Deserialize, Debug, Clone, PartialEq, Default)]
pub struct Ratings {
    #[serde(default)]
    pub imdb: Option<f64>,
    #[serde(default)]
    pub critic: Option<f64>,
    #[serde(default)]
    pub critic_source: Option<String>,
    #[serde(default)]
    pub audience: Option<f64>,
    #[serde(default)]
    pub audience_source: Option<String>,
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum Op {
    Set { items: Vec<Movie> },
}

pub struct Watchlist {
    name: String,
    items: Mutex<Vec<Movie>>,
}

pub fn build(params: &Value) -> Result<Box<dyn Widget>> {
    let p: Params = widget::params("watchlist", params)?;
    Ok(Box::new(Watchlist { name: p.name, items: Mutex::new(vec![]) }))
}

/// "IMDb 8.7 · RT 83%": IMDb's rating, then Rotten Tomatoes' critics; the
/// audience score only when there is neither. At most two.
fn source(s: &Option<String>) -> &str {
    s.as_deref().unwrap_or("")
}

/// The score from `wanted`, whichever of Plex's two slots it is in.
fn from(r: &Ratings, wanted: &str) -> Option<f64> {
    [(r.critic, &r.critic_source), (r.audience, &r.audience_source)]
        .into_iter()
        .find_map(|(value, src)| if source(src) == wanted { value } else { None })
}

pub fn ratings_line(r: &Ratings) -> String {
    let mut parts = vec![];
    if let Some(v) = r.imdb.or_else(|| from(r, "imdb")) {
        parts.push(format!("IMDb {v:.1}"));
    }
    if let (Some(v), "rottentomatoes") = (r.critic, source(&r.critic_source)) {
        parts.push(format!("RT {}%", (v * 10.0).round() as i64));
    }
    if parts.is_empty()
        && let Some(v) = r.audience
    {
        parts.push(match source(&r.audience_source) {
            "rottentomatoes" => format!("Audience {}%", (v * 10.0).round() as i64),
            "themoviedb" => format!("TMDB {v:.1}"),
            _ => format!("{v:.1}/10"),
        });
    }
    parts.join(" · ")
}

fn short(text: &str) -> String {
    let text: String = text.chars().take(TEXT_CHARS).collect();
    kodi_text(&text)
}

impl Watchlist {
    fn lock(&self) -> std::sync::MutexGuard<'_, Vec<Movie>> {
        self.items.lock().unwrap_or_else(|p| p.into_inner())
    }

    fn output(&self, items: &[Movie]) -> Output {
        let shown: Vec<&Movie> = items.iter().take(TV_ITEMS).collect();
        let mut view = View::new();
        view.insert("count".into(), match items.len() {
            0 => String::new(),
            1 => "1 in Plex".into(),
            n => format!("{n} in Plex"),
        });
        view.insert("n".into(), if shown.is_empty() { String::new() } else { shown.len().to_string() });
        for k in 1..=TV_ITEMS {
            let m = shown.get(k - 1);
            let line = m.map_or_else(String::new, |m| {
                let directors = m.directors.iter().take(2).cloned().collect::<Vec<_>>().join(", ");
                let year = m.year.map(|y| y.to_string()).unwrap_or_default();
                short(&[year, directors].into_iter().filter(|s| !s.is_empty()).collect::<Vec<_>>().join(" · "))
            });
            view.insert(format!("item{k}.title"), m.map_or_else(String::new, |m| short(&m.title)));
            view.insert(format!("item{k}.line"), line);
            view.insert(format!("item{k}.ratings"), m.map_or_else(String::new, |m| ratings_line(&m.ratings)));
            view.insert(format!("item{k}.poster"), m.and_then(|m| m.poster.clone()).unwrap_or_default());
        }
        Output::new(json!({"items": items}), view)
    }
}

impl Widget for Watchlist {
    fn kind(&self) -> &'static str {
        "watchlist"
    }

    fn initial(&self, ctx: &Ctx) -> Option<Output> {
        let mut items = self.lock();
        *items = ctx.state.load(&self.name).unwrap_or_default();
        Some(self.output(&items))
    }

    fn handle(&self, ctx: &Ctx, input: Value) -> Result<Output> {
        let Op::Set { items: mut next } =
            serde_json::from_value(input).map_err(|e| Error::invalid(format!("watchlist: {e}")))?;
        next.truncate(MAX_ITEMS);
        next.retain(|m| !m.title.trim().is_empty());
        // Only couchd's own artwork URLs: Kodi loads whatever URL it is given.
        for m in &mut next {
            if m.poster.as_deref().is_some_and(|p| !(p.starts_with("http://127.0.0.1:") || p.starts_with("http://localhost:"))) {
                m.poster = None;
            }
        }
        let mut items = self.lock();
        ctx.state.save(&self.name, &next)?;
        *items = next;
        Ok(self.output(&items))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::clock::Date;
    use crate::testing::{FakeFetch, ctx_at};

    fn ctx() -> Ctx {
        ctx_at(FakeFetch::new(), Date { year: 2026, month: 11, day: 23 })
    }

    fn matrix() -> Value {
        json!({"key": "44330", "title": "The Matrix", "year": 1999,
               "directors": ["Lana Wachowski", "Lilly Wachowski", "Someone Else"],
               "ratings": {"imdb": 8.7, "critic": 8.3, "critic_source": "rottentomatoes",
                           "audience": 8.5, "audience_source": "rottentomatoes"},
               "poster": "http://127.0.0.1:8765/api/plex/art?path=%2Fx&w=200&h=300", "watched": false,
               "something_new": 1})
    }

    #[test]
    fn set_shows_each_film_as_lines_and_survives_restart() {
        let ctx = ctx();
        let w = build(&Value::Null).unwrap();
        let empty = w.initial(&ctx).unwrap();
        assert_eq!((empty.view["count"].as_str(), empty.view["n"].as_str()), ("", ""));
        let out = w
            .handle(&ctx, json!({"op": "set", "items": [matrix(), {"title": "Amélie [2001] 🎬", "year": 2001,
                                 "ratings": {"audience": 8.3, "audience_source": "themoviedb"},
                                 "poster": "https://evil.example/x.jpg"}]}))
            .unwrap();
        assert_eq!(out.view["count"], "2 in Plex");
        assert_eq!(out.view["n"], "2");
        assert_eq!(out.view["item1.title"], "The Matrix");
        assert_eq!(out.view["item1.line"], "1999 · Lana Wachowski, Lilly Wachowski");
        assert_eq!(out.view["item1.ratings"], "IMDb 8.7 · RT 83%");
        assert!(out.view["item1.poster"].starts_with("http://127.0.0.1:8765/api/plex/art"));
        assert_eq!(out.view["item2.title"], "Amélie (2001)", "Kodi markup and emoji cleaned");
        assert_eq!(out.view["item2.ratings"], "TMDB 8.3");
        assert_eq!(out.view["item2.poster"], "", "only couchd's own artwork");
        assert_eq!(out.view["item3.title"], "");

        let again = build(&Value::Null).unwrap();
        assert_eq!(again.initial(&ctx).unwrap().view, out.view);

        let cleared = again.handle(&ctx, json!({"op": "set", "items": []})).unwrap();
        assert_eq!((cleared.view["count"].as_str(), cleared.view["item1.title"].as_str()), ("", ""));
    }

    #[test]
    fn ratings_pick_imdb_and_critics() {
        let r = |v: Value| ratings_line(&serde_json::from_value(v).unwrap());
        assert_eq!(r(json!({"critic": 8.3, "critic_source": "rottentomatoes"})), "RT 83%");
        assert_eq!(r(json!({"audience": 7.1, "audience_source": "imdb"})), "IMDb 7.1");
        assert_eq!(r(json!({"audience": 8.5, "audience_source": "rottentomatoes"})), "Audience 85%");
        assert_eq!(r(json!({"audience": 6.0})), "6.0/10");
        assert_eq!(r(json!({})), "");
    }

    #[test]
    fn bad_input_is_refused() {
        let ctx = ctx();
        let w = build(&Value::Null).unwrap();
        w.initial(&ctx);
        assert!(w.handle(&ctx, json!({"op": "add", "title": "x"})).is_err());
        assert!(w.handle(&ctx, json!({"items": []})).is_err());
        assert!(build(&json!({"rows": 3})).is_err());
    }
}
