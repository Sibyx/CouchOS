//! The duck: dressed for the weather, drawn from SVG blocks.
//!
//! Two halves. [`Duck::dress`] turns the weather into a [`Look`] by running
//! an ordered list of rules (each matching rule puts items on slots; later
//! rules win a slot, `none` empties it; the last matching `say` is the tip).
//! [`Look::svg`] then layers the scene: sky, ground, precipitation behind,
//! the duck and its clothes, precipitation in front.
//!
//! The rules and every colour come from config (`weather.params.duck`), with
//! the defaults below; `extra_rules` appends to the defaults, `rules`
//! replaces them.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::error::{Error, Result};

// --- the weather, as the duck sees it -------------------------------------------

/// Ordered by how much it matters for clothing: the worst sky in the next
/// hours wins.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Sky {
    Clear,
    PartlyCloudy,
    Cloudy,
    Fog,
    Drizzle,
    Rain,
    FreezingRain,
    Snow,
    Storm,
}

impl Sky {
    /// From a WMO weather code.
    pub fn of(code: u8) -> Self {
        match code {
            0 | 1 => Self::Clear,
            2 => Self::PartlyCloudy,
            45 | 48 => Self::Fog,
            51 | 53 | 55 => Self::Drizzle,
            56 | 57 | 66 | 67 => Self::FreezingRain,
            61 | 63 | 65 | 80..=82 => Self::Rain,
            71 | 73 | 75 | 77 | 85 | 86 => Self::Snow,
            95 | 96 | 99 => Self::Storm,
            _ => Self::Cloudy,
        }
    }

    pub fn wet(self) -> bool {
        self >= Self::Drizzle
    }
}

/// What the rules look at. `feels` and `sky` cover now and the next
/// `look_ahead_hours` (the duck dresses for going out, not for this second).
#[derive(Debug, Clone, PartialEq)]
pub struct Conditions {
    pub sky: Sky,
    pub night: bool,
    pub temp: f64,
    pub feels: f64,
    pub high: Option<f64>,
    pub low: Option<f64>,
    pub rain_chance: Option<u8>,
    pub wind_kmh: f64,
}

// --- clothing ----------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Slot {
    Head,
    Face,
    Neck,
    Top,
    Coat,
    Hands,
    Feet,
    Held,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Item {
    /// Takes off whatever an earlier rule put on the slot.
    None,
    Beanie,
    Cap,
    SunHat,
    RainHat,
    Sunglasses,
    Scarf,
    Sweater,
    WinterJacket,
    Raincoat,
    Mittens,
    Boots,
    Umbrella,
    IceCream,
    Lantern,
}

impl Item {
    #[cfg(test)]
    pub const ALL: [Item; 14] = [
        Item::Beanie,
        Item::Cap,
        Item::SunHat,
        Item::RainHat,
        Item::Sunglasses,
        Item::Scarf,
        Item::Sweater,
        Item::WinterJacket,
        Item::Raincoat,
        Item::Mittens,
        Item::Boots,
        Item::Umbrella,
        Item::IceCream,
        Item::Lantern,
    ];

    pub fn slot(self) -> Option<Slot> {
        Some(match self {
            Item::None => return None,
            Item::Beanie | Item::Cap | Item::SunHat | Item::RainHat => Slot::Head,
            Item::Sunglasses => Slot::Face,
            Item::Scarf => Slot::Neck,
            Item::Sweater => Slot::Top,
            Item::WinterJacket | Item::Raincoat => Slot::Coat,
            Item::Mittens => Slot::Hands,
            Item::Boots => Slot::Feet,
            Item::Umbrella | Item::IceCream | Item::Lantern => Slot::Held,
        })
    }
}

// --- rules -------------------------------------------------------------------------

/// All given fields must hold. Temperatures are the "feels like" value.
#[derive(Deserialize, Serialize, Clone, Debug, Default, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct When {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub feels_below: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub feels_at_least: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sky: Option<Vec<Sky>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rain_chance_at_least: Option<u8>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub wind_at_least: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub night: Option<bool>,
}

impl When {
    fn holds(&self, c: &Conditions) -> bool {
        self.feels_below.is_none_or(|t| c.feels < t)
            && self.feels_at_least.is_none_or(|t| c.feels >= t)
            && self.sky.as_ref().is_none_or(|s| s.contains(&c.sky))
            && self.rain_chance_at_least.is_none_or(|p| c.rain_chance.unwrap_or(0) >= p)
            && self.wind_at_least.is_none_or(|w| c.wind_kmh >= w)
            && self.night.is_none_or(|n| c.night == n)
    }
}

#[derive(Deserialize, Serialize, Clone, Debug, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Rule {
    #[serde(default)]
    pub when: When,
    #[serde(default)]
    pub wear: BTreeMap<Slot, Item>,
    /// The duck's line on the TV. `{temp}`, `{feels}`, `{high}`, `{low}`,
    /// `{rain_chance}`, `{wind}` are filled in.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub say: Option<String>,
}

impl Rule {
    fn check(&self) -> Result<()> {
        for (slot, item) in &self.wear {
            if item.slot().is_some_and(|s| s != *slot) {
                return Err(Error::invalid(format!("duck rule: {item:?} is not worn on {slot:?}")));
            }
        }
        Ok(())
    }
}

