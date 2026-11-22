//! Forecast from Open-Meteo (free, no key, ECMWF/ICON models): now, the rest
//! of today in a few steps, the next days, and the duck dressed for it.
//!
//! The TV shows today only (now, high/low, rain, sun, the next hours, the
//! duck). `data` keeps the next days for the phone and the web page.

use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::clock::Date;
use crate::duck::{self, Conditions, Duck, Sky};
use crate::error::{Error, Result};
use crate::svg;
use crate::view;
use crate::widget::{self, Ctx, Output, Refresh, View, Widget};

const API: &str = "https://api.open-meteo.com/v1/forecast";
/// Columns the TV has for hours.
const MAX_HOURS: usize = 6;
const MAX_DAYS: usize = 7;
/// Next days on the TV: its "next days" card shows four, five when it is
/// wide (nothing on the board). Every slot is always served (blank past
/// the forecast), so a shorter forecast clears the old ones.
const TV_DAYS: usize = 5;
/// The duck on the TV, in pixels (within Kodi's 540 px texture limit).
const DUCK_PX: u32 = 512;
const DUCK_PNG: &str = "duck-";
const DUCK_SVG: &str = "ducksvg-";

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Params {
    place: String,
    latitude: f64,
    longitude: f64,
    #[serde(default = "default_timezone")]
    timezone: String,
    #[serde(default = "default_every")]
    every_minutes: u64,
    /// The hour columns: every `hours_step` hours, `hours_count` of them.
    #[serde(default = "default_hours_step")]
    hours_step: usize,
    #[serde(default = "default_hours_count")]
    hours_count: usize,
    /// Next days in `data`; the TV shows the first `TV_DAYS` of them.
    #[serde(default = "default_days")]
    days: usize,
    #[serde(default)]
    duck: duck::Params,
}

fn default_timezone() -> String {
    "Europe/Bratislava".into()
}
fn default_every() -> u64 {
    15
}
fn default_hours_step() -> usize {
    3
}
fn default_hours_count() -> usize {
    4
}
fn default_days() -> usize {
    5
}

#[derive(Debug, Clone, Copy, PartialEq)]
struct Layout {
    hours_step: usize,
    hours_count: usize,
    days: usize,
}

pub struct Weather {
    place: String,
    url: String,
    every: Duration,
    layout: Layout,
    duck: Option<Duck>,
}

pub fn build(params: &Value) -> Result<Box<dyn Widget>> {
    let p: Params = widget::params("weather", params)?;
    let layout = Layout {
        hours_step: p.hours_step.clamp(1, 6),
        hours_count: p.hours_count.clamp(1, MAX_HOURS),
        days: p.days.min(MAX_DAYS),
    };
    let duck = Duck::build(p.duck)?;
    let look_ahead = duck.as_ref().map_or(0, |d| d.look_ahead_hours);
    let url = format!(
        "{API}?latitude={}&longitude={}&timezone={}\
         &current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m,is_day\
         &hourly=temperature_2m,apparent_temperature,weather_code,precipitation_probability,is_day\
         &daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset\
         &forecast_hours={}&forecast_days={}",
        p.latitude,
        p.longitude,
        p.timezone.replace('/', "%2F"),
        (layout.hours_step * layout.hours_count).max(look_ahead) + 1,
        layout.days + 1,
    );
    Ok(Box::new(Weather { place: p.place, url, every: Duration::from_secs(p.every_minutes.max(5) * 60), layout, duck }))
}

// --- Open-Meteo's response (only what is used) ---------------------------------

#[derive(Deserialize)]
struct Response {
    current: Current,
    hourly: Hourly,
    daily: Daily,
}

#[derive(Deserialize)]
struct Current {
    temperature_2m: f64,
    apparent_temperature: f64,
    weather_code: u8,
    wind_speed_10m: f64,
    relative_humidity_2m: f64,
    is_day: u8,
}

