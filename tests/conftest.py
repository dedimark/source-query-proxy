import asyncio
import logging
import socket

import pytest


@pytest.fixture(params=['INET'])
def addr_family(request):
    if request.param == 'INET':
        return ('127.0.0.1', 0), socket.AF_INET
    elif request.param == 'INET6':
        return ('::1', 0), socket.AF_INET6
    raise NotImplementedError


@pytest.fixture()
def udp_socket(addr_family):
    addr, family = addr_family

    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        yield sock


@pytest.fixture()
def challenge():
    """Challenge number

    Requested from server and should be used for final query
    """
    # any signed long (int32) value
    return 0xBEEF


@pytest.yield_fixture()
def event_loop():
    from asyncio import runners

    loop = asyncio.new_event_loop()
    yield loop
    # Right way to close event_loop
    # copied from asyncio.run
    # noinspection PyUnresolvedReferences,PyProtectedMember
    runners._cancel_all_tasks(loop)
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.close()


@pytest.fixture(autouse=True)
def _check_no_errors(caplog):
    yield
    for when in ('setup', 'call'):
        messages = [x.message for x in caplog.get_records(when) if x.levelno >= logging.ERROR]
        if messages:
            pytest.fail(f'error messages encountered during testing: {messages!r}')