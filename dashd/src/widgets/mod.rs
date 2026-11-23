//! Built-in widget kinds. A new kind is a module plus one line in [`KINDS`].

pub mod board;
pub mod clock;
pub mod daypart;
pub mod frog;
pub mod qr;
pub mod watchlist;
pub mod weather;

use serde_json::Value;

use crate::error::{Error, Result};
use crate::widget::{Factory, Widget};

pub const KINDS: &[(&str, Factory)] = &[
    ("clock", clock::build),
    ("weather", weather::build),
    ("frog", frog::build),
    ("board", board::build),
    ("qr", qr::build),
    ("daypart", daypart::build),
    ("watchlist", watchlist::build),
];

pub fn build(kind: &str, params: &Value) -> Result<Box<dyn Widget>> {
    let (_, factory) = KINDS
        .iter()
        .find(|(k, _)| *k == kind)
        .ok_or_else(|| Error::invalid(format!("unknown widget kind {kind:?} (known: {:?})", KINDS.iter().map(|k| k.0).collect::<Vec<_>>())))?;
    factory(params)
}

/// The TV skin and dashd must agree on every key: a skin binding a key
/// dashd does not serve shows an empty slot, silently (it happened: the
/// daily forecast went blank). This reads the real skin file.
#[cfg(test)]
mod skin_contract {
    use std::collections::BTreeSet;

    use serde_json::json;

    use crate::clock::Date;
    use crate::testing::{FakeFetch, ctx_at};
    use crate::widget::Refresh;

    const SKIN: &str = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../ansible/roles/kodi/files/screensaver.couchos.dashboard/resources/skins/default/1080i/screensaver-couchos-dashboard.xml"
    );

    fn skin_keys() -> BTreeSet<String> {
        let xml = std::fs::read_to_string(SKIN).unwrap();
        xml.split("Window.Property(")
            .skip(1)
            .filter_map(|rest| rest.split(')').next())
            .filter(|key| !key.contains('<')) // the header comment's "<widget id>.<key>"
            .map(str::to_owned)
            .collect()
    }

    #[test]
    fn every_key_the_skin_binds_is_served() {
        let fake = FakeFetch::new()
            .json("open-meteo", serde_json::from_str(super::weather::SAMPLE).unwrap())
            .json("/taxa?", json!({"total_results": 1, "results": [{"id": 1, "conservation_status": {"status_name": "endangered"}}]}))
            .json("/taxa/1", json!({"results": [{
                "name": "Rana", "preferred_common_name": "Frog", "wikipedia_summary": "A frog.",
                "ancestors": [{"rank": "family", "name": "Ranidae"}],
                "default_photo": {"license_code": "cc-by", "medium_url": "https://p/1/medium.jpg", "attribution": "(c) A"}
            }]}))
            .bytes("/1/large.jpg", b"JPEG");
        let ctx = ctx_at(fake, Date { year: 2026, month: 11, day: 22 });
        // Set by the screensaver itself, not by dashd: "offline" when dashd
        // does not answer, "slide" for the carousel, "board.cards" for how
        // many bottom-row slots the board's cards take, and the film the
        // watch list card features now (copied from watchlist.item<N>.*).
        let mut served: BTreeSet<String> = [
            "offline",
            "slide",
            "board.cards",
            "watchlist.title",
            "watchlist.line",
            "watchlist.ratings",
            "watchlist.poster",
        ]
        .map(str::to_owned)
        .into();
        let widgets = [
            ("clock", "clock", json!(null)),
            ("weather", "weather", json!({"place": "Here", "latitude": 1, "longitude": 2, "hours_step": 1, "hours_count": 6})),
            ("frog", "frog", json!(null)),
            ("board", "board", json!(null)),
            ("app", "qr", json!({"url": "https://example.com"})),
            ("mode", "daypart", json!(null)),
            ("watchlist", "watchlist", json!(null)),
        ];
        for (id, kind, params) in widgets {
            let w = super::build(kind, &params).unwrap();
            let mut views = vec![];
            views.extend(w.initial(&ctx).map(|o| o.view));
            if let Ok(Refresh::Changed(o)) = w.refresh(&ctx) {
                views.push(o.view);
            }
            served.insert(format!("{id}.stale"));
            for view in views {
                served.extend(view.keys().map(|k| format!("{id}.{k}")));
            }
        }
        let missing: Vec<_> = skin_keys().difference(&served).cloned().collect();
        assert!(missing.is_empty(), "the skin binds keys dashd does not serve: {missing:?}");
        assert!(skin_keys().len() > 40);
    }
}
