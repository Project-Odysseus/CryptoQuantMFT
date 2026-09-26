"""The dead-man's switch pings an outside monitor without ever breaking the runtime."""

from __future__ import annotations

from src.utils.heartbeat import Heartbeat


def test_alive_pings_are_throttled_and_failures_go_to_the_fail_url_with_the_reason() -> None:
    sent: list[tuple[str, bytes | None]] = []
    clock = [0.0]
    heartbeat = Heartbeat("https://hc-ping.com/abc/", min_interval=60.0, transport=lambda url, body: sent.append((url, body)), clock=lambda: clock[0])

    assert heartbeat.started() and heartbeat.alive()
    clock[0] = 30.0
    assert not heartbeat.alive()  # inside the interval: skipped
    clock[0] = 61.0
    assert heartbeat.alive()
    assert heartbeat.failed("cycle failed: venue down")
    assert heartbeat.alive()  # a failure resets the throttle, so recovery is reported at once
    assert sent == [("https://hc-ping.com/abc/start", None), ("https://hc-ping.com/abc", None), ("https://hc-ping.com/abc", None),
                    ("https://hc-ping.com/abc/fail", b"cycle failed: venue down"), ("https://hc-ping.com/abc", None)]


def test_a_monitor_that_is_down_never_raises() -> None:
    def broken(url: str, body: bytes | None) -> None:
        raise TimeoutError("monitor down")

    assert Heartbeat("https://hc-ping.com/abc", transport=broken).alive() is False
