"""Tests for the Open-Meteo-backed weather lookup (get_weather tool)."""

from datetime import datetime, timedelta
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from src.claude import weather_tool


def _fake_response(payload: Dict[str, Any]) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


def _weather_payload(date_str: str) -> Dict[str, Any]:
    hours = [f"{date_str}T{h:02d}:00" for h in range(24)]
    return {
        "daily": {
            "temperature_2m_max": [27.3],
            "temperature_2m_min": [17.8],
        },
        "hourly": {
            "time": hours,
            "temperature_2m": [15.0 + h * 0.3 for h in range(24)],
        },
    }


class FakeAsyncClient:
    """Stand-in for httpx.AsyncClient recording calls and returning canned data."""

    calls: list = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def get(self, url: str, params: Dict[str, Any]) -> MagicMock:
        FakeAsyncClient.calls.append((url, params))
        if url == weather_tool.GEOCODING_URL:
            return _fake_response(
                {
                    "results": [
                        {
                            "latitude": 52.52,
                            "longitude": 13.405,
                            "name": "Berlin",
                            "admin1": "Berlin",
                            "country": "Germany",
                        }
                    ]
                }
            )
        return _fake_response(_weather_payload(params["start_date"]))


@pytest.fixture(autouse=True)
def patch_httpx(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeAsyncClient.calls = []
    monkeypatch.setattr(weather_tool.httpx, "AsyncClient", FakeAsyncClient)
    weather_tool._geocode_cache.clear()


class TestDefaultLocation:
    async def test_omitted_location_skips_geocoding(self) -> None:
        today = datetime.now().date().isoformat()

        result = await weather_tool.get_weather_for(None, today)

        assert result["location"] == weather_tool.DEFAULT_LOCATION
        assert result["latitude"] == weather_tool.DEFAULT_LAT
        # Only the weather call, no geocoding round-trip.
        assert all(u != weather_tool.GEOCODING_URL for u, _ in FakeAsyncClient.calls)


class TestGeocodedLocation:
    async def test_named_location_is_geocoded_and_cached(self) -> None:
        today = datetime.now().date().isoformat()

        result = await weather_tool.get_weather_for("Berlin", today)
        await weather_tool.get_weather_for("berlin", today)  # cache hit, diff case

        assert "Berlin" in result["location"]
        geocode_calls = [
            u for u, _ in FakeAsyncClient.calls if u == weather_tool.GEOCODING_URL
        ]
        assert len(geocode_calls) == 1

    async def test_unknown_location_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def get_no_results(
            self: Any, url: str, params: Dict[str, Any]
        ) -> MagicMock:
            return _fake_response({"results": []})

        monkeypatch.setattr(FakeAsyncClient, "get", get_no_results)

        with pytest.raises(ValueError, match="no location found"):
            await weather_tool.get_weather_for("Nowhereville", None)


class TestDateRouting:
    async def test_recent_date_uses_forecast_endpoint(self) -> None:
        yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()

        result = await weather_tool.get_weather_for(None, yesterday)

        assert result["source"] == "forecast"
        assert any(u == weather_tool.FORECAST_URL for u, _ in FakeAsyncClient.calls)

    async def test_old_date_uses_archive_endpoint(self) -> None:
        long_ago = (datetime.now().date() - timedelta(days=30)).isoformat()

        result = await weather_tool.get_weather_for(None, long_ago)

        assert result["source"] == "archive"
        assert any(u == weather_tool.ARCHIVE_URL for u, _ in FakeAsyncClient.calls)

    async def test_no_date_defaults_to_today(self) -> None:
        result = await weather_tool.get_weather_for(None, None)

        assert result["date"] == datetime.now().date().isoformat()
        assert result["source"] == "forecast"


class TestPayloadShape:
    async def test_day_night_and_hourly_extracted(self) -> None:
        today = datetime.now().date().isoformat()

        result = await weather_tool.get_weather_for(None, today)

        assert result["day_max_c"] == 27.3
        assert result["night_min_c"] == 17.8
        assert set(result["hourly_c"]) == set(weather_tool._HOURS_OF_INTEREST)
