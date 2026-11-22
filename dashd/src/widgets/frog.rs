//! A new frog every day from iNaturalist's taxonomy: one of ~7,900 species of
//! Anura that have photos, with its Latin name, family, a Wikipedia summary
//! and a Creative Commons photo (attribution shown, as the licence requires).
//!
//! The day's pick is a hash of the date, so it is the same frog whenever
//! dashd restarts, and is saved with its photo, so a restart costs no
//! requests. Three API calls a day, far inside iNaturalist's limits.

use std::sync::Mutex;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::clock::Date;
use crate::error::{Error, Result};
use crate::view;
use crate::widget::{self, Ctx, Output, Refresh, Widget};

const API: &str = "https://api.inaturalist.org/v1/taxa";
/// Anura (frogs and toads) in iNaturalist's taxonomy.
const ANURA: u64 = 20979;
/// iNaturalist pages no further than page × per_page = 10,000.
const MAX_INDEX: u64 = 10_000;
const PHOTO_LIMIT: u64 = 4 * 1024 * 1024;
const SUMMARY_CHARS: usize = 300;
/// Species tried when one has no openly licensed photo.
const CANDIDATES: u64 = 4;
const STATE_KEY: &str = "frog";
const ASSET_PREFIX: &str = "frog-";

#[derive(Deserialize)]
struct Params {
    /// Another taxon (a family, the whole Amphibia) for a different daily pick.
    #[serde(default)]
    taxon_id: Option<u64>,
}

pub struct Frog {
    taxon_id: u64,
    today: Mutex<Option<Pick>>,
}

