from __future__ import print_function

from twisted.internet import protocol


class Node(object):
    def __init__(self, length=None, parent=None, data=None):
        self.data = data or []
        self.parent = parent
        self.length = length or (len(self.data) if isinstance(self.data, list) else None) or 1

    @property
    def full(self):
        if isinstance(self.data, list):
            return len(self.data) >= self.length
        else:
            return bool(self.data)

    def append(self, child):
        if isinstance(child, Node):
            child.parent = self
        self.data.append(child)

    def serialize(self):
        if isinstance(self.data, list):
            return [c.serialize() if isinstance(c, Node) else c for c in self.data]
        else:
            return self.data


class _RedisProtocol(protocol.Protocol):
    def connectionMade(self):
        self.parent.connectionMade(self)

    def request(self, *args):
        self.transport.write(self.encode_request(args))

    def encode_request(self, args):
        lines = []
        lines.append('*' + str(len(args)))
        for a in args:
            if isinstance(a, unicode):
                a = a.encode('utf8')
            lines.append('$' + str(len(a)))
            lines.append(a)
        lines.append('')
        return '\r\n'.join(lines)


class HiRedisProtocol(_RedisProtocol):
    def __init__(self, factory):
        self.parent = factory
        self.reader = hiredis.Reader()

    def dataReceived(self, data):
        self.reader.feed(data)
        response = self.reader.gets()
        while response:
            self.parent.handle(response)
            response = self.reader.gets()


class PythonRedisProtocol(_RedisProtocol):
    decode_next = -1  # the number of bytes we require to decode the next thing
                      # -1 is "until CRLF"
    decode_state = 'type'
    decode_type = '-'

    buf = ''

    def __init__(self, factory):
        self.parent = factory
        self.decode_node = Node(length=1)

    def request(self, *args):
        self.transport.write(self.encode_request(args))

    def encode_request(self, args):
        lines = []
        lines.append('*' + str(len(args)))
        for a in args:
            if isinstance(a, unicode):
                a = a.encode('utf8')
            lines.append('$' + str(len(a)))
            lines.append(a)
        lines.append('')
        return '\r\n'.join(lines)

    def reset(self):
        self.decode_node = Node(length=1)

    def add(self, thing):
        while self.decode_node and self.decode_node.full:
            assert self.decode_node != self.decode_node.parent
            self.decode_node = self.decode_node.parent
        assert self.decode_node
        self.decode_node.append(thing)
        if isinstance(thing, Node) and not thing.full:
            self.decode_node = thing
        else:
            n = self.decode_node
            while n.parent and n.full:
                n = n.parent
            if not n.parent:
                d = n.data[0].serialize()
                self.parent.handle(d)
                self.reset()

    def add_node(self, *a, **kw):
        n = Node(*a, **kw)
        self.add(n)
        if not n.full:
            self.decode_node = n

    def decoder(self, data):
        if self.decode_state == 'type':
            self.decode_type = {
                '$': 'bulk',
                '*': 'multi_bulk',
                ':': 'integer',
                '+': 'status',
                '-': 'error'
            }.get(data[0])
            stuff = data[1:]
            if self.decode_type in ('status', 'error'):
                self.reset()
            else:
                stuff = int(stuff)
                if self.decode_type == 'bulk':
                    if stuff == -1:
                        self.add(None)
                        self.decode_next = -1
                        self.decode_state = 'type'
                    else:
                        self.decode_next = stuff + 2
                        self.decode_state = 'read_bulk'
                elif self.decode_type == 'multi_bulk':
                    self.add_node(length=stuff)
                    self.decode_next = -1
                    self.decode_state = 'type'
                elif self.decode_type == 'integer':
                    self.add(stuff)
                    self.decode_next = -1
                    self.decode_state = 'type'
        elif self.decode_state == 'read_bulk':
            self.add(data)
            self.decode_next = -1
            self.decode_state = 'type'

    def dataReceived(self, data):
        self.buf += data
        while True:
            if self.decode_next >= 0 and len(self.buf) >= self.decode_next:
                d = self.buf[:self.decode_next - 2]
                self.buf = self.buf[self.decode_next:]
                self.decoder(d)
            elif self.decode_next < 0 and '\r\n' in self.buf:
                d, self.buf = self.buf.split('\r\n', 1)
                self.decoder(d)
            else:
                break


try:
    import hiredis
    RedisProtocol = HiRedisProtocol
    print("using hiredis to parse incoming redis messages")
except ImportError:
    RedisProtocol = PythonRedisProtocol
    print("using pure python to parse incoming redis messages - slow")


class RedisFactory(protocol.ReconnectingClientFactory):
    def __init__(self, parent, channels):
        self.parent = parent
        self.channels = channels

    def buildProtocol(self, addr):
        self.protocol = RedisProtocol(self)
        return self.protocol

    def handle(self, thing):
        if isinstance(thing, list) and len(thing) >= 1:
            cmd, args = thing[0], thing[1:]
            handler = getattr(self.parent, 'handle_' + cmd, None)
            if handler:
                handler(*args)
            else:
                print("warning: nothing handles '{}'".format(cmd))
        else:
            print("I don't understand: {}".format(repr(thing)))

    def connectionMade(self, protocol):
        self.resetDelay()
        self.subscribe(self.channels)

    def publish(self, data, channel=None):
        channel = channel or self.channel
        self.protocol.request("PUBLISH", channel, json.dumps(data))

    def subscribe(self, channels):
        self.protocol.request("SUBSCRIBE", *channels)