/// The defaults, coldest last within temperature, then the sky, then wind.
pub fn default_rules() -> Vec<Rule> {
    serde_json::from_value(json!([
        {"when": {"feels_at_least": 26}, "wear": {"head": "sun_hat", "face": "sunglasses", "held": "ice_cream"},
         "say": "Hot one: {high} today. Drink water!"},
        {"when": {"feels_at_least": 18, "feels_below": 26}, "wear": {"head": "cap"},
         "say": "T-shirt weather, up to {high}"},
        {"when": {"feels_at_least": 18, "sky": ["clear", "partly_cloudy"], "night": false}, "wear": {"face": "sunglasses"}},
        {"when": {"feels_at_least": 10, "feels_below": 18}, "wear": {"top": "sweater"},
         "say": "Sweater weather: {low} to {high}"},
        {"when": {"feels_at_least": 3, "feels_below": 10}, "wear": {"top": "sweater", "neck": "scarf", "head": "cap"},
         "say": "Chilly, {low} to {high}: scarf on"},
        {"when": {"feels_below": 3}, "wear": {"top": "sweater", "coat": "winter_jacket", "neck": "scarf", "head": "beanie"},
         "say": "Cold today, {low} to {high}. Wrap up warm"},
        {"when": {"feels_below": -3}, "wear": {"hands": "mittens", "feet": "boots"},
         "say": "Freezing: feels down to {feels}. Mittens on!"},
        {"when": {"wind_at_least": 35}, "say": "Windy: {wind} km/h. Hold on to your hat"},
        {"when": {"sky": ["fog"]}, "wear": {"held": "lantern"}, "say": "Foggy out there: take it slow"},
        {"when": {"sky": ["drizzle", "rain", "storm"], "feels_at_least": 3},
         "wear": {"coat": "raincoat", "head": "rain_hat", "face": "none"}},
        {"when": {"sky": ["drizzle", "rain", "freezing_rain", "storm"]}, "wear": {"held": "umbrella", "feet": "boots"},
         "say": "Take an umbrella: {rain_chance} chance of rain"},
        {"when": {"sky": ["freezing_rain"]}, "say": "Freezing rain: slippery out there!"},
        {"when": {"sky": ["storm"]}, "say": "Thunderstorm: better stay in"},
        {"when": {"sky": ["snow"]},
         "wear": {"head": "beanie", "top": "sweater", "coat": "winter_jacket", "neck": "scarf", "hands": "mittens", "feet": "boots", "held": "none", "face": "none"},
         "say": "Snow! {low} to {high}. Boots on"},
    ]))
    .expect("default duck rules")
}

// --- colours -------------------------------------------------------------------------

const PALETTE: &[(&str, &str)] = &[
    ("duck", "#FFD23F"),
    ("wing", "#F5B700"),
    ("beak", "#FF8C1A"),
    ("feet", "#FF8C1A"),
    ("sweater", "#2A9D8F"),
    ("sweater_pattern", "#F1FAEE"),
    ("winter_jacket", "#3D5A80"),
    ("fur", "#F1E6D0"),
    ("raincoat", "#E63946"),
    ("scarf", "#E63946"),
    ("scarf_stripe", "#F1FAEE"),
    ("beanie", "#C1121F"),
    ("pompom", "#F1FAEE"),
    ("cap", "#3A86FF"),
    ("sun_hat", "#E9C46A"),
    ("ribbon", "#E63946"),
    ("rain_hat", "#E63946"),
    ("mittens", "#C1121F"),
    ("boots", "#264653"),
    ("umbrella", "#3A86FF"),
    ("umbrella_stripe", "#F1FAEE"),
];

fn valid_color(c: &str) -> bool {
    c.len() == 7 && c.starts_with('#') && c[1..].bytes().all(|b| b.is_ascii_hexdigit())
}

// --- config ----------------------------------------------------------------------------

#[derive(Deserialize, Debug)]
#[serde(deny_unknown_fields)]
pub struct Params {
    #[serde(default = "yes")]
    pub enabled: bool,
    /// Dress for now and this many hours ahead.
    #[serde(default = "default_look_ahead")]
    pub look_ahead_hours: usize,
    /// An hour's rain or snow counts from this chance (%).
    #[serde(default = "default_min_chance")]
    pub min_chance: u8,
    /// The scarf flies from this wind speed (km/h).
    #[serde(default = "default_windy")]
    pub windy_kmh: f64,
    /// Replace the default rules.
    #[serde(default)]
    pub rules: Option<Vec<Rule>>,
    /// Run after the rules (the defaults or `rules`).
    #[serde(default)]
    pub extra_rules: Vec<Rule>,
    /// Override colours by name ("duck", "scarf", ...), "#RRGGBB".
    #[serde(default)]
    pub colors: BTreeMap<String, String>,
}

impl Default for Params {
    fn default() -> Self {
        serde_json::from_value(json!({})).expect("default duck params")
    }
}

fn yes() -> bool {
    true
}
fn default_look_ahead() -> usize {
    3
}
fn default_min_chance() -> u8 {
    30
}
fn default_windy() -> f64 {
    30.0
}

pub struct Duck {
    pub look_ahead_hours: usize,
    pub min_chance: u8,
    windy_kmh: f64,
    rules: Vec<Rule>,
    palette: BTreeMap<&'static str, String>,
}

impl Duck {
    pub fn build(p: Params) -> Result<Option<Self>> {
        if !p.enabled {
            return Ok(None);
        }
        let mut rules = p.rules.unwrap_or_else(default_rules);
        rules.extend(p.extra_rules);
        for rule in &rules {
            rule.check()?;
        }
        let mut palette: BTreeMap<&'static str, String> = PALETTE.iter().map(|(k, v)| (*k, (*v).to_owned())).collect();
        for (name, colour) in p.colors {
            let Some((key, _)) = PALETTE.iter().find(|(k, _)| *k == name) else {
                let known: Vec<_> = PALETTE.iter().map(|(k, _)| *k).collect();
                return Err(Error::invalid(format!("duck colour {name:?}: known are {known:?}")));
            };
            if !valid_color(&colour) {
                return Err(Error::invalid(format!("duck colour {name}: {colour:?} is not #RRGGBB")));
            }
            palette.insert(key, colour);
        }
        Ok(Some(Self { look_ahead_hours: p.look_ahead_hours.min(12), min_chance: p.min_chance, windy_kmh: p.windy_kmh, rules, palette }))
    }

