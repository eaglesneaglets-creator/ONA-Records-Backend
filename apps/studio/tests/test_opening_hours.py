"""
ONA's opening hours: "24/7, by prior booking only". The settings default; the
rest of the suite runs at 09:00–21:00 (conftest.py) because that is what it
was written against.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from rest_framework import serializers

from apps.studio.availability import availability, day_slots
from apps.studio.brokering import outside_hours
from apps.studio.serializers import check_closing, check_start

pytestmark = pytest.mark.django_db
TZ = ZoneInfo('Africa/Accra')


@pytest.fixture(autouse=True)
def all_day(settings):
    settings.STUDIO_OPEN_HOUR, settings.STUDIO_CLOSE_HOUR = 0, 24


def at(hour, days=3):
    d = date.today() + timedelta(days=days)
    return datetime.combine(d, datetime.min.time(), tzinfo=TZ) + timedelta(hours=hour)


def test_a_day_has_24_slots_from_midnight():
    slots = day_slots(date.today() + timedelta(days=2))
    assert len(slots) == 24
    assert slots[0][0].hour == 0 and slots[-1][1].hour == 0  # ends at the next midnight


def test_calendar_offers_the_small_hours():
    d = date.today() + timedelta(days=2)
    assert len(availability(start=d, end=d)) == 24


def test_a_session_can_start_at_two_in_the_morning():
    assert check_start(at(2)) == at(2)


def test_a_session_may_run_past_midnight():
    assert check_closing(at(22), 4) == at(26)


def test_staff_are_never_warned_about_hours():
    assert outside_hours(at(23), at(27)) is False


def test_settings_still_allow_fixed_hours(settings):
    settings.STUDIO_OPEN_HOUR, settings.STUDIO_CLOSE_HOUR = 9, 21
    with pytest.raises(serializers.ValidationError):
        check_start(at(2))
    with pytest.raises(serializers.ValidationError):
        check_closing(at(20), 2)
