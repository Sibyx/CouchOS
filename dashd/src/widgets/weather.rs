//! Forecast from Open-Meteo (free, no key, ECMWF/ICON models): now, the next
//! twelve hours in two-hour steps, and the next five days.

use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::clock::Date;
use crate::error::{Error, Result};
use crate::view;
use crate::widget::{self, Ctx, Output, Refresh, View, Widget};

const API: &str = "https://api.open-meteo.com/v1/forecast";
const HOUR_STEP: usize = 2;
const HOUR_SLOTS: usize = 6;
const DAY_SLOTS: usize = 5;

#[derive(Deserialize)]
struct Params {
    place: String,
    latitude: f64,
    longitude: f64,
    #[serde(default = "default_timezone")]
    timezone: String,
    #[serde(default = "default_every")]
    every_minutes: u64,
}

fn default_timezone() -> String {
    "Europe/Bratislava".into()
}

fn default_every() -> u64 {
    15
}

pub struct Weather {
    place: String,
    url: String,
    every: Duration,
}

pub fn build(params: &Value) -> Result<Box<dyn Widget>> {
    let p: Params = widget::params("weather", params)?;
    let url = format!(
        "{API}?latitude={}&longitude={}&timezone={}\
         &current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m,is_day\
         &hourly=temperature_2m,weather_code,precipitation_probability,is_day\
         &daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset\
         &forecast_hours={}&forecast_days={}",
        p.latitude,
        p.longitude,
        p.timezone.replace('/', "%2F"),
        HOUR_STEP * HOUR_SLOTS + 1,
        DAY_SLOTS + 1,
    );
    Ok(Box::new(Weather { place: p.place, url, every: Duration::from_secs(p.every_minutes.max(5) * 60) }))
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

#[derive(Deserialize)]
struct Hourly {
    time: Vec<String>,
    temperature_2m: Vec<Option<f64>>,
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

impl Forecast {
    fn from_response(place: &str, r: Response) -> Result<Self> {
        let d = &r.daily;
        if d.time.is_empty() || r.hourly.time.is_empty() {
            return Err(Error::upstream("open-meteo: empty forecast"));
        }
        let hours = (1..=HOUR_SLOTS)
            .map(|n| n * HOUR_STEP)
            .take_while(|&i| i < r.hourly.time.len())
            .map(|i| Hour {
                time: clock_time(&r.hourly.time[i]),
                temperature: at(&r.hourly.temperature_2m, i),
                code: at(&r.hourly.weather_code, i),
                rain_chance: at(&r.hourly.precipitation_probability, i),
                is_day: at(&r.hourly.is_day, i).is_none_or(|d| d == 1),
            })
            .collect();
        let days = (1..d.time.len().min(DAY_SLOTS + 1))
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

    /// Display strings: whole degrees, an icon per slot, precipitation only
    /// when it is worth a glance. Three lines for "now", then two rows.
    fn view(&self) -> View {
        let n = &self.now;
        let t = &self.today;
        let mut summary = format!("Feels like {}  ·  High {}  ·  Low {}", deg(Some(n.feels_like)), deg(t.high), deg(t.low));
        if let Some(p) = chance(t.rain_chance) {
            summary.push_str(&format!("  ·  Precip. {p}"));
        }
        let mut view = view! {
            "place" => self.place,
            "temp" => deg(Some(n.temperature)),
            "condition" => n.condition,
            "icon" => icon(Some(n.code), n.is_day),
            "summary" => summary,
            "detail" => format!(
                "Wind {} km/h  ·  Humidity {} %  ·  Sun {} – {}",
                n.wind_kmh.round(), n.humidity.round(), t.sunrise, t.sunset
            ),
        };
        for (i, h) in self.hours.iter().enumerate() {
            let k = i + 1;
            view.insert(format!("hour{k}.time"), h.time.clone());
            view.insert(format!("hour{k}.icon"), icon(h.code, h.is_day).to_owned());
            view.insert(format!("hour{k}.temp"), deg(h.temperature));
            view.insert(format!("hour{k}.rain"), chance(h.rain_chance).unwrap_or_default());
        }
        for (i, d) in self.days.iter().enumerate() {
            let k = i + 1;
            view.insert(format!("day{k}.name"), d.weekday.to_owned());
            view.insert(format!("day{k}.icon"), icon(d.code, true).to_owned());
            view.insert(format!("day{k}.high"), deg(d.high));
            view.insert(format!("day{k}.low"), deg(d.low));
            view.insert(format!("day{k}.rain"), chance(d.rain_chance).unwrap_or_default());
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

impl Widget for Weather {
    fn kind(&self) -> &'static str {
        "weather"
    }

    fn next_refresh(&self, _ctx: &Ctx) -> Option<Duration> {
        Some(self.every)
    }

    fn refresh(&self, ctx: &Ctx) -> Result<Refresh> {
        let response: Response = serde_json::from_value(ctx.http.get_json(&self.url)?)?;
        let forecast = Forecast::from_response(&self.place, response)?;
        Ok(Refresh::Changed(Output::new(json!(forecast), forecast.view())))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    pub const SAMPLE: &str = r#"{"current":{"time":"2026-11-22T02:00","temperature_2m":2.0,"apparent_temperature":-0.4,"weather_code":0,"wind_speed_10m":6.2,"relative_humidity_2m":75,"is_day":0},
      "hourly":{"time":["2026-11-22T02:00","2026-11-22T03:00","2026-11-22T04:00","2026-11-22T05:00","2026-11-22T06:00"],
                "temperature_2m":[2.0,1.7,1.7,2.8,3.0],"weather_code":[0,2,3,3,61],"precipitation_probability":[0,0,10,30,null]},
      "daily":{"time":["2026-11-22","2026-11-23","2026-11-24"],"weather_code":[3,3,77],"temperature_2m_max":[6.0,4.2,3.7],
               "temperature_2m_min":[1.6,0.7,-0.6],"precipitation_probability_max":[5,3,75],
               "sunrise":["2026-11-22T07:08","2026-11-23T07:09","2026-11-24T07:11"],"sunset":["2026-11-22T16:06","2026-11-23T16:05","2026-11-24T16:04"]}}"#;

    fn forecast() -> Forecast {
        Forecast::from_response("Staré Mesto", serde_json::from_str(SAMPLE).unwrap()).unwrap()
    }

    #[test]
    fn picks_two_hour_steps_and_future_days() {
        let f = forecast();
        let times: Vec<_> = f.hours.iter().map(|h| h.time.as_str()).collect();
        assert_eq!(times, ["04:00", "06:00"]);
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
        assert_eq!(v["summary"], "Feels like 0°  ·  High 6°  ·  Low 2°");
        assert_eq!(v["detail"], "Wind 6 km/h  ·  Humidity 75 %  ·  Sun 07:08 – 16:06");
        assert_eq!(v["hour1.rain"], "");
        assert_eq!(v["hour1.icon"], "26");
        assert_eq!(v["hour2.temp"], "3°");
        assert_eq!(v["hour2.icon"], "11");
        assert_eq!(v["day2.high"], "4°");
        assert_eq!(v["day2.low"], "-1°");
        assert_eq!(v["day2.icon"], "13");
        assert_eq!(v["day2.rain"], "75 %");
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
}
