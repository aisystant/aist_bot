import pytest

from config.settings import _read_main_pool_sizes


def test_main_pool_defaults_leave_shared_server_headroom() -> None:
    assert _read_main_pool_sizes({}) == (2, 10)


def test_main_pool_sizes_can_be_configured() -> None:
    environ = {"DB_POOL_MIN_SIZE": "3", "DB_POOL_MAX_SIZE": "12"}

    assert _read_main_pool_sizes(environ) == (3, 12)


@pytest.mark.parametrize(
    "environ",
    [
        {"DB_POOL_MIN_SIZE": "0"},
        {"DB_POOL_MAX_SIZE": "not-a-number"},
        {"DB_POOL_MIN_SIZE": "11", "DB_POOL_MAX_SIZE": "10"},
    ],
)
def test_main_pool_sizes_fail_fast_on_invalid_values(environ) -> None:
    with pytest.raises(ValueError):
        _read_main_pool_sizes(environ)
