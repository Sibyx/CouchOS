//! A message and a TODO list on the TV, written from the phone app.
//!
//! ```text
//! POST /v1/widgets/<id>
//!   {"op": "message", "text": "Dinner at 7?", "from": "Jakub"}   ("text": "" clears it)
//!   {"op": "add", "text": "Buy milk"}
//!   {"op": "done", "id": 3}            ("done": false to reopen)
//!   {"op": "edit", "id": 3, "text": "Buy oat milk"}
//!   {"op": "remove", "id": 3}
//!   {"op": "clear_done"}
//! ```
//!
//! Every answer is the whole board. Done items stay on the TV, dimmed, for
//! `keep_done_hours`, then only the app lists them. A message can expire
//! after `message_hours` (0: it stays until replaced).

use std::sync::Mutex;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::error::{Error, Result};
use crate::widget::{self, Ctx, Output, Refresh, View, Widget};

const MESSAGE_CHARS: usize = 280;
/// Up to this long, the TV's message banner uses its big type (two lines
/// of font37 across 1144 px); longer ones the smaller one (four lines).
const MESSAGE_BIG_CHARS: usize = 110;
const ITEM_CHARS: usize = 80;
const MAX_ITEMS: usize = 40;
/// Rows the TV has.
const TV_ROWS: usize = 6;
/// Expiry is checked this often (no I/O, and unchanged output is dropped).
const RECHECK: Duration = Duration::from_secs(10 * 60);

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Params {
    /// The file it is kept in; another board needs another name.
    #[serde(default = "default_name")]
    name: String,
    #[serde(default = "default_rows")]
    rows: usize,
    #[serde(default = "default_keep_done")]
    keep_done_hours: u64,
    #[serde(default)]
    message_hours: u64,
}

fn default_name() -> String {
    "board".into()
}
fn default_rows() -> usize {
    TV_ROWS
}
fn default_keep_done() -> u64 {
    12
}

#[derive(Serialize, Deserialize, Debug, Clone, Default, PartialEq)]
pub struct State {
    pub message: Option<Message>,
    pub items: Vec<Item>,
    pub next_id: u64,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
pub struct Message {
    pub text: String,
    #[serde(default)]
    pub from: Option<String>,
    pub at: u64,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
pub struct Item {
    pub id: u64,
    pub text: String,
    pub done: bool,
    pub added_at: u64,
    #[serde(default)]
    pub done_at: Option<u64>,
}

#[derive(Deserialize)]
#[serde(tag = "op", rename_all = "snake_case", deny_unknown_fields)]
enum Op {
    Message {
        text: String,
        #[serde(default)]
        from: Option<String>,
    },
    Add {
        text: String,
    },
    Done {
        id: u64,
        #[serde(default = "yes")]
        done: bool,
    },
    Edit {
        id: u64,
        text: String,
    },
    Remove {
        id: u64,
    },
    ClearDone,
}

fn yes() -> bool {
    true
}

pub struct Board {
    name: String,
    rows: usize,
    keep_done: u64,
    message_for: u64,
    state: Mutex<State>,
}

pub fn build(params: &Value) -> Result<Box<dyn Widget>> {
    let p: Params = widget::params("board", params)?;
    Ok(Box::new(Board {
        name: p.name,
        rows: p.rows.clamp(1, TV_ROWS),
        keep_done: p.keep_done_hours * 3600,
        message_for: p.message_hours * 3600,
        state: Mutex::new(State::default()),
    }))
}

/// One line of user text, trimmed and limited.
fn clean(text: &str, max: usize, what: &str) -> Result<String> {
    let text = text.trim();
    if text.chars().count() > max {
        return Err(Error::invalid(format!("{what}: at most {max} characters")));
    }
    Ok(text.to_owned())
}

/// Kodi label markup is [B], [COLOR ...], [CR]: user text must not be read
/// as markup, and a line break becomes Kodi's own. Kodi's fonts have no
/// emoji (they would show as boxes), so those go; the app keeps them.
fn kodi_text(text: &str) -> String {
    let plain: String = text
        .chars()
        .filter(|&c| !(c as u32 >= 0x1_F000 || ('\u{2600}'..='\u{27BF}').contains(&c) || c == '\u{FE0F}' || c == '\u{200D}'))
        .collect();
    let lines: Vec<String> = plain
        .lines()
        .map(|l| l.split_whitespace().collect::<Vec<_>>().join(" ").replace('[', "(").replace(']', ")"))
        .collect();
    lines.join("[CR]")
}

impl State {
    fn item(&mut self, id: u64) -> Result<&mut Item> {
        self.items.iter_mut().find(|i| i.id == id).ok_or_else(|| Error::not_found(format!("no item {id}")))
    }