/// Index 0 is the current hour.
#[derive(Deserialize)]
struct Hourly {
    time: Vec<String>,
    temperature_2m: Vec<Option<f64>>,
    #[serde(default)]
    apparent_temperature: Vec<Option<f64>>,
    weather_code: Vec<Option<u8>>,
    precipitation_probability: Vec<Option<u8>>,
    #[serde(default)]
    is_day: Vec<Option<u8>>,
}

#[derive(Deserialize)]
struct Daily {
    time: Vec<String>,
    weather_code: Vec<Option<u8>>,
    temperature_2m_max: Vec<Option<f64>>,
    temperature_2m_min: Vec<Option<f64>>,
    precipitation_probability_max: Vec<Option<u8>>,
    sunrise: Vec<String>,
    sunset: Vec<String>,
}

// --- what dashd serves ---------------------------------------------------------

#[derive(Serialize, Debug, PartialEq)]
pub struct Forecast {
    pub place: String,
    pub now: Now,
    pub today: Today,
    pub hours: Vec<Hour>,
    pub days: Vec<Day>,
}

#[derive(Serialize, Debug, PartialEq)]
pub struct Now {
    pub temperature: f64,
    pub feels_like: f64,
    pub code: u8,
    pub condition: &'static str,
    pub wind_kmh: f64,
    pub humidity: f64,
    pub is_day: bool,
}

#[derive(Serialize, Debug, PartialEq)]
pub struct Today {
    pub high: Option<f64>,
    pub low: Option<f64>,
    pub rain_chance: Option<u8>,
    pub sunrise: String,
    pub sunset: String,
}

#[derive(Serialize, Debug, PartialEq)]
pub struct Hour {
    pub time: String,
    pub temperature: Option<f64>,
    pub code: Option<u8>,
    pub rain_chance: Option<u8>,
    pub is_day: bool,
}

#[derive(Serialize, Debug, PartialEq)]
pub struct Day {
    pub date: String,
    pub weekday: &'static str,
    pub high: Option<f64>,
    pub low: Option<f64>,
    pub code: Option<u8>,
    pub condition: &'static str,
    pub rain_chance: Option<u8>,
}

/// WMO weather interpretation codes, as Open-Meteo documents them.
pub fn condition(code: u8) -> &'static str {
    match code {
        0 => "Clear",
        1 => "Mostly clear",
        2 => "Partly cloudy",
        3 => "Overcast",
        45 | 48 => "Fog",
        51 | 53 | 55 => "Drizzle",
        56 | 57 => "Freezing drizzle",
        61 => "Light rain",
        63 => "Rain",
        65 => "Heavy rain",
        66 | 67 => "Freezing rain",
        71 => "Light snow",
        73 => "Snow",
        75 => "Heavy snow",
        77 => "Snow grains",
        80 | 81 => "Showers",
        82 => "Heavy showers",
        85 | 86 => "Snow showers",
        95 => "Thunderstorm",
        96 | 99 => "Thunderstorm, hail",
        _ => "",
    }
}

/// The WMO code as an icon of Kodi's stock set (resource.images.weathericons
/// .default, weather.com numbering 0-47): the TV shows icons, not words.
pub fn icon(code: Option<u8>, is_day: bool) -> &'static str {
    let (day, night) = match code {
        Some(0) => ("32", "31"),
        Some(1) => ("34", "33"),
        Some(2) => ("30", "29"),
        Some(3) => ("26", "26"),
        Some(45 | 48) => ("20", "20"),
        Some(51 | 53 | 55) => ("9", "9"),
        Some(56 | 57) => ("8", "8"),
        Some(61) => ("11", "11"),
        Some(63) => ("12", "12"),
        Some(65 | 82) => ("40", "40"),
        Some(66 | 67) => ("10", "10"),
        Some(71) => ("14", "14"),
        Some(73) => ("16", "16"),
        Some(75) => ("42", "42"),
        Some(77) => ("13", "13"),
        Some(80 | 81) => ("39", "45"),
        Some(85 | 86) => ("41", "46"),
        Some(95) => ("4", "47"),
        Some(96 | 99) => ("3", "3"),
        _ => ("na", "na"),
    };
    if is_day { day } else { night }
}

