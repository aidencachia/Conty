import pytest
from conftest import Clock

from conty.auth import APIError, Limiter, Sessions


def test_pairing_expiry_single_use_and_session_expiry():
    clock = Clock()
    output = []
    sessions = Sessions(clock=clock, output=output.append)
    sessions.rotate()
    original = sessions.code
    credential, session = sessions.pair(original, "phone")
    assert sessions.authenticate(credential) == session
    assert credential not in sessions.records
    with pytest.raises(APIError, match="invalid or expired"):
        sessions.pair(original, "phone")
    clock.now += 121
    with pytest.raises(APIError, match="invalid or expired"):
        sessions.pair(sessions.code, "phone")
    sessions.maintain()
    assert sessions.code_expires > clock.now
    clock.now += 8 * 3600
    assert not sessions.active(session.id)
    with pytest.raises(APIError) as caught:
        sessions.authenticate(credential)
    assert caught.value.status == 401
    sessions.maintain()
    assert not sessions.records


def test_pairing_throttle_and_session_capacity():
    clock = Clock()
    sessions = Sessions(clock=clock, output=lambda _: None)
    sessions.rotate()
    for _ in range(5):
        with pytest.raises(APIError):
            sessions.pair("wrong", "phone")
    with pytest.raises(APIError) as caught:
        sessions.pair(sessions.code, "phone")
    assert caught.value.status == 429
    clock.now += 60
    for index in range(32):
        clock.now += 60
        sessions.maintain()
        sessions.pair(sessions.code, str(index))
    clock.now += 60
    with pytest.raises(APIError) as caught:
        sessions.pair(sessions.code, "another")
    assert caught.value.code == "session_capacity"


def test_global_pairing_throttle():
    sessions = Sessions(output=lambda _: None)
    sessions.rotate()
    for index in range(20):
        with pytest.raises(APIError):
            sessions.pair("wrong", str(index))
    with pytest.raises(APIError) as caught:
        sessions.pair(sessions.code, "new-source")
    assert caught.value.status == 429


def test_limiter_refills_and_bounds_source_state():
    clock = Clock()
    limiter = Limiter(2, 1, clock=clock, capacity=2)
    assert limiter.allow("a") and limiter.allow("a")
    assert not limiter.allow("a")
    assert limiter.allow("b")
    assert not limiter.allow("c")
    assert len(limiter.entries) == 2
    clock.now += 3
    assert limiter.allow("c")
    assert len(limiter.entries) == 1
