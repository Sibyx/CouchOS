//! Sunrise and sunset from latitude and longitude, computed here (the
//! sunrise equation, within a few minutes at mid latitudes), so a widget
//! can plan around them without asking a web API or another widget.

use crate::clock::Date;

/// Local sunrise and sunset on `date`, in seconds since local midnight, for
/// a place at `latitude`/`longitude` (degrees, north and east positive) and a
/// local time `utc_offset` seconds ahead of UTC. `None` when the sun stays up
/// or stays down all day (polar summer and winter).
pub fn rise_and_set(date: Date, latitude: f64, longitude: f64, utc_offset: i32) -> Option<(i64, i64)> {
    // Days since J2000.0 at local noon of the date.
    let julian_noon = date.days_since_epoch() as f64 + 2_440_588.0;
    let n = julian_noon - 2_451_545.0 + 0.0008;
    let mean_solar_noon = n - longitude / 360.0;
    let anomaly = (357.5291 + 0.985_600_28 * mean_solar_noon).rem_euclid(360.0).to_radians();
    let center = 1.9148 * anomaly.sin() + 0.0200 * (2.0 * anomaly).sin() + 0.0003 * (3.0 * anomaly).sin();
    let ecliptic = (anomaly.to_degrees() + center + 180.0 + 102.9372).rem_euclid(360.0).to_radians();
    let transit = 2_451_545.0 + mean_solar_noon + 0.0053 * anomaly.sin() - 0.0069 * (2.0 * ecliptic).sin();
    let declination = (ecliptic.sin() * 23.4397_f64.to_radians().sin()).asin();
    let phi = latitude.to_radians();
    // -0.833°: the sun's radius plus refraction at the horizon.
    let cos_hour = ((-0.833_f64).to_radians().sin() - phi.sin() * declination.sin()) / (phi.cos() * declination.cos());
    if !(-1.0..=1.0).contains(&cos_hour) {
        return None;
    }
    let half_day = cos_hour.acos().to_degrees() / 360.0;
    let local = |julian: f64| {
        let unix = (julian - 2_440_587.5) * 86_400.0;
        unix.round() as i64 + i64::from(utc_offset) - date.days_since_epoch() * 86_400
    };
    Some((local(transit - half_day), local(transit + half_day)))
}

#[cfg(test)]
mod tests {
    use super::*;

    const BRATISLAVA: (f64, f64) = (48.1437, 17.1066);

    fn hm(seconds: i64) -> String {
        format!("{:02}:{:02}", seconds / 3600, seconds % 3600 / 60)
    }

    #[test]
    fn bratislava_through_the_year() {
        // Reference: Open-Meteo's daily sunrise/sunset for these coordinates,
        // all at UTC+1 (fetched 2026-11-22). Ours run 1-3 min later at
        // sunset (two approximations); plenty for dimming a TV.
        let cases = [
            (Date { year: 2026, month: 8, day: 21 }, 3600, "04:54", "18:54"),
            (Date { year: 2026, month: 9, day: 22 }, 3600, "05:38", "17:49"),
            (Date { year: 2026, month: 11, day: 22 }, 3600, "07:08", "16:06"),
            (Date { year: 2026, month: 12, day: 7 }, 3600, "07:27", "15:58"),
        ];
        for (date, offset, rise, set) in cases {
            let (r, s) = rise_and_set(date, BRATISLAVA.0, BRATISLAVA.1, offset).unwrap();
            let near = |got: i64, want: &str| {
                let (h, m) = want.split_once(':').unwrap();
                let want = h.parse::<i64>().unwrap() * 3600 + m.parse::<i64>().unwrap() * 60;
                (got - want).abs() <= 240
            };
            assert!(near(r, rise), "{date} sunrise {} (want {rise})", hm(r));
            assert!(near(s, set), "{date} sunset {} (want {set})", hm(s));
        }
    }

    #[test]
    fn polar_days_and_nights_have_none() {
        assert_eq!(rise_and_set(Date { year: 2026, month: 6, day: 21 }, 78.2, 15.6, 7200), None);
        assert_eq!(rise_and_set(Date { year: 2026, month: 12, day: 21 }, 78.2, 15.6, 3600), None);
    }
}
