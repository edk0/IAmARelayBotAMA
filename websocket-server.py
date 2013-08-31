#!/usr/bin/python
import json
import sys
import string
import random
import time
import yaml

from tx_redis import RedisFactory

from twisted.internet import protocol
from twisted.internet import reactor
from twisted.python import log
from twisted.application.strports import listen

from txws import WebSocketFactory


ALPHABET = string.lowercase + string.uppercase + string.digits


with open("config.yml") as f:
    CONFIG = yaml.load(f)


class RelayHistory(object):
    _counter = 0

    def __init__(self, size, mode='count'):
        self._size = size
        self._mode = mode
        self._hardlimit = self._size * 3
        self._history = []

    def push(self, event):
        event = (time.time(), event)
        self._history.append(event)
        if self._mode == 'count':
            self._limiter_count(event)
        else:
            self._limiter_time(event)

    def _limiter_count(self, event):
        self._counter += 1
        while self._counter > self._size or len(self._history) > self._hardlimit:
            ev = self._history.pop(0)
            self._counter -= 1

    def _limiter_time(self, event):
        n = 0
        while self._history[n][0] < time.time() - self._size:
            n += 1
        del self._history[:n]

    def __iter__(self):
        for ev in list(self._history):
            yield ev[1]


class WebProtocol(protocol.Protocol):
    def __init__(self, factory):
        self.factory = factory

    def get_channel(self):
        bits = self.transport.location.split('/')
        assert bits[0] == '' and bits[1] == 'chat' and bits[3] == 'socket'
        return bits[2]

    def connectionMade(self):
        oldValidateHeaders = self.transport.validateHeaders
        def wrap(*args, **kwargs):
            r = oldValidateHeaders(*args, **kwargs)
            if r: self.headersValidated()
            return r
        self.transport.validateHeaders = wrap

    def headersValidated(self):
        self.factory.connectionMade(self)

    def connectionLost(self, reason):
        self.factory.connectionLost(self)

    def send(self, data):
        if isinstance(data, unicode):
            data = data.encode('utf8')
        self.transport.write(data)


class WebFactory(protocol.ServerFactory):
    def __init__(self, parent, channels):
        self.parent = parent
        self.channel_map = channels
        self.clients = {v: set() for v in self.channel_map.values()}

    def buildProtocol(self, addr):
        return WebProtocol(self)

    def connectionMade(self, protocol):
        channel = protocol.get_channel()
        if channel not in self.channel_map:
            self.parent.error_client(protocol, "{} is not a valid channel!".format(channel))
        else:
            self.parent.new_client(protocol)
            ch = self.channel_map[channel]
            self.clients[ch].add(protocol)

    def connectionLost(self, protocol):
        if protocol.get_channel() not in self.clients or protocol not in self.clients[protocol.get_channel()]:
            return
        self.clients[protocol.get_channel()].remove(protocol)

    def relay(self, channel, data):
        for p in self.clients[channel]:
            p.send(data)


class Manager:
    def setup(self):
        self.channel_map = CONFIG['web']['channel_map']
        self.history = dict((k, RelayHistory(CONFIG['web']['history_size'], CONFIG['web']['history_mode'])) for k in self.channel_map.values())

        self.redis_factory = RedisFactory(self, list(v for v in self.channel_map.values()))
        self._web_factory = WebFactory(self, self.channel_map)
        self.ws_factory = WebSocketFactory(self._web_factory)

        reactor.connectTCP(CONFIG['redis_host'], CONFIG['redis_port'], self.redis_factory)
        listen(CONFIG['web']['host'], self.ws_factory)

    @staticmethod
    def random_str(l=12):
        return ''.join(random.choice(ALPHABET) for i in xrange(l))

    def new_client(self, client):
        for msg in self.history.get(self.channel_map[client.get_channel()], []):
            client.send(msg)

    def error_client(self, client, message):
        client.send(u"\u00a7e" + message)
        client.transport.loseConnection()

    def handle_subscribe(self, channel, count):
        pass

    def handle_message(self, channel, data):
        self.history[channel].push(data)
        self._web_factory.relay(channel, data)


if __name__ == '__main__':
    log.startLogging(sys.stdout)
    manager = Manager()
    manager.setup()
    reactor.run()