pub fn build(params: &Value) -> Result<Box<dyn Widget>> {
    let p: Params = widget::params("frog", params)?;
    Ok(Box::new(Frog { taxon_id: p.taxon_id.unwrap_or(ANURA), today: Mutex::new(None) }))
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
pub struct Pick {
    pub date: String,
    pub taxon_id: u64,
    pub name: String,
    pub common_name: Option<String>,
    pub family: Option<String>,
    pub summary: String,
    pub conservation: Option<String>,
    pub observations: u64,
    pub wikipedia_url: Option<String>,
    pub photo: Photo,
}

#[derive(Serialize, Deserialize, Debug, Clone, PartialEq)]
pub struct Photo {
    /// Served by dashd: /v1/assets/<asset>. One name per day, so clients'
    /// image caches (Kodi's is keyed by URL) never show yesterday's frog.
    pub asset: String,
    pub attribution: String,
    pub license: String,
}

impl Pick {
    fn output(&self) -> Output {
        let view = view! {
            "name" => self.name,
            "common" => self.common_name.as_deref().unwrap_or(""),
            "family" => self.family.as_deref().unwrap_or(""),
            "summary" => tv_summary(&self.summary, TV_SUMMARY_CHARS),
            "status" => self.conservation.as_deref().map_or_else(String::new, status),
            "photo" => format!("/v1/assets/{}", self.photo.asset),
            "attribution" => tv_attribution(&self.photo.attribution),
        };
        let mut data = json!(self);
        data["photo"]["url"] = json!(format!("/v1/assets/{}", self.photo.asset));
        Output::new(data, view)
    }
}

fn capitalize(s: &str) -> String {
    let mut chars = s.chars();
    chars.next().map_or_else(String::new, |c| c.to_uppercase().chain(chars).collect())
}

/// The IUCN status in its Red List colour (Kodi label markup): a glance says
/// "this one is in trouble" before the words do.
fn status(name: &str) -> String {
    let colour = match name.to_ascii_lowercase().as_str() {
        "critically endangered" | "extinct in the wild" | "extinct" => "FFE57373",
        "endangered" => "FFF0A15E",
        "vulnerable" => "FFE8CF6A",
        "near threatened" => "FFC5D86D",
        "least concern" => "FF8FBF7A",
        _ => "FFA0A0A0",
    };
    format!("[COLOR {colour}]{}[/COLOR]", capitalize(name))
}

/// "(c) STRI, some rights reserved (CC BY-NC)" → "Photo © STRI · CC BY-NC ·
/// iNaturalist": the same credit, short enough for one line on the TV.
fn tv_attribution(attribution: &str) -> String {
    let a = attribution
        .replace("(c)", "©")
        .replace(", some rights reserved", "")
        .replace(", all rights reserved", "")
        .replace(", no rights reserved", "");
    let a = match (a.rfind(" ("), a.ends_with(')')) {
        (Some(i), true) => format!("{} · {}", &a[..i], &a[i + 2..a.len() - 1]),
        _ => a,
    };
    format!("Photo {a}  ·  iNaturalist")
}

/// Room for three lines on the TV.
const TV_SUMMARY_CHARS: usize = 170;

/// The summary for the TV: without the opening "X is a species of frog in
/// the family Y." (the name and family are already on screen), and only
/// whole sentences that fit in `max` characters.
fn tv_summary(summary: &str, max: usize) -> String {
    let mut sentences: Vec<&str> = summary.split_inclusive(". ").map(str::trim).filter(|s| !s.is_empty()).collect();
    let boilerplate = |s: &str| s.contains(" is a species of ") || s.contains(" is a genus of ");
    if sentences.len() > 1 && boilerplate(sentences[0]) {
        sentences.remove(0);
    }
    let mut out = String::new();
    for s in sentences {
        let len = out.chars().count() + s.chars().count() + 1;
        if len > max && !out.is_empty() {
            break;
        }
        if !out.is_empty() {
            out.push(' ');
        }
        out.push_str(s);
    }
    if out.chars().count() <= max {
        return out;
    }
    // One long sentence: cut at a word.
    let cut: String = out.chars().take(max).collect();
    let end = cut.rfind(' ').unwrap_or(cut.len());
    format!("{}…", cut[..end].trim_end_matches([',', ';', ' ']))
}

/// splitmix64: a well-spread index from a day number.
fn mix(mut x: u64) -> u64 {
    x = x.wrapping_add(0x9E37_79B9_7F4A_7C15);
    x = (x ^ (x >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    x = (x ^ (x >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    x ^ (x >> 31)
}

/// Candidate positions (0-based) in the species list for `date`.
fn candidates(date: Date, total: u64) -> impl Iterator<Item = u64> {
    let day = date.days_since_epoch() as u64;
    (0..CANDIDATES).map(move |k| mix(day.wrapping_mul(CANDIDATES).wrapping_add(k)) % total.max(1))
}

/// Wikipedia's summary as plain text, cut at a sentence near `max` chars.
fn plain_summary(html: &str, max: usize) -> String {
    let mut text = String::with_capacity(html.len());
    let mut in_tag = false;
    for c in html.chars() {
        match c {
            '<' => in_tag = true,
            '>' => in_tag = false,
            _ if !in_tag => text.push(c),
            _ => {}
        }
    }
    let text = text.replace("&amp;", "&").replace("&quot;", "\"").replace("&#39;", "'").replace("&nbsp;", " ");
    let text = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if text.chars().count() <= max {
        return text;
    }
    let cut: String = text.chars().take(max).collect();
    match cut.rfind(". ") {
        Some(end) if end > max / 3 => cut[..=end].to_owned(),
        _ => format!("{}…", cut.trim_end()),
    }
}

/// A photo with a licence; iNaturalist's default first.
fn licensed_photo(detail: &Value) -> Option<(String, String, String)> {
    let photos = std::iter::once(&detail["default_photo"])
        .chain(detail["taxon_photos"].as_array().into_iter().flatten().map(|tp| &tp["photo"]));
    photos.filter_map(|p| {
        let license = p["license_code"].as_str().filter(|l| !l.is_empty())?;
        let base = p["medium_url"].as_str().or_else(|| p["url"].as_str())?;
        let large = base.replace("/medium.", "/large.").replace("/square.", "/large.");
        Some((large, p["attribution"].as_str().unwrap_or("").to_owned(), license.to_uppercase()))
    }).next()
}

impl Frog {
    fn list_url(&self, page: u64) -> String {
        format!(
            "{API}?taxon_id={}&rank=species&is_active=true&photos=true&order_by=id&order=asc&per_page=1&page={page}",
            self.taxon_id
        )
    }

    fn fetch_pick(&self, ctx: &Ctx, date: Date) -> Result<Pick> {
        let total = ctx.http.get_json(&self.list_url(1))?["total_results"]
            .as_u64()
            .filter(|&n| n > 0)
            .ok_or_else(|| Error::upstream("inaturalist: no species"))?
            .min(MAX_INDEX);
        let mut last_error = Error::upstream("inaturalist: no species with a licensed photo");
        for index in candidates(date, total) {
            match self.try_species(ctx, date, index) {
                Ok(pick) => return Ok(pick),
                Err(err) => last_error = err,
            }
        }
        Err(last_error)
    }

    fn try_species(&self, ctx: &Ctx, date: Date, index: u64) -> Result<Pick> {
        let listed = ctx.http.get_json(&self.list_url(index + 1))?;
        let taxon = &listed["results"][0];
        let id = taxon["id"].as_u64().ok_or_else(|| Error::upstream("inaturalist: no taxon id"))?;
        let detail_page = ctx.http.get_json(&format!("{API}/{id}"))?;
        let detail = &detail_page["results"][0];
        let (url, attribution, license) =
            licensed_photo(detail).ok_or_else(|| Error::upstream(format!("taxon {id}: no licensed photo")))?;

        let asset = format!("{ASSET_PREFIX}{date}.jpg");
        let bytes = ctx.http.get_bytes(&url, PHOTO_LIMIT)?;
        ctx.assets.put(&asset, &bytes)?;
        ctx.assets.prune(ASSET_PREFIX, &asset)?;

        let family = detail["ancestors"]
            .as_array()
            .and_then(|a| a.iter().find(|t| t["rank"] == "family"))
            .and_then(|t| t["name"].as_str())
            .map(str::to_owned);
        let text = |v: &Value| v.as_str().filter(|s| !s.is_empty()).map(str::to_owned);
        Ok(Pick {
            date: date.to_string(),
            taxon_id: id,
            name: text(&detail["name"]).unwrap_or_default(),
            common_name: text(&detail["preferred_common_name"]),
            family,
            summary: plain_summary(detail["wikipedia_summary"].as_str().unwrap_or(""), SUMMARY_CHARS),
            conservation: text(&taxon["conservation_status"]["status_name"]),
            observations: detail["observations_count"].as_u64().or(taxon["observations_count"].as_u64()).unwrap_or(0),
            wikipedia_url: text(&detail["wikipedia_url"]).map(|u| u.replace(' ', "_")),
            photo: Photo { asset, attribution, license },
        })
    }

    fn is_current(&self, ctx: &Ctx, date: Date) -> bool {
        let today = self.today.lock().unwrap_or_else(|p| p.into_inner());
        today.as_ref().is_some_and(|p| p.date == date.to_string() && ctx.assets.exists(&p.photo.asset))
    }
}

impl Widget for Frog {
    fn kind(&self) -> &'static str {
        "frog"
    }

    /// The saved frog. Usually today's; after a restart past midnight it is
    /// yesterday's until the fetch succeeds, which beats an empty panel when
    /// the network is down.
    fn initial(&self, ctx: &Ctx) -> Option<Output> {
        let saved: Pick = ctx.state.load(STATE_KEY)?;
        let output = ctx.assets.exists(&saved.photo.asset).then(|| saved.output());
        *self.today.lock().unwrap_or_else(|p| p.into_inner()) = Some(saved);
        output
    }

    fn next_refresh(&self, ctx: &Ctx) -> Option<Duration> {
        let now = ctx.clock.now();
        if self.is_current(ctx, now.date) {
            // A minute past midnight.
            Some(Duration::from_secs(now.seconds_to_midnight + 60))
        } else {
            Some(Duration::ZERO)
        }
    }

    fn refresh(&self, ctx: &Ctx) -> Result<Refresh> {
        let date = ctx.clock.now().date;
        if self.is_current(ctx, date) {
            return Ok(Refresh::Unchanged);
        }
        let pick = self.fetch_pick(ctx, date)?;
        ctx.state.save(STATE_KEY, &pick)?;
        let output = pick.output();
        *self.today.lock().unwrap_or_else(|p| p.into_inner()) = Some(pick);
        Ok(Refresh::Changed(output))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing::{FakeFetch, ctx_at};

    #[test]
    fn same_day_same_candidates_and_they_differ_across_days() {
        let day = |d| Date { year: 2026, month: 11, day: d };
        let a: Vec<_> = candidates(day(22), 7940).collect();
        assert_eq!(a, candidates(day(22), 7940).collect::<Vec<_>>());
        assert_ne!(a[0], candidates(day(23), 7940).next().unwrap());
        assert!(a.iter().all(|&i| i < 7940));
    }

    #[test]
    fn summary_is_plain_and_short() {
        let html = "<b>Atelopus patazensis</b> is a species of toad &amp; frog. It is endemic to Peru. ".repeat(10);
        let s = plain_summary(&html, 120);
        assert!(s.starts_with("Atelopus patazensis is a species of toad & frog."));
        assert!(s.ends_with('.'));
        assert!(s.chars().count() <= 120);
    }

    #[test]
    fn tv_summary_drops_the_obvious_and_fits() {
        let s = "Isthmohyla calypsa is a species of frog in the family Hylidae. It is known from the southern \
                 Cordillera de Talamanca in Costa Rica, Cerro Pando in Costa Rica and Panama, and the Pacific slope \
                 in southwestern Panama. It appears to have gone extinct in Costa Rica.";
        let tv = tv_summary(s, TV_SUMMARY_CHARS);
        assert!(tv.starts_with("It is known from"), "{tv}");
        assert!(tv.chars().count() <= TV_SUMMARY_CHARS, "{tv}");
        // A lone species sentence is all there is: keep it.
        assert_eq!(tv_summary("X is a species of frog.", 170), "X is a species of frog.");
        let long = "word ".repeat(60);
        let cut = tv_summary(long.trim(), 50);
        assert!(cut.ends_with('…') && cut.chars().count() <= 51, "{cut}");
    }

    #[test]
    fn attribution_fits_one_line() {
        assert_eq!(
            tv_attribution("(c) STRI, some rights reserved (CC BY-NC)"),
            "Photo © STRI · CC BY-NC  ·  iNaturalist"
        );
        assert_eq!(tv_attribution("(c) Ann"), "Photo © Ann  ·  iNaturalist");
    }

    #[test]
    fn status_is_coloured() {
        assert_eq!(status("critically endangered"), "[COLOR FFE57373]Critically endangered[/COLOR]");
    }

    #[test]
    fn skips_unlicensed_photos() {
        let detail = json!({
            "default_photo": {"license_code": null, "medium_url": "https://x/1/medium.jpg"},
            "taxon_photos": [{"photo": {"license_code": "cc-by", "url": "https://x/2/square.jpg", "attribution": "(c) Ann"}}]
        });
        let (url, by, license) = licensed_photo(&detail).unwrap();
        assert_eq!(url, "https://x/2/large.jpg");
        assert_eq!(by, "(c) Ann");
        assert_eq!(license, "CC-BY");
    }

    #[test]
    fn fetches_once_a_day_and_survives_restart() {
        let fake = FakeFetch::new()
            .json("/taxa?", json!({"total_results": 1, "results": [{"id": 70470}]}))
            .json("/taxa/70470", json!({"results": [{
                "name": "Atelopus patazensis", "preferred_common_name": "Pataz Stubfoot Toad",
                "wikipedia_summary": "<b>Atelopus patazensis</b> is a toad.", "observations_count": 1,
                "ancestors": [{"rank": "family", "name": "Bufonidae"}],
                "default_photo": {"license_code": "cc-by-nc", "medium_url": "https://p/1/medium.jpg", "attribution": "(c) acatenazzi"}
            }]}))
            .bytes("/1/large.jpg", b"JPEG");
        let ctx = ctx_at(fake, Date { year: 2026, month: 11, day: 22 });
        let frog = build(&Value::Null).unwrap();

        let Refresh::Changed(out) = frog.refresh(&ctx).unwrap() else { panic!("expected a frog") };
        assert_eq!(out.view["name"], "Atelopus patazensis");
        assert_eq!(out.view["family"], "Bufonidae");
        assert_eq!(out.view["photo"], "/v1/assets/frog-2026-11-22.jpg");
        assert_eq!(frog.refresh(&ctx).unwrap(), Refresh::Unchanged);
        assert!(frog.next_refresh(&ctx).unwrap() > Duration::from_secs(60));

        // A new process the same day: from disk, no requests.
        let restarted = build(&Value::Null).unwrap();
        assert_eq!(restarted.initial(&ctx).unwrap().view["name"], "Atelopus patazensis");
        assert_eq!(restarted.refresh(&ctx).unwrap(), Refresh::Unchanged);
    }
}