/// "HH:MM" of an ISO local time ("2026-11-22T07:08").
fn clock_time(iso: &str) -> String {
    iso.get(11..16).unwrap_or("").to_owned()
}

fn at<T: Copy>(values: &[Option<T>], i: usize) -> Option<T> {
    values.get(i).copied().flatten()
}

/// What the duck dresses for: the coldest "feels like" and the worst sky
/// from now to `ahead` hours on. An hour's rain or snow counts only from
/// `min_chance` %, so an unlikely shower does not bring the umbrella.
fn conditions(r: &Response, ahead: usize, min_chance: u8) -> Conditions {
    let c = &r.current;
    let h = &r.hourly;
    let span = 0..=ahead.min(h.time.len().saturating_sub(1));
    let mut sky = Sky::of(c.weather_code);
    let mut feels = c.apparent_temperature;
    let mut rain: Option<u8> = None;
    for i in span {
        let chance = at(&h.precipitation_probability, i);
        if let Some(p) = chance {
            rain = Some(rain.map_or(p, |r| r.max(p)));
        }
        if let Some(code) = at(&h.weather_code, i) {
            let hour_sky = Sky::of(code);
            if !hour_sky.wet() || chance.is_none_or(|p| p >= min_chance) {
                sky = sky.max(hour_sky);
            }
        }
        if let Some(t) = at(&h.apparent_temperature, i) {
            feels = feels.min(t);
        }
    }
    Conditions {
        sky,
        night: c.is_day == 0,
        temp: c.temperature_2m,
        feels,
        high: at(&r.daily.temperature_2m_max, 0),
        low: at(&r.daily.temperature_2m_min, 0),
        rain_chance: rain.or_else(|| at(&r.daily.precipitation_probability_max, 0)),
        wind_kmh: c.wind_speed_10m,
    }
}

impl Forecast {
    fn from_response(place: &str, r: Response, layout: Layout) -> Result<Self> {
        let d = &r.daily;
        if d.time.is_empty() || r.hourly.time.is_empty() {
            return Err(Error::upstream("open-meteo: empty forecast"));
        }
        let hours = (1..=layout.hours_count)
            .map(|n| n * layout.hours_step)
            .take_while(|&i| i < r.hourly.time.len())
            .map(|i| Hour {
                time: clock_time(&r.hourly.time[i]),
                temperature: at(&r.hourly.temperature_2m, i),
                code: at(&r.hourly.weather_code, i),
                rain_chance: at(&r.hourly.precipitation_probability, i),
                is_day: at(&r.hourly.is_day, i).is_none_or(|d| d == 1),
            })
            .collect();
        let days = (1..d.time.len().min(layout.days + 1))
            .map(|i| {
                let code = at(&d.weather_code, i);
                Day {
                    date: d.time[i].clone(),
                    weekday: Date::parse(&d.time[i]).map_or("", Date::weekday_short),
                    high: at(&d.temperature_2m_max, i),
                    low: at(&d.temperature_2m_min, i),
                    code,
                    condition: code.map_or("", condition),
                    rain_chance: at(&d.precipitation_probability_max, i),
                }
            })
            .collect();
        let c = r.current;
        Ok(Self {
            place: place.to_owned(),
            now: Now {
                temperature: c.temperature_2m,
                feels_like: c.apparent_temperature,
                code: c.weather_code,
                condition: condition(c.weather_code),
                wind_kmh: c.wind_speed_10m,
                humidity: c.relative_humidity_2m,
                is_day: c.is_day == 1,
            },
            today: Today {
                high: at(&d.temperature_2m_max, 0),
                low: at(&d.temperature_2m_min, 0),
                rain_chance: at(&d.precipitation_probability_max, 0),
                sunrise: d.sunrise.first().map_or_else(String::new, |s| clock_time(s)),
                sunset: d.sunset.first().map_or_else(String::new, |s| clock_time(s)),
            },
            hours,
            days,
        })
    }

