//! Local calendar time without a date-time crate: the system's `localtime_r`
//! (TZ and /etc/localtime, so the Pi's Europe/Bratislava) plus civil-date
//! arithmetic for weekdays and day numbers.

use std::fmt;
use std::time::{SystemTime, UNIX_EPOCH};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub struct Date {
    pub year: i32,
    pub month: u32,
    pub day: u32,
}

impl Date {
    /// "2026-11-22" (also accepts a trailing "T..." time, as Open-Meteo sends).
    pub fn parse(text: &str) -> Option<Self> {
        let date = text.get(..10)?;
        let mut parts = date.split('-');
        let year = parts.next()?.parse().ok()?;
        let month = parts.next()?.parse().ok()?;
        let day = parts.next()?.parse().ok()?;
        ((1..=12).contains(&month) && (1..=31).contains(&day)).then_some(Self { year, month, day })
    }

    /// Days since 1970-01-01 (Howard Hinnant's days_from_civil).
    pub fn days_since_epoch(self) -> i64 {
        let y = i64::from(self.year) - i64::from(self.month <= 2);
        let era = y.div_euclid(400);
        let yoe = y - era * 400;
        let m = i64::from(self.month);
        let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + i64::from(self.day) - 1;
        let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
        era * 146_097 + doe - 719_468
    }

    /// The date `days_since_epoch` days after 1970-01-01 (Hinnant's civil_from_days).
    pub fn from_days(days: i64) -> Self {
        let z = days + 719_468;
        let era = z.div_euclid(146_097);
        let doe = z - era * 146_097;
        let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
        let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
        let mp = (5 * doy + 2) / 153;
        let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
        let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
        let year = (yoe + era * 400 + i64::from(month <= 2)) as i32;
        Self { year, month, day }
    }

    pub fn add_days(self, n: i64) -> Self {
        Self::from_days(self.days_since_epoch() + n)
    }

    /// 0 = Monday ... 6 = Sunday.
    pub fn weekday(self) -> u32 {
        // 1970-01-01 was a Thursday (3).
        (self.days_since_epoch() + 3).rem_euclid(7) as u32
    }

    pub fn weekday_short(self) -> &'static str {
        ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][self.weekday() as usize]
    }
}

impl fmt::Display for Date {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{:04}-{:02}-{:02}", self.year, self.month, self.day)
    }
}

/// What "now" is, in local time.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LocalNow {
    pub unix: u64,
    pub date: Date,
    /// Local wall-clock time: seconds since local midnight (0..86_399).
    pub seconds_of_day: u32,
    /// Local time minus UTC, in seconds (+3600 for CET, +7200 for CEST).
    pub utc_offset: i32,
    pub seconds_to_midnight: u64,
}

#[cfg(test)]
impl LocalNow {
    /// A given local wall-clock time.
    pub fn at(date: Date, seconds_of_day: u32, utc_offset: i32) -> Self {
        let unix = date.days_since_epoch() * 86_400 + i64::from(seconds_of_day) - i64::from(utc_offset);
        Self {
            unix: unix.max(0) as u64,
            date,
            seconds_of_day,
            utc_offset,
            seconds_to_midnight: 86_400u64.saturating_sub(u64::from(seconds_of_day)).max(1),
        }
    }
}

/// Source of time, so widgets with daily behaviour can be tested.
pub trait Clock: Send + Sync {
    fn now(&self) -> LocalNow;
}

pub struct SystemClock;

impl Clock for SystemClock {
    fn now(&self) -> LocalNow {
        let unix = SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_secs());
        let t = unix as libc::time_t;
        // SAFETY: localtime_r only writes the tm we own; a zeroed tm is valid.
        let mut tm: libc::tm = unsafe { std::mem::zeroed() };
        let ok = unsafe { !libc::localtime_r(&t, &mut tm).is_null() };
        if !ok {
            return LocalNow {
                unix,
                date: Date { year: 1970, month: 1, day: 1 },
                seconds_of_day: 0,
                utc_offset: 0,
                seconds_to_midnight: 86_400,
            };
        }
        let elapsed = (tm.tm_hour * 3600 + tm.tm_min * 60 + tm.tm_sec).clamp(0, 86_399) as u32;
        LocalNow {
            unix,
            date: Date { year: tm.tm_year + 1900, month: (tm.tm_mon + 1) as u32, day: tm.tm_mday as u32 },
            seconds_of_day: elapsed,
            utc_offset: tm.tm_gmtoff as i32,
            // DST days are 23 or 25 h; being a few seconds early is fine,
            // the daily widgets check the date again when they wake.
            seconds_to_midnight: 86_400u64.saturating_sub(u64::from(elapsed)).max(1),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_dates_and_datetimes() {
        assert_eq!(Date::parse("2026-11-22"), Some(Date { year: 2026, month: 11, day: 22 }));
        assert_eq!(Date::parse("2026-11-22T07:08"), Some(Date { year: 2026, month: 11, day: 22 }));
        assert_eq!(Date::parse("2026-13-01"), None);
        assert_eq!(Date::parse("nope"), None);
    }

    #[test]
    fn epoch_days_and_weekdays() {
        assert_eq!(Date { year: 1970, month: 1, day: 1 }.days_since_epoch(), 0);
        assert_eq!(Date { year: 2000, month: 3, day: 1 }.days_since_epoch(), 11_017);
        // 2026-11-22 is a Sunday.
        let d = Date { year: 2026, month: 11, day: 22 };
        assert_eq!(d.weekday_short(), "Sun");
        assert_eq!(d.to_string(), "2026-11-22");
    }

    #[test]
    fn days_round_trip_across_months_years_and_leap_days() {
        for days in [-1, 0, 59, 11_016, 11_017, 20_779, 20_780, 21_000] {
            assert_eq!(Date::from_days(days).days_since_epoch(), days);
        }
        let d = Date { year: 2028, month: 2, day: 28 };
        assert_eq!(d.add_days(1), Date { year: 2028, month: 2, day: 29 });
        assert_eq!(d.add_days(2), Date { year: 2028, month: 3, day: 1 });
        assert_eq!(Date { year: 2026, month: 12, day: 31 }.add_days(1), Date { year: 2027, month: 1, day: 1 });
        assert_eq!(Date { year: 2026, month: 1, day: 1 }.add_days(-1), Date { year: 2025, month: 12, day: 31 });
    }

    #[test]
    fn local_now_at_a_wall_clock_time() {
        let now = LocalNow::at(Date { year: 2026, month: 11, day: 22 }, 23 * 3600, 3600);
        assert_eq!(now.seconds_to_midnight, 3600);
        assert_eq!(now.unix % 86_400, 22 * 3600); // 23:00 CET is 22:00 UTC
    }
}
