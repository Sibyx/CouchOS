//! A QR code on the TV (the app's TestFlight invite): scan it from the
//! couch. Drawn once at start; an empty `url` shows nothing.

use qrcodegen::{QrCode, QrCodeEcc};
use serde::Deserialize;
use serde_json::{Value, json};

use crate::error::{Error, Result};
use crate::svg;
use crate::view;
use crate::widget::{self, Ctx, Output, Widget};

/// About this many pixels on the TV; the exact size is a whole number of
/// pixels per module, so the code stays crisp.
const TARGET_PX: usize = 216;
const QUIET: usize = 2;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Params {
    #[serde(default)]
    url: String,
    #[serde(default = "default_title")]
    title: String,
    #[serde(default)]
    subtitle: String,
}

fn default_title() -> String {
    "Get the app".into()
}

pub struct Qr {
    url: String,
    title: String,
    subtitle: String,
}

pub fn build(params: &Value) -> Result<Box<dyn Widget>> {
    let p: Params = widget::params("qr", params)?;
    let url = p.url.trim().to_owned();
    if !url.is_empty() {
        QrCode::encode_text(&url, QrCodeEcc::Medium).map_err(|_| Error::invalid("qr: url too long"))?;
    }
    Ok(Box::new(Qr { url, title: p.title, subtitle: p.subtitle }))
}

/// The code as SVG: one path of dark modules on a white, rounded card.
pub fn qr_svg(text: &str) -> Result<(String, usize)> {
    let code = QrCode::encode_text(text, QrCodeEcc::Medium).map_err(|_| Error::invalid("qr: text too long"))?;
    let n = code.size() as usize;
    let side = n + 2 * QUIET;
    let mut path = String::new();
    for y in 0..n {
        for x in 0..n {
            if code.get_module(x as i32, y as i32) {
                path.push_str(&format!("M{} {}h1v1h-1z", x + QUIET, y + QUIET));
            }
        }
    }
    let svg = format!(
        r##"<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side} {side}" shape-rendering="crispEdges"><rect width="{side}" height="{side}" rx="1.2" fill="#FFFFFF"/><path d="{path}" fill="#111111"/></svg>"##
    );
    Ok((svg, side))
}

impl Widget for Qr {
    fn kind(&self) -> &'static str {
        "qr"
    }

    fn initial(&self, ctx: &Ctx) -> Option<Output> {
        if self.url.is_empty() {
            return Some(Output::new(json!({"url": null}), view! {"image" => "", "title" => "", "subtitle" => ""}));
        }
        let drawn = (|| -> Result<(String, String)> {
            let (source, side) = qr_svg(&self.url)?;
            let px = (TARGET_PX / side).max(1) * side;
            let hash = svg::content_hash(&source);
            let (png, svg_name) = (format!("qr-{hash}.png"), format!("qrsvg-{hash}.svg"));
            if !ctx.assets.exists(&png) {
                ctx.assets.put(&png, &svg::png(&source, px as u32, px as u32)?)?;
                ctx.assets.put(&svg_name, source.as_bytes())?;
            }
            Ok((format!("/v1/assets/{png}"), format!("/v1/assets/{svg_name}")))
        })();
        match drawn {
            Ok((png, svg_url)) => Some(Output::new(
                json!({"url": self.url, "title": self.title, "subtitle": self.subtitle, "image": png, "svg": svg_url}),
                view! {"image" => png, "title" => self.title, "subtitle" => self.subtitle},
            )),
            Err(err) => {
                crate::log::warn(&format!("qr: {err}"));
                None
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::clock::Date;
    use crate::testing::{FakeFetch, ctx_at};

    #[test]
    fn draws_a_crisp_code_and_hides_when_empty() {
        let ctx = ctx_at(FakeFetch::new(), Date { year: 2026, month: 11, day: 22 });
        let qr = build(&json!({"url": "https://testflight.apple.com/join/Ur8dT4mb", "title": "CouchOS"})).unwrap();
        let out = qr.initial(&ctx).unwrap();
        let png = out.view["image"].trim_start_matches("/v1/assets/").to_owned();
        assert!(png.starts_with("qr-") && ctx.assets.exists(&png));
        assert_eq!(out.view["title"], "CouchOS");
        let (_, side) = qr_svg("https://testflight.apple.com/join/Ur8dT4mb").unwrap();
        assert_eq!(side, 29 + 2 * QUIET); // version 3
        let none = build(&Value::Null).unwrap().initial(&ctx).unwrap();
        assert_eq!(none.view["image"], "");
    }
}