    /// Display strings for today and the next days: whole degrees, an icon
    /// per slot, precipitation only when it is worth a glance.
    fn view(&self) -> View {
        let n = &self.now;
        let t = &self.today;
        let mut view = view! {
            "place" => self.place,
            "temp" => deg(Some(n.temperature)),
            "condition" => n.condition,
            "icon" => icon(Some(n.code), n.is_day),
            "feels" => format!("Feels like {}", deg(Some(n.feels_like))),
            "highlow" => format!("{} / {}", deg(t.high), deg(t.low)),
            "rain" => chance(t.rain_chance).map_or_else(String::new, |p| format!("{p} rain")),
            "wind" => format!("Wind {} km/h", n.wind_kmh.round()),
            "sun" => format!("{} – {}", t.sunrise, t.sunset),
        };
        for (i, h) in self.hours.iter().enumerate() {
            let k = i + 1;
            view.insert(format!("hour{k}.time"), h.time.clone());
            view.insert(format!("hour{k}.icon"), icon(h.code, h.is_day).to_owned());
            view.insert(format!("hour{k}.temp"), deg(h.temperature));
            view.insert(format!("hour{k}.rain"), chance(h.rain_chance).unwrap_or_default());
        }
        view.insert("days".into(), if self.days.is_empty() { String::new() } else { "true".into() });
        for k in 1..=TV_DAYS {
            let d = self.days.get(k - 1);
            view.insert(format!("day{k}.name"), d.map_or("", |d| d.weekday).to_owned());
            view.insert(format!("day{k}.icon"), d.map_or("", |d| icon(d.code, true)).to_owned());
            view.insert(format!("day{k}.high"), d.map_or_else(String::new, |d| deg(d.high)));
            view.insert(format!("day{k}.low"), d.map_or_else(String::new, |d| deg(d.low)));
            view.insert(format!("day{k}.rain"), d.and_then(|d| chance(d.rain_chance)).unwrap_or_default());
        }
        view
    }
}

/// "3°", "-2°"; never "-0°".
fn deg(t: Option<f64>) -> String {
    t.map_or_else(|| "–".into(), |t| format!("{}°", (t.round() as i64)))
}

/// "40 %" from 20 % up (rain or snow alike); below that, nothing.
fn chance(chance: Option<u8>) -> Option<String> {
    chance.filter(|&p| p >= 20).map(|p| format!("{p} %"))
}

impl Weather {
    /// The duck as files named by their content: the PNG for Kodi, the SVG
    /// for everyone else. Rendered only when the picture changes.
    fn draw_duck(&self, ctx: &Ctx, conditions: &Conditions, duck: &Duck) -> Result<(Value, View)> {
        let look = duck.dress(conditions);
        let source = look.svg();
        let hash = svg::content_hash(&source);
        let (png, svg_name) = (format!("{DUCK_PNG}{hash}.png"), format!("{DUCK_SVG}{hash}.svg"));
        if !ctx.assets.exists(&png) {
            ctx.assets.put(&png, &svg::png(&source, DUCK_PX, DUCK_PX)?)?;
            ctx.assets.put(&svg_name, source.as_bytes())?;
            ctx.assets.prune(DUCK_PNG, &png)?;
            ctx.assets.prune(DUCK_SVG, &svg_name)?;
        }
        let mut data = look.data();
        data["image"] = json!(format!("/v1/assets/{png}"));
        data["svg"] = json!(format!("/v1/assets/{svg_name}"));
        let view = view! {
            "duck" => format!("/v1/assets/{png}"),
            "tip" => look.tip,
        };
        Ok((data, view))
    }
}

