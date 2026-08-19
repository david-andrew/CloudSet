from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone


def _julian_day(moment: datetime) -> float:
    return moment.timestamp() / 86400.0 + 2440587.5


def solar_position(moment: datetime, latitude: float, longitude: float) -> tuple[float, float]:
    """Return approximate solar elevation and azimuth in degrees.

    NOAA-style equations are accurate enough for forecast alignment and avoid a
    heavyweight astronomy dependency on the Pi.
    """
    moment = moment.astimezone(timezone.utc)
    jd = _julian_day(moment)
    century = (jd - 2451545.0) / 36525.0
    geom_long = (280.46646 + century * (36000.76983 + century * 0.0003032)) % 360
    geom_anom = 357.52911 + century * (35999.05029 - 0.0001537 * century)
    ecc = 0.016708634 - century * (0.000042037 + 0.0000001267 * century)
    anom = math.radians(geom_anom)
    center = (
        math.sin(anom) * (1.914602 - century * (0.004817 + 0.000014 * century))
        + math.sin(2 * anom) * (0.019993 - 0.000101 * century)
        + math.sin(3 * anom) * 0.000289
    )
    true_long = geom_long + center
    omega = 125.04 - 1934.136 * century
    apparent_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    mean_obliq = 23 + (26 + ((21.448 - century * (46.815 + century * (0.00059 - century * 0.001813)))) / 60) / 60
    obliq = mean_obliq + 0.00256 * math.cos(math.radians(omega))
    decl = math.asin(math.sin(math.radians(obliq)) * math.sin(math.radians(apparent_long)))
    y = math.tan(math.radians(obliq / 2)) ** 2
    eq_time = 4 * math.degrees(
        y * math.sin(2 * math.radians(geom_long))
        - 2 * ecc * math.sin(anom)
        + 4 * ecc * y * math.sin(anom) * math.cos(2 * math.radians(geom_long))
        - 0.5 * y * y * math.sin(4 * math.radians(geom_long))
        - 1.25 * ecc * ecc * math.sin(2 * anom)
    )
    minutes = moment.hour * 60 + moment.minute + moment.second / 60
    true_solar = (minutes + eq_time + 4 * longitude) % 1440
    hour_angle = true_solar / 4 - 180
    lat = math.radians(latitude)
    ha = math.radians(hour_angle)
    cos_zenith = max(-1.0, min(1.0, math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(ha)))
    zenith = math.acos(cos_zenith)
    elevation = 90 - math.degrees(zenith)
    azimuth = (math.degrees(math.atan2(math.sin(ha), math.cos(ha) * math.sin(lat) - math.tan(decl) * math.cos(lat))) + 180) % 360
    return elevation, azimuth


def sunset_utc(day: date, latitude: float, longitude: float) -> datetime | None:
    """Find sunset for the requested local-solar date by bisection.

    Starting at local-solar midnight matters west of Greenwich: the sunset for
    a North American civil date can occur after midnight on the next UTC date.
    """
    start = datetime.combine(day, time.min, tzinfo=timezone.utc) - timedelta(hours=longitude / 15)
    samples = [start + timedelta(minutes=10 * i) for i in range(145)]
    values = [solar_position(t, latitude, longitude)[0] + 0.833 for t in samples]
    bracket: tuple[datetime, datetime] | None = None
    for first, second, a, b in zip(samples, samples[1:], values, values[1:]):
        if a >= 0 > b:
            bracket = first, second
            break
    if bracket is None:
        return None
    low, high = bracket
    for _ in range(18):
        mid = low + (high - low) / 2
        if solar_position(mid, latitude, longitude)[0] + 0.833 >= 0:
            low = mid
        else:
            high = mid
    return low + (high - low) / 2