    fn apply(&mut self, op: Op, now: u64) -> Result<()> {
        match op {
            Op::Message { text, from } => {
                let text = clean(&text, MESSAGE_CHARS, "message")?;
                let from = from.map(|f| clean(&f, 40, "from")).transpose()?.filter(|f| !f.is_empty());
                self.message = (!text.is_empty()).then_some(Message { text, from, at: now });
            }
            Op::Add { text } => {
                let text = clean(&text, ITEM_CHARS, "item")?;
                if text.is_empty() {
                    return Err(Error::invalid("item: empty"));
                }
                if self.items.len() >= MAX_ITEMS {
                    return Err(Error::invalid(format!("at most {MAX_ITEMS} items: remove some first")));
                }
                self.next_id += 1;
                self.items.push(Item { id: self.next_id, text, done: false, added_at: now, done_at: None });
            }
            Op::Done { id, done } => {
                let item = self.item(id)?;
                if item.done != done {
                    item.done = done;
                    item.done_at = done.then_some(now);
                }
            }
            Op::Edit { id, text } => {
                let text = clean(&text, ITEM_CHARS, "item")?;
                if text.is_empty() {
                    return Err(Error::invalid("item: empty"));
                }
                self.item(id)?.text = text;
            }
            Op::Remove { id } => {
                self.item(id)?;
                self.items.retain(|i| i.id != id);
            }
            Op::ClearDone => self.items.retain(|i| !i.done),
        }
        Ok(())
    }
}

impl Board {
    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|p| p.into_inner())
    }

    /// The board as shown at `now`: open items first, then recently done.
    fn output(&self, state: &State, now: u64) -> Output {
        let message = state.message.as_ref().filter(|m| self.message_for == 0 || now < m.at + self.message_for);
        let shown_done = |i: &&Item| i.done && i.done_at.is_some_and(|t| now < t + self.keep_done);
        let open: Vec<&Item> = state.items.iter().filter(|i| !i.done).collect();
        let visible: Vec<&Item> = open.iter().copied().chain(state.items.iter().filter(shown_done)).collect();

        let mut view = View::new();
        // The TV shows the message as a banner, without its author ("from"
        // is still accepted and kept in `data`, for apps that send it).
        // Long ones get the banner's smaller type.
        view.insert("message".into(), message.map_or_else(String::new, |m| kodi_text(&m.text)));
        view.insert(
            "message_long".into(),
            if message.is_some_and(|m| m.text.chars().count() > MESSAGE_BIG_CHARS) { "true".into() } else { String::new() },
        );
        view.insert("items".into(), if visible.is_empty() { String::new() } else { "true".into() });
        view.insert(
            "count".into(),
            match open.len() {
                0 if !visible.is_empty() => "All done!".into(),
                0 => String::new(),
                1 => "1 to do".into(),
                n => format!("{n} to do"),
            },
        );
        for k in 1..=TV_ROWS {
            let item = visible.get(k - 1).filter(|_| k <= self.rows);
            view.insert(format!("item{k}.text"), item.map_or_else(String::new, |i| kodi_text(&i.text)));
            view.insert(format!("item{k}.done"), if item.is_some_and(|i| i.done) { "true".into() } else { String::new() });
        }
        let hidden = visible.len().saturating_sub(self.rows);
        view.insert("more".into(), if hidden > 0 { format!("+ {hidden} more") } else { String::new() });

        let data = json!({
            "message": message,
            "items": state.items,
            "shown": visible.iter().take(self.rows).map(|i| i.id).collect::<Vec<_>>(),
        });
        Output::new(data, view)
    }
}