impl Widget for Weather {
    fn kind(&self) -> &'static str {
        "weather"
    }

    fn next_refresh(&self, _ctx: &Ctx) -> Option<Duration> {
        Some(self.every)
    }

    fn refresh(&self, ctx: &Ctx) -> Result<Refresh> {
        let response: Response = serde_json::from_value(ctx.http.get_json(&self.url)?)?;
        let conditions = self.duck.as_ref().map(|d| (conditions(&response, d.look_ahead_hours, d.min_chance), d));
        let forecast = Forecast::from_response(&self.place, response, self.layout)?;
        let mut data = json!(forecast);
        let mut view = forecast.view();
        // The duck is a bonus: if it cannot be drawn, the forecast still
        // shows (the skin hides the duck and its bubble when empty).
        if let Some((c, duck)) = conditions {
            match self.draw_duck(ctx, &c, duck) {
                Ok((duck_data, duck_view)) => {
                    data["duck"] = duck_data;
                    view.extend(duck_view);
                }
                Err(err) => crate::log::warn(&format!("weather: the duck: {err}")),
            }
        }
        Ok(Refresh::Changed(Output::new(data, view)))
    }
}

/// An Open-Meteo answer, for tests here and the skin contract test.
#[cfg(test)]
pub const SAMPLE: &str = r#"{"current":{"time":"2026-11-22T02:00","temperature_2m":2.0,"apparent_temperature":-0.4,"weather_code":0,"wind_speed_10m":6.2,"relative_humidity_2m":75,"is_day":0},
      "hourly":{"time":["2026-11-22T02:00","2026-11-22T03:00","2026-11-22T04:00","2026-11-22T05:00","2026-11-22T06:00","2026-11-22T07:00","2026-11-22T08:00"],
                "temperature_2m":[2.0,1.7,1.7,2.8,3.0,3.2,3.5],"apparent_temperature":[-0.4,-1.2,-1.6,0.1,0.5,0.8,1.0],
                "weather_code":[0,2,61,3,71,3,3],"precipitation_probability":[0,0,10,30,60,null,0]},
      "daily":{"time":["2026-11-22","2026-11-23","2026-11-24"],"weather_code":[3,3,77],"temperature_2m_max":[6.0,4.2,3.7],
               "temperature_2m_min":[1.6,0.7,-0.6],"precipitation_probability_max":[5,3,75],
               "sunrise":["2026-11-22T07:08","2026-11-23T07:09","2026-11-24T07:11"],"sunset":["2026-11-22T16:06","2026-11-23T16:05","2026-11-24T16:04"]}}"#;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testing::{FakeFetch, ctx_at};

    const LAYOUT: Layout = Layout { hours_step: 3, hours_count: 4, days: 5 };

    fn response() -> Response {
        serde_json::from_str(SAMPLE).unwrap()
    }

    fn forecast() -> Forecast {
        Forecast::from_response("Staré Mesto", response(), LAYOUT).unwrap()
    }

    #[test]
    fn picks_hour_steps_and_future_days() {
        let f = forecast();
        let times: Vec<_> = f.hours.iter().map(|h| h.time.as_str()).collect();
        assert_eq!(times, ["05:00", "08:00"]);
        assert_eq!(f.days.len(), 2);
        assert_eq!(f.days[0].weekday, "Mon");
        assert_eq!(f.days[1].condition, "Snow grains");
        assert_eq!(f.today.sunset, "16:06");
    }

    #[test]
    fn view_is_display_ready() {
        let v = forecast().view();
        assert_eq!(v["temp"], "2°");
        assert_eq!(v["icon"], "31"); // clear, at night
        assert_eq!(v["feels"], "Feels like 0°");
        assert_eq!(v["highlow"], "6° / 2°");
        assert_eq!(v["rain"], "");
        assert_eq!(v["sun"], "07:08 – 16:06");
        assert_eq!(v["hour1.rain"], "30 %");
        assert_eq!(v["hour1.icon"], "26");
        assert_eq!(v["hour2.temp"], "4°");
        assert_eq!(v["days"], "true");
        assert_eq!(v["day1.name"], "Mon");
        assert_eq!(v["day1.icon"], "26");
        assert_eq!((v["day1.high"].as_str(), v["day1.low"].as_str()), ("4°", "1°"));
        assert_eq!(v["day1.rain"], "");
        assert_eq!(v["day2.rain"], "75 %");
        // past the forecast: blank, so a shorter forecast clears the slot
        assert_eq!(v["day3.name"], "");
        assert_eq!(v["day4.icon"], "");
    }

    #[test]
    fn the_duck_looks_ahead_but_ignores_unlikely_rain() {
        let r = response();
        // 02:00-05:00: the rain at 04:00 is only 10 %, so it does not count;
        // the overcast at 05:00 does.
        let c = conditions(&r, 3, 30);
        assert_eq!(c.sky, Sky::Cloudy);
        assert_eq!(c.feels, -1.6);
        assert_eq!(c.rain_chance, Some(30));
        assert!(c.night);
        // 06:00 brings likely snow.
        assert_eq!(conditions(&r, 4, 30).sky, Sky::Snow);
        assert_eq!(conditions(&r, 3, 5).sky, Sky::Rain);
    }

    #[test]
    fn refresh_draws_the_duck_once_per_look() {
        let fake = FakeFetch::new().json("open-meteo", serde_json::from_str(SAMPLE).unwrap());
        let ctx = ctx_at(fake, Date { year: 2026, month: 11, day: 22 });
        let w = build(&json!({"place": "Here", "latitude": 48.1, "longitude": 17.1, "duck": {"look_ahead_hours": 4}})).unwrap();
        let Refresh::Changed(out) = w.refresh(&ctx).unwrap() else { panic!() };
        let image = out.view["duck"].clone();
        assert!(image.starts_with("/v1/assets/duck-") && image.ends_with(".png"), "{image}");
        assert!(ctx.assets.exists(image.trim_start_matches("/v1/assets/")));
        assert_eq!(out.view["tip"], "Snow! 2° to 6°. Boots on");
        assert_eq!(out.data["duck"]["sky"], "snow");
        assert_eq!(out.data["duck"]["outfit"]["coat"], "winter_jacket");
        let svg = out.data["duck"]["svg"].as_str().unwrap().trim_start_matches("/v1/assets/").to_owned();
        assert!(ctx.assets.exists(&svg));
        // the same weather again: the same files
        let Refresh::Changed(again) = w.refresh(&ctx).unwrap() else { panic!() };
        assert_eq!(again.view["duck"], image);

        let off = build(&json!({"place": "Here", "latitude": 48.1, "longitude": 17.1, "duck": {"enabled": false}})).unwrap();
        let Refresh::Changed(out) = off.refresh(&ctx).unwrap() else { panic!() };
        assert!(!out.view.contains_key("duck"));
    }

    #[test]
    fn icons_follow_day_and_night() {
        assert_eq!(icon(Some(0), true), "32");
        assert_eq!(icon(Some(0), false), "31");
        assert_eq!(icon(Some(42), true), "na");
        assert_eq!(icon(None, true), "na");
    }

    #[test]
    fn unknown_codes_have_no_label() {
        assert_eq!(condition(42), "");
        assert_eq!(condition(95), "Thunderstorm");
    }

    #[test]
    fn params_are_checked() {
        assert!(build(&json!({"place": "x", "latitude": 1, "longitude": 2, "hours": 3})).is_err());
        assert!(build(&json!({"place": "x", "latitude": 1, "longitude": 2, "duck": {"colors": {"duck": "red"}}})).is_err());
    }
}
