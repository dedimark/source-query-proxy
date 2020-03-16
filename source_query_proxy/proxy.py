import asyncio
import collections
import logging
import random
import time
import typing

import async_timeout
import backoff

from . import config
from .source import messages
from .transport import bind
from .transport import connect

MAX_SIZE_32 = 2 ** 31 - 1


class AwaitableDict(collections.UserDict):
    """Оборачивает все значения в asyncio.Future
    таким образом можно дождаться появления нужного значения в словаре
    """

    def __setitem__(self, key, value):
        if key in self:
            fut = self.data[key]
            if fut.done():
                fut = self.data[key] = asyncio.Future()
            fut.set_result(value)
        else:
            fut = asyncio.Future()
            fut.set_result(value)
            self.data[key] = fut

    def __getitem__(self, key):
        value = super().__getitem__(key)
        if not value.done():
            raise KeyError(key)
        return value.result()

    async def get_wait(self, key):
        if key not in self.data:
            fut = asyncio.Future()
            self.data[key] = fut
        else:
            fut = self.data[key]

        return await fut


class QueryProxy:
    A2S_EMPTY_CHALLENGE = -1

    def __init__(self, settings: config.ServerModel, name: str = None):
        listen_addr = (str(settings.network.bind_ip), settings.network.bind_port)
        server_addr = (str(settings.network.server_ip), settings.network.server_port)

        if name is None:
            name = '%s:%s' % listen_addr

        self.listen_addr = listen_addr
        self.server_addr = server_addr
        self.resp_cache = AwaitableDict()
        self.our_a2s_challenge = random.randint(1, MAX_SIZE_32)
        self.settings = settings
        self.logger = logging.getLogger(name)

    # noinspection PyPep8Naming
    @property
    def retry_TimeoutError(self):  # noqa: ignore=N802
        return backoff.on_exception(backoff.constant, asyncio.TimeoutError, logger=self.logger)

    async def listen_client_requests(self):
        self.logger.debug('Binding ... ')
        async with (await bind(self.listen_addr)) as listening:
            self.logger.debug('Binding ... done!')
            self.logger.debug('Listening started!')
            while True:
                request, data, addr = await listening.recv_packet()
                if request is None:
                    self.logger.warning(
                        'Broken data was received: data[:150]=%s', data[:150],
                    )
                    continue

                response = await self.get_response_for(request)
                await listening.send_packet(response, addr=addr)

    async def update_server_query_cache(self):
        return await asyncio.gather(
            self.retry_TimeoutError(self._update_info)(),
            self.retry_TimeoutError(self._update_players)(),
            self.retry_TimeoutError(self._update_rules)(),
        )

    async def send_recv_packet(self, client, packet: messages.Packet, timeout=None):
        """Send packet and wait for response for it

        In addition to call client.[send_packet(), recv_packet()] this method handle
        GetChallengeResponse logic

        :param client: connected client
        :param packet: any `messages.Packet` instance to send to
        :param timeout: how much wait response
            trigger asyncio.TimeoutError on exceeded
        :return: tuple (message, data, addr, new_challenge)
            `new_challenge` will be None if not present
        """
        old_challenge = packet.get('challenge')

        a2s_challenge = old_challenge
        while True:
            if a2s_challenge is not None:
                await client.send_packet(packet.encode(challenge=a2s_challenge))
            else:
                await client.send_packet(packet.encode())

            start = time.monotonic()
            with async_timeout.timeout(timeout):
                message, data, addr = await client.recv_packet()
                self.logger.debug('Got %s for %ss', message.__class__.__name__, time.monotonic() - start)

            if isinstance(message, messages.GetChallengeResponse):
                if old_challenge is not self.A2S_EMPTY_CHALLENGE:
                    self.logger.warning(
                        'Challenge number changed: %s -> %s', old_challenge, message['challenge'],
                    )

                a2s_challenge = message['challenge']
                continue

            break

        return message, data, addr, a2s_challenge

    async def _update_info(self):
        logger = self.logger.getChild('update-info')
        request = messages.InfoRequest().encode()

        get_time = asyncio.get_event_loop().time
        connection_lifetime = self.settings.src_query_port_lifetime
        while True:
            connection_eta = get_time() + connection_lifetime

            async with (await connect(self.server_addr)) as client:
                logger.debug('Connected to %s (client port=%s)', self.server_addr, client.sockname[1])

                while get_time() < connection_eta:
                    await client.send_packet(request)
                    start = time.monotonic()
                    with async_timeout.timeout(connection_lifetime):
                        message, data, addr = await client.recv_packet()
                        self.logger.debug('Got %s for %ss', message.__class__.__name__, time.monotonic() - start)
                    self.resp_cache['a2s_info'] = data
                    await asyncio.sleep(self.settings.a2s_info_cache_lifetime)

                logger.debug('Connection expired. Closing')

    async def _update_rules(self):
        logger = self.logger.getChild('update-rules')

        get_time = asyncio.get_event_loop().time
        connection_lifetime = self.settings.src_query_port_lifetime
        while True:
            connection_eta = get_time() + connection_lifetime

            async with (await connect(self.server_addr)) as client:
                logger.debug('Connected to %s (client port=%s)', self.server_addr, client.sockname[1])

                a2s_challenge = self.A2S_EMPTY_CHALLENGE
                while get_time() < connection_eta:
                    request = messages.RulesRequest(challenge=a2s_challenge)
                    message, data, addr, a2s_challenge = await self.send_recv_packet(
                        client, request, timeout=connection_lifetime,
                    )
                    self.resp_cache['a2s_rules'] = data
                    await asyncio.sleep(self.settings.a2s_rules_cache_lifetime)

                logger.debug('Connection expired. Closing')

    async def _update_players(self):
        logger = self.logger.getChild('update-players')

        get_time = asyncio.get_event_loop().time
        connection_lifetime = self.settings.src_query_port_lifetime
        while True:
            connection_eta = get_time() + connection_lifetime

            async with (await connect(self.server_addr)) as client:
                logger.debug('Connected to %s (client port=%s)', self.server_addr, client.sockname[1])

                a2s_challenge = self.A2S_EMPTY_CHALLENGE
                while get_time() < connection_eta:
                    request = messages.PlayersRequest(challenge=a2s_challenge)
                    message, data, addr, a2s_challenge = await self.send_recv_packet(
                        client, request, timeout=connection_lifetime,
                    )

                    self.resp_cache['a2s_players'] = data
                    await asyncio.sleep(self.settings.a2s_players_cache_lifetime)

                logger.debug('Connection expired. Closing')

    async def get_response_for(self, message) -> typing.Optional[bytes]:
        resp = None

        if isinstance(message, messages.InfoRequest):
            resp = await self.resp_cache.get_wait('a2s_info')
        elif isinstance(message, (messages.PlayersRequest, messages.RulesRequest)):
            challenge = message['challenge']

            if challenge == self.our_a2s_challenge:
                if isinstance(message, messages.PlayersRequest):
                    resp = await self.resp_cache.get_wait('a2s_players')
                elif isinstance(message, messages.RulesRequest):
                    resp = await self.resp_cache.get_wait('a2s_rules')
            elif self.our_a2s_challenge != self.A2S_EMPTY_CHALLENGE:
                # player request challenge number or we don't know who is it
                # return challenge number
                resp = messages.GetChallengeResponse(challenge=self.our_a2s_challenge).encode()

        if resp is None:
            raise NotImplementedError

        return resp

    async def run(self):
        await asyncio.gather(self.update_server_query_cache(), self.listen_client_requests())