import socket
from functools import partial

import pytest
from tests.helpers.network import _no_real_network as _no_real_network


@pytest.mark.parametrize("operation", ["connect", "connect_ex", "create_connection", "getaddrinfo"])
def test_provider_unit_tests_cannot_open_real_network_connections(operation):
    with socket.socket() as connection:
        if operation in {"connect", "connect_ex"}:
            action = partial(getattr(connection, operation), ("example.invalid", 443))
        elif operation == "create_connection":
            action = partial(socket.create_connection, ("example.invalid", 443))
        else:
            action = partial(socket.getaddrinfo, "example.invalid", 443)
        with pytest.raises(pytest.fail.Exception, match="unit test attempted real network access"):
            action()
