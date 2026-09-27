"""Binance intraday klines: read from the archive once, cached per month, as bars."""

from __future__ import annotations

import io
import zipfile

import src.data.binance_archive as archive


def _zip(rows: list[str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as handle:
        handle.writestr("BTCUSDT-3m-2026-06.csv", "\n".join(rows) + "\n")
    return buffer.getvalue()


def test_monthly_klines_are_downloaded_once_and_read_back_as_bars(tmp_path, monkeypatch) -> None:
    rows = ["open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore",
            "1780272000000,100,101,99,100.5,2,1780272179999,201,10,1,100,0",
            "1780272180000000,100.5,102,100,101.5,3,1780272359999999,304,12,2,200,0"]  # a later file in microseconds
    fetched: list[str] = []
    monkeypatch.setattr(archive, "_fetch", lambda url, **_: fetched.append(url) or (_zip(rows) if "2026-06" in url else None))

    bars = archive.load_klines("BTCUSDT", "3m", ["2026-06", "2026-07"], market="spot", cache_dir=tmp_path)
    again = archive.load_klines("BTCUSDT", "3m", ["2026-06"], market="spot", cache_dir=tmp_path)

    assert [bar.close for bar in bars] == [100.5, 101.5] and bars[1].timestamp.minute == 3 and bars[0].interval_seconds == 180
    assert fetched[0].endswith("data/spot/monthly/klines/BTCUSDT/3m/BTCUSDT-3m-2026-06.zip")
    assert len(fetched) == 2 and [bar.close for bar in again] == [100.5, 101.5]  # the second call came from the cache