    pub fn dress(&self, c: &Conditions) -> Look {
        let mut outfit = BTreeMap::new();
        let mut tip = String::new();
        for rule in self.rules.iter().filter(|r| r.when.holds(c)) {
            for (slot, item) in &rule.wear {
                if *item == Item::None {
                    outfit.remove(slot);
                } else {
                    outfit.insert(*slot, *item);
                }
            }
            if let Some(say) = &rule.say {
                tip = fill(say, c);
            }
        }
        Look {
            sky: c.sky,
            night: c.night,
            windy: c.wind_kmh >= self.windy_kmh,
            shiver: c.feels < -3.0,
            sweat: c.feels >= 28.0,
            outfit,
            tip,
            palette: self.palette.clone(),
        }
    }
}

fn deg(t: Option<f64>) -> String {
    t.map_or_else(|| "–".into(), |t| format!("{}°", t.round() as i64))
}

fn fill(template: &str, c: &Conditions) -> String {
    template
        .replace("{temp}", &deg(Some(c.temp)))
        .replace("{feels}", &deg(Some(c.feels)))
        .replace("{high}", &deg(c.high))
        .replace("{low}", &deg(c.low))
        .replace("{rain_chance}", &c.rain_chance.map_or_else(|| "some".into(), |p| format!("{p} %")))
        .replace("{wind}", &format!("{}", c.wind_kmh.round() as i64))
}

// --- the picture ------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub struct Look {
    pub sky: Sky,
    pub night: bool,
    pub windy: bool,
    pub shiver: bool,
    pub sweat: bool,
    pub outfit: BTreeMap<Slot, Item>,
    pub tip: String,
    palette: BTreeMap<&'static str, String>,
}

/// The duck's geometry (viewBox 512 x 512, facing the viewer).
const HEAD: (f64, f64, f64) = (256.0, 192.0, 80.0);
const BODY: (f64, f64, f64, f64) = (256.0, 355.0, 125.0, 105.0);
/// Where the wings end: mittens, the umbrella handle, the ice cream.
const LEFT_HAND: (f64, f64) = (122.0, 410.0);
const RIGHT_HAND: (f64, f64) = (390.0, 410.0);

/// Deterministic scatter: the same picture for the same look.
struct Scatter(u64);

impl Scatter {
    fn next(&mut self) -> f64 {
        self.0 = self.0.wrapping_mul(6_364_136_223_846_793_005).wrapping_add(1_442_695_040_888_963_407);
        (self.0 >> 11) as f64 / (1u64 << 53) as f64
    }
}

impl Look {
    pub fn wears(&self, item: Item) -> bool {
        item.slot().is_some_and(|s| self.outfit.get(&s) == Some(&item))
    }

    fn c(&self, name: &str) -> &str {
        self.palette.get(name).map_or("#FF00FF", String::as_str)
    }

    /// Structured, for clients that draw their own duck.
    pub fn data(&self) -> Value {
        json!({
            "sky": self.sky,
            "night": self.night,
            "windy": self.windy,
            "tip": self.tip,
            "outfit": self.outfit,
        })
    }

