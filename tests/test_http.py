"""Tests for proxy rotation and burn-on-failure (engine-rotator behaviour)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nestick.config import Settings  # noqa: E402
from nestick.http import Fetcher, PROXY_BURN_STRIKES  # noqa: E402


def make_fetcher(proxies, clients=None):
    s = Settings(query="q", proxies=proxies, cache=False, max_retries=1)
    f = Fetcher(s)
    if clients is not None:
        f._clients = clients  # stand-ins; _client() never touches the network
    return f


def test_round_robin_picks_each_proxy():
    f = make_fetcher(["http://p1", "http://p2"], clients=["a", "b"])
    idx1, c1 = f._client()
    idx2, c2 = f._client()
    assert (idx1, c1) == (1, "b")
    assert (idx2, c2) == (0, "a")


def test_proxy_burned_after_enough_strikes():
    f = make_fetcher(["http://p1", "http://p2"], clients=["a", "b"])
    for _ in range(PROXY_BURN_STRIKES - 1):
        f._strike_proxy(0)
    assert 0 not in f._proxy_burned
    f._strike_proxy(0)
    assert 0 in f._proxy_burned


def test_rotates_off_burned_proxy():
    f = make_fetcher(["http://p1", "http://p2"], clients=["a", "b"])
    for _ in range(PROXY_BURN_STRIKES):
        f._strike_proxy(0)
    seen = {f._client()[0] for _ in range(10)}
    assert seen == {1}


def test_all_burned_falls_back_to_round_robin():
    f = make_fetcher(["http://p1", "http://p2"], clients=["a", "b"])
    for _ in range(PROXY_BURN_STRIKES):
        f._strike_proxy(0)
        f._strike_proxy(1)
    idx, client = f._client()
    assert client in ("a", "b")
    assert idx in (0, 1)


def test_reward_resets_strikes():
    f = make_fetcher(["http://p1", "http://p2"], clients=["a", "b"])
    f._strike_proxy(0)
    f._strike_proxy(0)
    assert f._proxy_strikes.get(0) == 2
    f._reward_proxy(0)
    assert f._proxy_strikes.get(0) is None
    assert 0 not in f._proxy_burned


def test_direct_connection_never_burned():
    f = make_fetcher([], clients=[None])
    for _ in range(PROXY_BURN_STRIKES * 2):
        f._strike_proxy(0)
    assert 0 not in f._proxy_burned
    assert f._proxy_strikes.get(0) is None
