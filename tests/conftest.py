"""Shared pytest configuration."""


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "live: runs against the public EspoCRM demo (network, real browser; deselect with -m 'not live')"
    )