    pub fn svg(&self) -> String {
        let mut s = String::with_capacity(16 * 1024);
        s.push_str(r#"<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">"#);
        self.defs(&mut s);
        s.push_str(r#"<g clip-path="url(#scene)">"#);
        self.backdrop(&mut s);
        self.precipitation(&mut s, 0);
        if self.wears(Item::Umbrella) {
            self.umbrella(&mut s);
        }
        self.duck(&mut s);
        self.precipitation(&mut s, 1);
        if self.sky == Sky::Fog {
            s.push_str(r##"<g fill="#FFFFFF" opacity="0.4"><rect x="-30" y="396" width="330" height="24" rx="12"/><rect x="250" y="350" width="300" height="20" rx="10"/></g>"##);
        }
        s.push_str("</g></svg>");
        s
    }

    fn defs(&self, s: &mut String) {
        let (top, bottom) = match (self.night, self.sky) {
            (true, Sky::Clear | Sky::PartlyCloudy) => ("#0F1B3D", "#2B3A67"),
            (true, _) => ("#1E2638", "#3A4458"),
            (false, Sky::Clear) => ("#5DB7EE", "#CDEEFF"),
            (false, Sky::PartlyCloudy) => ("#7FB6DC", "#DCEFFA"),
            (false, Sky::Cloudy) => ("#93A9BA", "#DCE4EA"),
            (false, Sky::Fog) => ("#AEB6BD", "#E4E7EA"),
            (false, Sky::Snow) => ("#A9BCCB", "#EEF3F7"),
            (false, Sky::Storm) => ("#454F5E", "#7C8794"),
            (false, _) => ("#6F8597", "#AEBCC8"),
        };
        let (bx, by, brx, bry) = BODY;
        s.push_str(&format!(
            r##"<defs><clipPath id="scene"><rect width="512" height="512" rx="56"/></clipPath>
<clipPath id="body"><ellipse cx="{bx}" cy="{by}" rx="{brx}" ry="{bry}"/></clipPath>
<linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{top}"/><stop offset="1" stop-color="{bottom}"/></linearGradient>
<radialGradient id="glow"><stop offset="0" stop-color="#FFE9A8" stop-opacity="0.9"/><stop offset="1" stop-color="#FFE9A8" stop-opacity="0"/></radialGradient>
<mask id="crescent"><rect width="512" height="512" fill="#fff"/><circle cx="438" cy="74" r="30" fill="#000"/></mask></defs>"##
        ));
    }

    fn backdrop(&self, s: &mut String) {
        s.push_str(r#"<rect width="512" height="512" fill="url(#sky)"/>"#);
        let clear = matches!(self.sky, Sky::Clear | Sky::PartlyCloudy);
        if self.night {
            if clear {
                let mut r = Scatter(7);
                for _ in 0..18 {
                    let (x, y, size) = (r.next() * 512.0, r.next() * 260.0, 1.5 + r.next() * 2.5);
                    s.push_str(&format!(r##"<circle cx="{x:.0}" cy="{y:.0}" r="{size:.1}" fill="#FFF8E1" opacity="0.8"/>"##));
                }
            }
            if self.sky <= Sky::Cloudy {
                s.push_str(r##"<circle cx="420" cy="88" r="36" fill="#F4F1DE" mask="url(#crescent)"/>"##);
            }
        } else if clear {
            s.push_str(r##"<g transform="translate(424 92)"><g stroke="#FFD54A" stroke-width="8" stroke-linecap="round">"##);
            for i in 0..8 {
                let a = f64::from(i) * std::f64::consts::FRAC_PI_4;
                let (x1, y1, x2, y2) = (a.cos() * 56.0, a.sin() * 56.0, a.cos() * 74.0, a.sin() * 74.0);
                s.push_str(&format!(r#"<line x1="{x1:.1}" y1="{y1:.1}" x2="{x2:.1}" y2="{y2:.1}"/>"#));
            }
            s.push_str(r##"</g><circle r="44" fill="#FFD54A"/><circle r="34" fill="#FFE27A"/></g>"##);
        }
        let (light, dark) = if self.night { ("#56627A", "#3D475C") } else { ("#FFFFFF", "#DDE3EA") };
        let (light, dark) = match self.sky {
            Sky::Storm => ("#5C6573", "#454D59"),
            Sky::Rain | Sky::Drizzle | Sky::FreezingRain if !self.night => ("#C3CCD5", "#A5B0BB"),
            _ => (light, dark),
        };
        match self.sky {
            Sky::Clear => {}
            Sky::PartlyCloudy => cloud(s, 300.0, 120.0, 1.0, light, dark),
            _ => {
                cloud(s, 70.0, 80.0, 1.15, light, dark);
                cloud(s, 300.0, 56.0, 1.35, light, dark);
                if self.sky != Sky::Fog {
                    cloud(s, 190.0, 150.0, 0.8, light, dark);
                }
            }
        }
        if self.sky == Sky::Storm {
            s.push_str(r##"<path d="M96 132 L64 200 L92 200 L70 266 L130 182 L100 182 L124 132 Z" fill="#FFD54A"/>"##);
        }
        if self.sky == Sky::Fog {
            s.push_str(r##"<g fill="#FFFFFF" opacity="0.5"><rect x="-40" y="170" width="420" height="26" rx="13"/><rect x="200" y="226" width="380" height="22" rx="11"/><rect x="-20" y="300" width="300" height="22" rx="11"/></g>"##);
        }
        if self.windy {
            s.push_str(r##"<g fill="none" stroke="#FFFFFF" stroke-opacity="0.7" stroke-width="5" stroke-linecap="round">
<path d="M24 232 Q86 214 132 232 T214 228"/><path d="M60 270 Q110 256 150 270"/><path d="M380 250 Q430 234 490 246"/></g>"##);
        }
        // the ground
        let ground = match (self.sky, self.night) {
            (Sky::Snow, false) => ("#F7FAFC", "#DCE6EE"),
            (Sky::Snow, true) => ("#C7D2E0", "#AAB6C7"),
            (_, false) => ("#86C06C", "#6FA758"),
            (_, true) => ("#3F5D3C", "#33502F"),
        };
        s.push_str(&format!(
            r#"<path d="M0 438 Q140 412 256 424 T512 430 V512 H0 Z" fill="{}"/><path d="M0 470 Q180 452 300 466 T512 462 V512 H0 Z" fill="{}"/>"#,
            ground.0, ground.1
        ));
        if self.sky.wet() && self.sky != Sky::Snow {
            s.push_str(r##"<ellipse cx="256" cy="478" rx="170" ry="20" fill="#8FB3CF" opacity="0.75"/>
<ellipse cx="170" cy="480" rx="26" ry="5" fill="none" stroke="#E3F1FF" stroke-width="3" opacity="0.8"/>
<ellipse cx="350" cy="476" rx="20" ry="4" fill="none" stroke="#E3F1FF" stroke-width="3" opacity="0.8"/>"##);
        }
        s.push_str(r##"<ellipse cx="256" cy="466" rx="116" ry="14" fill="#000" opacity="0.14"/>"##);
    }

    /// Rain and snow, half behind the duck, half in front. Nothing falls
    /// under the umbrella.
    fn precipitation(&self, s: &mut String, layer: u64) {
        let (drops, flakes) = match self.sky {
            Sky::Drizzle => (16, 0),
            Sky::Rain => (34, 0),
            Sky::Storm => (40, 0),
            Sky::FreezingRain => (22, 14),
            Sky::Snow => (0, 46),
            _ => return,
        };
        let umbrella = self.wears(Item::Umbrella);
        let dry = |x: f64, y: f64| umbrella && (130.0..=470.0).contains(&x) && y > 70.0;
        let mut r = Scatter(42 + layer);
        for _ in 0..drops / 2 {
            let (x, y) = (r.next() * 520.0, r.next() * 450.0);
            if !dry(x, y) {
                s.push_str(&format!(
                    r##"<line x1="{x:.0}" y1="{y:.0}" x2="{:.0}" y2="{:.0}" stroke="#E3F1FF" stroke-width="4" stroke-linecap="round" opacity="0.85"/>"##,
                    x - 7.0,
                    y + 20.0
                ));
            }
        }
        for _ in 0..flakes / 2 {
            let (x, y, size) = (r.next() * 512.0, r.next() * 470.0, 3.0 + r.next() * 5.0);
            if !dry(x, y) {
                s.push_str(&format!(r##"<circle cx="{x:.0}" cy="{y:.0}" r="{size:.1}" fill="#FFFFFF" opacity="0.92"/>"##));
            }
        }
    }

    fn umbrella(&self, s: &mut String) {
        let (main, stripe) = (self.c("umbrella"), self.c("umbrella_stripe"));
        // Drawn upright around its tip, then leaned so the handle lands in the right wing.
        s.push_str(&format!(
            r##"<g transform="translate(300 52) rotate(-14)">
<line x1="0" y1="-30" x2="0" y2="360" stroke="#4A4A4A" stroke-width="7" stroke-linecap="round"/>
<path d="M0 360 q0 26 -20 26 q-16 0 -16 -16" fill="none" stroke="#4A4A4A" stroke-width="8" stroke-linecap="round"/>
<path d="M-170 50 Q-150 -40 0 -44 Q150 -40 170 50 Q127 32 85 50 Q42 32 0 50 Q-42 32 -85 50 Q-127 32 -170 50 Z" fill="{main}"/>
<path d="M0 -44 Q-36 0 -42 41 Q-20 32 0 50 Z M0 -44 Q100 -36 127 41 Q106 32 85 50 Q70 -10 0 -44 Z" fill="{stripe}" opacity="0.85"/>
<circle cx="0" cy="-46" r="7" fill="#4A4A4A"/></g>"##
        ));
    }

    fn duck(&self, s: &mut String) {
        let (hx, hy, hr) = HEAD;
        let (bx, by, brx, bry) = BODY;
        let duck = self.c("duck");

        // feet, or boots
        if self.wears(Item::Boots) {
            let boots = self.c("boots");
            for x in [212.0, 300.0] {
                s.push_str(&format!(
                    r##"<rect x="{:.0}" y="420" width="48" height="48" rx="12" fill="{boots}"/><ellipse cx="{:.0}" cy="466" rx="34" ry="13" fill="{boots}"/><rect x="{:.0}" y="470" width="68" height="8" rx="4" fill="#1B1B1B" opacity="0.55"/><rect x="{:.0}" y="428" width="48" height="7" fill="#FFFFFF" opacity="0.25"/>"##,
                    x - 24.0, x + 6.0, x - 28.0, x - 24.0
                ));
            }
        } else {
            let feet = self.c("feet");
            for dx in [0.0, 88.0] {
                s.push_str(&format!(
                    r#"<path d="M{} 462 C{} 438 {} 438 {} 462 Q{} 454 {} 466 Q{} 454 {} 466 Q{} 454 {} 462 Z" fill="{feet}"/>"#,
                    180.0 + dx, 180.0 + dx, 244.0 + dx, 244.0 + dx, 233.0 + dx, 222.0 + dx, 212.0 + dx, 202.0 + dx, 191.0 + dx, 180.0 + dx
                ));
            }
        }

        // tail and body
        s.push_str(&format!(
            r#"<path d="M150 318 Q112 280 120 252 Q150 284 176 300 Z" fill="{duck}"/><ellipse cx="{bx}" cy="{by}" rx="{brx}" ry="{bry}" fill="{duck}"/>"#
        ));
        s.push_str(r##"<ellipse cx="256" cy="388" rx="72" ry="52" fill="#FFFFFF" opacity="0.18"/>"##);

        // clothes on the body
        s.push_str(r#"<g clip-path="url(#body)">"#);
        if self.wears(Item::Sweater) {
            let (c, p) = (self.c("sweater"), self.c("sweater_pattern"));
            s.push_str(&format!(
                r##"<rect x="0" y="240" width="512" height="194" fill="{c}"/><rect x="0" y="418" width="512" height="16" fill="#000" opacity="0.15"/>
<polyline points="120,330 146,312 172,330 198,312 224,330 250,312 276,330 302,312 328,330 354,312 380,330 406,312" fill="none" stroke="{p}" stroke-width="7" stroke-linejoin="round"/>
<g fill="{p}"><circle cx="160" cy="360" r="5"/><circle cx="208" cy="360" r="5"/><circle cx="256" cy="360" r="5"/><circle cx="304" cy="360" r="5"/><circle cx="352" cy="360" r="5"/></g>"##
            ));
        }
        if self.wears(Item::WinterJacket) {
            let c = self.c("winter_jacket");
            s.push_str(&format!(
                r##"<rect x="0" y="240" width="242" height="210" fill="{c}"/><rect x="270" y="240" width="242" height="210" fill="{c}"/>
<g fill="none" stroke="#000" stroke-opacity="0.18" stroke-width="5"><path d="M110 300 Q176 316 242 300 M270 300 Q336 316 402 300 M110 346 Q176 362 242 346 M270 346 Q336 362 402 346 M110 392 Q176 408 242 392 M270 392 Q336 408 402 392"/></g>
<rect x="236" y="240" width="8" height="210" fill="#000" opacity="0.2"/><rect x="268" y="240" width="8" height="210" fill="#000" opacity="0.2"/>
<rect x="0" y="434" width="242" height="16" fill="#000" opacity="0.2"/><rect x="270" y="434" width="242" height="16" fill="#000" opacity="0.2"/>"##
            ));
        }
        if self.wears(Item::Raincoat) {
            let c = self.c("raincoat");
            s.push_str(&format!(
                r##"<rect x="0" y="240" width="512" height="214" fill="{c}"/><rect x="253" y="270" width="6" height="184" fill="#000" opacity="0.18"/>
<g fill="#FFF3D6"><circle cx="276" cy="310" r="8"/><circle cx="276" cy="354" r="8"/><circle cx="276" cy="398" r="8"/></g>
<path d="M150 424 H362" stroke="#FFFFFF" stroke-opacity="0.35" stroke-width="6"/>"##
            ));
        }
        s.push_str("</g>");

        // collar behind the head
        if self.wears(Item::WinterJacket) {
            let fur = self.c("fur");
            s.push_str(&format!(
                r#"<g fill="{fur}"><ellipse cx="256" cy="262" rx="104" ry="26"/><circle cx="160" cy="258" r="18"/><circle cx="196" cy="248" r="18"/><circle cx="316" cy="248" r="18"/><circle cx="352" cy="258" r="18"/></g>"#
            ));
        }
        if self.wears(Item::Raincoat) {
            let c = self.c("raincoat");
            s.push_str(&format!(r#"<path d="M180 252 L256 296 L332 252 L320 286 L256 312 L192 286 Z" fill="{c}"/>"#));
        }

        // wings: sleeves when dressed
        let sleeve = [Item::WinterJacket, Item::Raincoat, Item::Sweater].into_iter().find(|&i| self.wears(i));
        let wing = match sleeve {
            Some(Item::WinterJacket) => self.c("winter_jacket"),
            Some(Item::Raincoat) => self.c("raincoat"),
            Some(_) => self.c("sweater"),
            None => self.c("wing"),
        };
        let held = self.outfit.get(&Slot::Held).copied();
        // The right wing reaches up a little when it holds something.
        let right_tilt = if held.is_some() { -32 } else { -18 };
        s.push_str(&format!(
            r#"<ellipse cx="142" cy="350" rx="36" ry="64" transform="rotate(18 142 350)" fill="{wing}"/><ellipse cx="370" cy="350" rx="36" ry="64" transform="rotate({right_tilt} 370 350)" fill="{wing}"/>"#
        ));
        if sleeve.is_some() {
            s.push_str(&format!(
                r##"<g fill="#000" opacity="0.16"><ellipse cx="128" cy="396" rx="26" ry="10" transform="rotate(18 128 396)"/><ellipse cx="384" cy="396" rx="26" ry="10" transform="rotate({right_tilt} 384 396)"/></g>"##
            ));
        }
        if self.wears(Item::Mittens) {
            let m = self.c("mittens");
            for (x, y) in [LEFT_HAND, RIGHT_HAND] {
                s.push_str(&format!(
                    r##"<circle cx="{x}" cy="{y}" r="22" fill="{m}"/><circle cx="{:.0}" cy="{:.0}" r="9" fill="{m}"/><rect x="{:.0}" y="{:.0}" width="40" height="10" rx="5" fill="#FFFFFF" opacity="0.6"/>"##,
                    if x < 256.0 { x + 18.0 } else { x - 18.0 }, y - 10.0, x - 20.0, y - 26.0
                ));
            }
        }

        // head
        s.push_str(&format!(r#"<circle cx="{hx}" cy="{hy}" r="{hr}" fill="{duck}"/>"#));
        if !self.outfit.contains_key(&Slot::Head) {
            s.push_str(&format!(r#"<path d="M250 116 Q240 88 262 82 Q250 96 266 114 Z" fill="{duck}"/>"#));
        }
        // face
        s.push_str(r##"<g fill="#FF8FA3" opacity="0.45"><ellipse cx="198" cy="228" rx="16" ry="9"/><ellipse cx="314" cy="228" rx="16" ry="9"/></g>"##);
        if self.wears(Item::Sunglasses) {
            s.push_str(r##"<g fill="#1D1D1D"><rect x="196" y="186" width="52" height="32" rx="12"/><rect x="264" y="186" width="52" height="32" rx="12"/></g>
<path d="M246 196 Q256 190 266 196" stroke="#1D1D1D" stroke-width="5" fill="none"/>
<g stroke="#FFFFFF" stroke-opacity="0.5" stroke-width="4" stroke-linecap="round"><path d="M206 194 L218 194"/><path d="M274 194 L286 194"/></g>"##);
        } else if self.night && !self.sky.wet() {
            // sleepy
            s.push_str(r##"<g stroke="#2B2B2B" stroke-width="6" stroke-linecap="round" fill="none"><path d="M212 204 Q226 214 240 204"/><path d="M272 204 Q286 214 300 204"/></g>"##);
        } else {
            s.push_str(r##"<g fill="#2B2B2B"><ellipse cx="226" cy="202" rx="10" ry="13"/><ellipse cx="286" cy="202" rx="10" ry="13"/></g>
<g fill="#FFFFFF"><circle cx="230" cy="196" r="4"/><circle cx="290" cy="196" r="4"/></g>"##);
        }
        let beak = self.c("beak");
        s.push_str(&format!(
            r##"<ellipse cx="256" cy="248" rx="24" ry="9" fill="{beak}"/><ellipse cx="256" cy="246" rx="24" ry="9" fill="#000" opacity="0.15"/><ellipse cx="256" cy="236" rx="36" ry="14" fill="{beak}"/><ellipse cx="248" cy="231" rx="12" ry="4" fill="#FFFFFF" opacity="0.35"/>"##
        ));
        if self.sweat {
            s.push_str(r##"<path d="M340 140 Q352 160 346 170 Q336 176 332 166 Q330 156 340 140 Z" fill="#8FD3FF"/>"##);
        }
        if self.shiver {
            s.push_str(r##"<g fill="none" stroke="#FFFFFF" stroke-width="5" stroke-linecap="round" opacity="0.85"><path d="M108 300 Q98 316 108 332"/><path d="M92 292 Q78 316 92 340"/><path d="M404 300 Q414 316 404 332"/><path d="M420 292 Q434 316 420 340"/></g>"##);
        }

        // hats
        match self.outfit.get(&Slot::Head) {
            Some(Item::Beanie) => {
                let (c, p) = (self.c("beanie"), self.c("pompom"));
                s.push_str(&format!(
                    r##"<path d="M178 170 C178 84 334 84 334 170 Z" fill="{c}"/><circle cx="256" cy="90" r="20" fill="{p}"/>
<rect x="170" y="146" width="172" height="34" rx="16" fill="{c}"/><rect x="170" y="146" width="172" height="34" rx="16" fill="#000" opacity="0.18"/>
<g stroke="#000" stroke-opacity="0.18" stroke-width="4"><path d="M196 150 V176 M222 150 V176 M248 150 V176 M274 150 V176 M300 150 V176 M326 150 V176"/></g>"##
                ));
            }
            Some(Item::Cap) => {
                let c = self.c("cap");
                s.push_str(&format!(
                    r##"<path d="M180 164 C180 92 332 92 332 164 Z" fill="{c}"/><path d="M256 98 V164" stroke="#000" stroke-opacity="0.15" stroke-width="4"/>
<path d="M172 164 Q256 196 340 164 Q256 176 172 164 Z" fill="{c}"/><path d="M172 164 Q256 196 340 164 Q256 176 172 164 Z" fill="#000" opacity="0.2"/><circle cx="256" cy="100" r="7" fill="{c}"/>"##
                ));
            }
            Some(Item::SunHat) => {
                let (c, r) = (self.c("sun_hat"), self.c("ribbon"));
                s.push_str(&format!(
                    r##"<ellipse cx="256" cy="158" rx="152" ry="28" fill="{c}"/><ellipse cx="256" cy="158" rx="152" ry="28" fill="#000" opacity="0.08"/>
<path d="M192 156 C192 82 320 82 320 156 Z" fill="{c}"/><rect x="192" y="132" width="128" height="20" fill="{r}"/>"##
                ));
            }
            Some(Item::RainHat) => {
                let c = self.c("rain_hat");
                s.push_str(&format!(
                    r##"<path d="M186 160 C186 88 326 88 326 160 Z" fill="{c}"/>
<path d="M146 176 Q256 136 366 176 Q376 196 352 200 Q256 166 160 200 Q136 196 146 176 Z" fill="{c}"/><path d="M146 176 Q256 136 366 176" stroke="#000" stroke-opacity="0.15" stroke-width="4" fill="none"/>"##
                ));
            }
            _ => {}
        }

        // scarf, over the neck
        if self.wears(Item::Scarf) {
            let (c, p) = (self.c("scarf"), self.c("scarf_stripe"));
            let tail = if self.windy { "rotate(-72 312 276)" } else { "rotate(-6 312 276)" };
            s.push_str(&format!(
                r##"<g transform="{tail}"><rect x="296" y="272" width="34" height="96" rx="9" fill="{c}"/><g fill="{p}"><rect x="296" y="300" width="34" height="9"/><rect x="296" y="330" width="34" height="9"/></g>
<g stroke="{c}" stroke-width="5" stroke-linecap="round"><path d="M302 368 V382 M313 368 V384 M324 368 V382"/></g></g>
<rect x="176" y="256" width="160" height="34" rx="17" fill="{c}"/><g fill="{p}"><rect x="206" y="256" width="10" height="34"/><rect x="236" y="256" width="10" height="34"/><rect x="266" y="256" width="10" height="34"/><rect x="296" y="256" width="10" height="34"/></g>"##
            ));
        }

        // in the right wing
        match held {
            Some(Item::IceCream) => s.push_str(r##"<path d="M372 376 L408 376 L390 424 Z" fill="#D9A066"/><path d="M378 386 L402 386 M383 398 L398 398" stroke="#B5814A" stroke-width="3"/>
<circle cx="382" cy="364" r="16" fill="#FF8FB1"/><circle cx="400" cy="360" r="16" fill="#9EE6CF"/><circle cx="392" cy="344" r="14" fill="#FFF1C1"/><circle cx="394" cy="328" r="6" fill="#E63946"/>"##),
            Some(Item::Lantern) => s.push_str(r##"<circle cx="398" cy="392" r="70" fill="url(#glow)"/><path d="M384 362 Q398 340 412 362" fill="none" stroke="#3A3A3A" stroke-width="5"/>
<rect x="380" y="364" width="36" height="50" rx="8" fill="#3A3A3A"/><rect x="386" y="370" width="24" height="38" rx="5" fill="#FFE08A"/>"##),
            _ => {}
        }
    }
}

fn cloud(s: &mut String, x: f64, y: f64, scale: f64, light: &str, dark: &str) {
    s.push_str(&format!(
        r#"<g transform="translate({x} {y}) scale({scale})"><ellipse cx="62" cy="30" rx="70" ry="22" fill="{dark}"/><circle cx="26" cy="12" r="28" fill="{light}"/><circle cx="66" cy="-6" r="38" fill="{light}"/><circle cx="104" cy="14" r="26" fill="{light}"/><rect x="0" y="8" width="128" height="30" rx="15" fill="{light}"/></g>"#
    ));
}

#[cfg(test)]
mod tests {
    use super::*;

    fn conditions(sky: Sky, feels: f64) -> Conditions {
        Conditions { sky, night: false, temp: feels + 2.0, feels, high: Some(feels + 4.0), low: Some(feels - 3.0), rain_chance: Some(80), wind_kmh: 8.0 }
    }

    fn duck() -> Duck {
        Duck::build(Params::default()).unwrap().unwrap()
    }

    #[test]
    fn snow_means_beanie_sweater_and_winter_jacket() {
        let look = duck().dress(&conditions(Sky::Snow, -1.0));
        for item in [Item::Beanie, Item::Sweater, Item::WinterJacket, Item::Scarf, Item::Mittens, Item::Boots] {
            assert!(look.wears(item), "{item:?} in {:?}", look.outfit);
        }
        assert!(!look.outfit.contains_key(&Slot::Held));
        assert_eq!(look.tip, "Snow! -4° to 3°. Boots on");
    }

    #[test]
    fn rain_brings_the_umbrella_and_later_rules_win() {
        let look = duck().dress(&conditions(Sky::Rain, 12.0));
        assert!(look.wears(Item::Raincoat) && look.wears(Item::Umbrella) && look.wears(Item::RainHat));
        assert!(look.wears(Item::Sweater));
        assert_eq!(look.tip, "Take an umbrella: 80 % chance of rain");
        let hot = duck().dress(&conditions(Sky::Clear, 30.0));
        assert!(hot.wears(Item::SunHat) && hot.wears(Item::Sunglasses) && hot.wears(Item::IceCream) && hot.sweat);
    }

    #[test]
    fn config_extends_replaces_and_is_checked() {
        let p: Params = serde_json::from_value(json!({
            "extra_rules": [{"when": {"night": true}, "say": "Night night"}],
            "colors": {"scarf": "#00FF00"}
        }))
        .unwrap();
        let d = Duck::build(p).unwrap().unwrap();
        let mut c = conditions(Sky::Clear, 5.0);
        c.night = true;
        let look = d.dress(&c);
        assert_eq!(look.tip, "Night night");
        assert!(look.svg().contains("#00FF00"));

        let only: Params = serde_json::from_value(json!({"rules": [{"wear": {"held": "lantern"}}]})).unwrap();
        let look = Duck::build(only).unwrap().unwrap().dress(&conditions(Sky::Snow, -10.0));
        assert_eq!(look.outfit.len(), 1);

        let wrong_slot: Params = serde_json::from_value(json!({"rules": [{"wear": {"head": "boots"}}]})).unwrap();
        assert!(Duck::build(wrong_slot).is_err());
        let bad_colour: Params = serde_json::from_value(json!({"colors": {"duck": "yellow"}})).unwrap();
        assert!(Duck::build(bad_colour).is_err());
        let unknown: Params = serde_json::from_value(json!({"colors": {"hat": "#000000"}})).unwrap();
        assert!(Duck::build(unknown).is_err());
        assert!(Duck::build(serde_json::from_value(json!({"enabled": false})).unwrap()).unwrap().is_none());
    }

    /// Every block, every sky, day and night: all of it must parse and render.
    #[test]
    fn every_block_renders() {
        let skies = [Sky::Clear, Sky::PartlyCloudy, Sky::Cloudy, Sky::Fog, Sky::Drizzle, Sky::Rain, Sky::FreezingRain, Sky::Snow, Sky::Storm];
        let d = duck();
        for (i, sky) in skies.into_iter().enumerate() {
            let mut look = d.dress(&conditions(sky, 5.0));
            look.night = i % 2 == 1;
            look.windy = i % 3 == 0;
            // a different item per slot on each pass, so all of them get drawn
            for item in Item::ALL.into_iter().skip(i % 3).step_by(2) {
                look.outfit.insert(item.slot().unwrap(), item);
            }
            crate::svg::png(&look.svg(), 64, 64).unwrap_or_else(|e| panic!("{sky:?}: {e}"));
        }
        for item in Item::ALL {
            let mut look = d.dress(&conditions(Sky::Clear, 15.0));
            look.outfit.insert(item.slot().unwrap(), item);
            crate::svg::png(&look.svg(), 32, 32).unwrap_or_else(|e| panic!("{item:?}: {e}"));
        }
    }

    /// `DUCK_PREVIEW=/some/dir cargo test duck::tests::preview -- --ignored`
    /// writes one PNG per typical day, to look at the art.
    #[test]
    #[ignore]
    fn preview() {
        let dir = std::path::PathBuf::from(std::env::var("DUCK_PREVIEW").expect("DUCK_PREVIEW=dir"));
        std::fs::create_dir_all(&dir).unwrap();
        let d = duck();
        let days = [
            ("snow", Sky::Snow, -2.0, false, 10.0),
            ("freezing-clear", Sky::Clear, -8.0, false, 5.0),
            ("rain", Sky::Rain, 11.0, false, 12.0),
            ("rain-cold", Sky::Rain, 1.0, false, 12.0),
            ("storm", Sky::Storm, 16.0, false, 40.0),
            ("hot", Sky::Clear, 30.0, false, 5.0),
            ("warm", Sky::PartlyCloudy, 21.0, false, 8.0),
            ("mild-cloudy", Sky::Cloudy, 13.0, false, 8.0),
            ("chilly-windy", Sky::Cloudy, 6.0, false, 42.0),
            ("fog", Sky::Fog, 4.0, false, 3.0),
            ("night-clear", Sky::Clear, 8.0, true, 5.0),
            ("night-snow", Sky::Snow, -3.0, true, 5.0),
        ];
        for (name, sky, feels, night, wind) in days {
            let mut c = conditions(sky, feels);
            c.night = night;
            c.wind_kmh = wind;
            let look = d.dress(&c);
            std::fs::write(dir.join(format!("{name}.png")), crate::svg::png(&look.svg(), 512, 512).unwrap()).unwrap();
            println!("{name}: {:?} — {}", look.outfit, look.tip);
        }
    }

    #[test]
    fn worst_sky_wins() {
        assert!(Sky::of(73) > Sky::of(61));
        assert_eq!(Sky::of(3).max(Sky::of(95)), Sky::Storm);
        assert!(!Sky::of(45).wet() && Sky::of(51).wet());
    }
}