impl Widget for Board {
    fn kind(&self) -> &'static str {
        "board"
    }

    fn initial(&self, ctx: &Ctx) -> Option<Output> {
        let mut state = self.lock();
        *state = ctx.state.load(&self.name).unwrap_or_default();
        Some(self.output(&state, ctx.clock.now().unix))
    }

    /// Done items and messages age out; nothing else changes on its own.
    fn next_refresh(&self, _ctx: &Ctx) -> Option<Duration> {
        Some(RECHECK)
    }

    fn refresh(&self, ctx: &Ctx) -> Result<Refresh> {
        let state = self.lock();
        Ok(Refresh::Changed(self.output(&state, ctx.clock.now().unix)))
    }

    fn handle(&self, ctx: &Ctx, input: Value) -> Result<Output> {
        let op: Op = serde_json::from_value(input).map_err(|e| Error::invalid(format!("board: {e}")))?;
        let now = ctx.clock.now().unix;
        let mut state = self.lock();
        let mut next = state.clone();
        next.apply(op, now)?;
        ctx.state.save(&self.name, &next)?;
        *state = next;
        Ok(self.output(&state, now))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::clock::Date;
    use crate::testing::{FakeFetch, ctx_at};

    fn ctx() -> Ctx {
        ctx_at(FakeFetch::new(), Date { year: 2026, month: 11, day: 22 })
    }

    #[test]
    fn message_and_list_round_trip_and_survive_restart() {
        let ctx = ctx();
        let board = build(&json!({"rows": 3})).unwrap();
        board.initial(&ctx);
        let out = board.handle(&ctx, json!({"op": "message", "text": "Dinner at [7]? 🍄\nLove you ❤️", "from": "Jakub"})).unwrap();
        assert_eq!(out.view["message"], "Dinner at (7)?[CR]Love you");
        assert_eq!(out.data["message"]["text"], "Dinner at [7]? 🍄\nLove you ❤️");
        assert_eq!(kodi_text("typed [CR] and (CR)"), "typed (CR) and (CR)");
        assert!(!out.view.contains_key("from"), "the TV no longer shows the author");
        assert_eq!(out.data["message"]["from"], "Jakub");
        assert_eq!(out.view["message_long"], "");
        let long = board.handle(&ctx, json!({"op": "message", "text": "x".repeat(MESSAGE_BIG_CHARS + 1)})).unwrap();
        assert_eq!(long.view["message_long"], "true");
        board.handle(&ctx, json!({"op": "message", "text": "Dinner at [7]? 🍄\nLove you ❤️", "from": "Jakub"})).unwrap();
        for t in ["Milk", "Bread", "Eggs", "Coffee"] {
            board.handle(&ctx, json!({"op": "add", "text": t})).unwrap();
        }
        let out = board.handle(&ctx, json!({"op": "done", "id": 1})).unwrap();
        // open first, done after; three rows, one hidden
        assert_eq!(out.view["item1.text"], "Bread");
        assert_eq!(out.view["item3.text"], "Coffee");
        assert_eq!(out.view["item4.text"], "");
        assert_eq!(out.view["more"], "+ 1 more");
        assert_eq!(out.view["count"], "3 to do");

        let again = build(&json!({"rows": 3})).unwrap();
        let restored = again.initial(&ctx).unwrap();
        assert_eq!(restored.view, out.view);
        assert_eq!(restored.data["items"][0]["done"], true);

        let out = again.handle(&ctx, json!({"op": "message", "text": "  "})).unwrap();
        assert_eq!(out.view["message"], "");
        assert!(out.data["message"].is_null());
    }

    #[test]
    fn done_items_fade_out_of_the_tv() {
        let board = Board { name: "b".into(), rows: 6, keep_done: 3600, message_for: 600, state: Mutex::new(State::default()) };
        let mut state = State::default();
        state.apply(Op::Add { text: "Milk".into() }, 0).unwrap();
        state.apply(Op::Done { id: 1, done: true }, 100).unwrap();
        state.apply(Op::Message { text: "Hi".into(), from: None }, 100).unwrap();
        let fresh = board.output(&state, 200);
        assert_eq!(fresh.view["item1.done"], "true");
        assert_eq!(fresh.view["count"], "All done!");
        assert_eq!(fresh.view["message"], "Hi");
        let later = board.output(&state, 100 + 3600);
        assert_eq!(later.view["item1.text"], "");
        assert_eq!(later.view["items"], "");
        assert_eq!(later.view["message"], "");
        // the app still has it
        assert_eq!(later.data["items"][0]["text"], "Milk");
    }

    #[test]
    fn bad_input_is_refused_and_changes_nothing() {
        let ctx = ctx();
        let board = build(&Value::Null).unwrap();
        board.initial(&ctx);
        assert!(board.handle(&ctx, json!({"op": "add", "text": ""})).is_err());
        assert!(board.handle(&ctx, json!({"op": "add", "text": "x".repeat(81)})).is_err());
        assert_eq!(board.handle(&ctx, json!({"op": "done", "id": 9})).unwrap_err().http_status(), 404);
        assert!(board.handle(&ctx, json!({"op": "explode"})).is_err());
        let out = board.handle(&ctx, json!({"op": "add", "text": "Milk"})).unwrap();
        assert_eq!(out.data["items"][0]["id"], 1);
        board.handle(&ctx, json!({"op": "edit", "id": 1, "text": "Oat milk"})).unwrap();
        board.handle(&ctx, json!({"op": "done", "id": 1})).unwrap();
        let out = board.handle(&ctx, json!({"op": "clear_done"})).unwrap();
        assert_eq!(out.data["items"], json!([]));
    }
}
