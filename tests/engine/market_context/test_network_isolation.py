import socket

import pytest


@pytest.mark.parametrize(
    "operation", ["connect", "connect_ex", "create_connection", "getaddrinfo"]
)
def test_provider_unit_tests_cannot_open_real_network_connections(operation):
    with pytest.raises(pytest.fail.Exception, match="unit test attempted real network access"):
        if operation in {"connect", "connect_ex"}:
            with socket.socket() as connection:
                getattr(connection, operation)(("example.invalid", 443))
        elif operation == "create_connection":
            socket.create_connection(("example.invalid", 443))
        else:
            socket.getaddrinfo("example.invalid", 443)
