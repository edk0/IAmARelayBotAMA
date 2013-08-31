from __future__ import print_function

import re
import os.path as path
import yaml

from tx_redis import RedisFactory

from twisted.words.protocols import irc
from twisted.internet import defer, protocol, reactor, task
from twisted.internet.interfaces import ISSLTransport
from twisted.python.util import InsensitiveDict

try:
    from OpenSSL import SSL
    from twisted.internet import ssl

    have_ssl = True
    
    class RelayContextFactory(ssl.ClientContextFactory):
        def __init__(self, parent, fingerprint=None, cert=None):
            self.parent = parent
            self.fingerprint = fingerprint
            self.cert = path.expanduser(cert) if cert else None
        
        @staticmethod
        def stripfp(fp):
            return fp.replace(':', '').lower()
        
        def verify(self, conn, cert, errno, errdepth, rc):
            ok = self.stripfp(cert.digest("sha1")) == self.stripfp(self.fingerprint)
            if self.parent and self.parent.factory.reconnect and not ok:
                print("irc: server certificate verification failed")
                self.parent.factory.reconnect = False
            return ok
            
        def getContext(self):
            ctx = ssl.ClientContextFactory.getContext(self)
            if self.fingerprint:
                ctx.set_verify(SSL.VERIFY_PEER, self.verify)
            if self.cert:
                ctx.use_certificate_file(self.cert)
                ctx.use_privatekey_file(self.cert)
            return ctx
except:
    have_ssl = False


class IRCUser(object):
    username = ""
    hostname = ""
    oper = False
    away = False
    
    def __init__(self, parent, nick):
        self.parent = parent
        self.nick = nick

    def on(self, channel):
        return self.parent.channels.get(channel, {}).get(self.nick, False)


class IRCUserInChannel(object):
    status = ""

    def __init__(self, user, channel):
        self.user = user
        self.channel = channel

    def __getattr__(self, k):
        return getattr(self.user, k)
    
    @property
    def priority(self):
        p = self.parent.priority
        if self.status:
            return min([p[s] for s in self.status])
        else:
            return None


class SASLExternal(object):
    name = "EXTERNAL"

    def __init__(self, username, password):
        pass

    def is_valid(self):
        return True

    def respond(self, data):
        return ""


class SASLPlain(object):
    name = "PLAIN"

    def __init__(self, username, password):
        self.response = "{0}\0{0}\0{1}".format(username, password)

    def is_valid(self):
        return self.response != "\0\0"

    def respond(self, data):
        if data:
            return False
        return self.response


SASL_MECHANISMS = (SASLExternal, SASLPlain)


class IRCBot(irc.IRCClient):
    sasl_buffer = ""
    sasl_result = None
    sasl_login = None

    def __init__(self, factory, parent):
        self.factory     = factory
        self.nickname    = parent.nickname.encode('ascii')
        self.realname    = parent.realname.encode('ascii')
        self.username    = parent.ident.encode('ascii')
        self.ns_username = parent.username
        self.ns_password = parent.password
        self.password    = parent.server_password.encode('ascii')
        self.join_channels = parent.channel_map.keys()

        self.users        = InsensitiveDict()
        self.channels     = InsensitiveDict()
        self.cap_requests = set()

        self.parent = parent

    def register(self, nickname, hostname="foo", servername="bar"):
        self.sendLine("CAP LS")
        return irc.IRCClient.register(self, nickname, hostname, servername)

    def sendLine(self, line):
        if isinstance(line, unicode):
            line = line.encode('utf8', 'replace')
        irc.IRCClient.sendLine(self, line)

    def _parse_cap(self, cap):
        mod = ''
        while cap[0] in "-~=":
            mod, cap = mod + cap[0], cap[1:]
        if '/' in cap:
            vendor, cap = cap.split('/', 1)
        else:
            vendor = None
        return (cap, mod, vendor)

    def request_cap(self, *caps):
        self.cap_requests |= set(caps)
        self.sendLine("CAP REQ :{0}".format(' '.join(caps)))

    @defer.inlineCallbacks
    def end_cap(self):
        if self.sasl_result:
            yield self.sasl_result
        self.sendLine("CAP END")

    def irc_CAP(self, prefix, params):
        self.supports_cap = True
        identifier, subcommand, args = params
        args = args.split(' ')
        if subcommand == "LS":
            self.sasl_start(args)
            if not self.cap_requests:
                self.sendLine("CAP END")
        elif subcommand == "ACK":
            ack = []
            for cap in args:
                if not cap:
                    continue
                cap, mod, vendor = self._parse_cap(cap)
                if '-' in mod:
                    if cap in self.capabilities:
                        del self.capabilities[cap]
                    continue
                self.cap_requests.remove(cap)
                if cap == 'sasl':
                    self.sasl_next()
            if ack:
                self.sendLine("CAP ACK :{0}".format(' '.join(ack)))
            if not self.cap_requests:
                self.end_cap()
        elif subcommand == "NAK":
            # this implementation is probably not compliant but it will have to do for now
            for cap in args:
                self.cap_requests.remove(cap)
            if not self.cap_requests:
                self.end_cap()

    def signedOn(self):
        if ISSLTransport.providedBy(self.transport):
            cert = self.transport.getPeerCertificate()
            fp = cert.digest("sha1")
            verified = "verified" if self.factory.parent.server_fingerprint else "unverified"
            print("irc: connected securely. server fingerprint: {0} ({1})".format(fp, verified))
        else:
            print("irc: connected")
        
        if self.ns_username and self.ns_password and not self.sasl_login:
            self.msg('NickServ', 'IDENTIFY {0} {1}'.format(self.ns_username, self.ns_password))
        
        for channel in self.join_channels:
            self.join(channel)

    def irc_JOIN(self, prefix, params):
        nick = prefix.split('!')[0]
        channel = params[-1]
        if nick == self.nickname:
            self.joined(channel)
        else:
            self.userJoined(prefix, channel)

    def joined(self, channel):
        print('irc: joined channel')
        self.factory.client = self
        def who():
            self.sendLine("WHO " + channel)
        task.LoopingCall(who).start(30)
    
    def isupport(self, args):
        self.compute_prefix_names()
        
    def compute_prefix_names(self):
        KNOWN_NAMES = {"o": "op", "h": "halfop", "v": "voice"}
        prefixdata = self.supported.getFeature("PREFIX", {"o": ("@", 0), "v": ("+", 1)}).items()
        op_priority = ([priority for mode, (prefix, priority) in prefixdata if mode == "o"] + [None])[0]
        self.prefixes, self.statuses, self.priority = {}, {}, {}

        for mode, (prefix, priority) in prefixdata:
            name = "?"
            if mode in KNOWN_NAMES:
                name = KNOWN_NAMES[mode]
            elif priority == 0:
                if op_priority == 2:
                    name = "owner"
                else:
                    name = "admin"
            else:
                name = "+" + mode
            self.prefixes[mode] = prefix
            self.statuses[prefix] = name
            self.priority[name] = priority
            self.priority[mode] = priority
            self.priority[prefix] = priority

    def parse_prefixes(self, user, nick, prefixes=''):
        status = []
        prefixdata = self.supported.getFeature("PREFIX", {"o": ("@", 0), "v": ("+", 1)}).items()
        for mode, (prefix, priority) in prefixdata:
            if prefix in prefixes + nick:
                nick = nick.replace(prefix, '')
                status.append((prefix, priority))
        if nick == self.nickname:
            return
        user.status = ''.join(t[0] for t in sorted(status, key=lambda t: t[1]))
    
    def irc_RPL_WHOREPLY(self, prefix, params):
        _, channel, username, host, server, nick, status, hg = params
        if nick == self.nickname:
            return
        hops, gecos = hg.split(' ', 1)
        user = self.get_user(nick) or IRCUser(self, nick)
        user.username = username
        user.hostname = host
        user.oper = '*' in status
        user.away = status[0] == 'G'
        self.users[nick] = user
        self.get_channel(channel)[nick] = IRCUserInChannel(user, channel)
        self.parse_prefixes(user, nick, status[1:].replace('*', ''))
    
    def modeChanged(self, user, channel, _set, modes, args):
        args = list(args)
        if channel not in self.parent.channel_map:
            return
        for m, arg in zip(modes, args):
            if m in self.prefixes and arg != self.nickname:
                u = self.get_user(arg).on(channel)
                if u:
                    u.status = u.status.replace(self.prefixes[m], '')
                    if _set:
                        u.status = ''.join(sorted(list(u.status + self.prefixes[m]),
                                                  key=lambda k: self.priority[k]))

    def has_status(self, nick, status):
        if status != 0 and not status:
            return True
        if status not in self.priority:
            return False
        priority = self.priority[status]
        u = self.users.get(nick, None)
        return u and (u.priority is not None) and u.priority <= priority

    def get_channel(self, channel):
        return self.channels.setdefault(channel, InsensitiveDict())

    def get_user(self, nick):
        return self.users.get(nick, False)
    
    def userJoined(self, user, channel):
        nick = user.split('!')[0]
        user = IRCUser(self, nick)
        self.users[nick] = user
        self.get_channel(channel)[nick] = IRCUserInChannel(user, channel)
    
    def userRenamed(self, oldname, newname):
        if oldname not in self.users:
            return
        u = self.users[oldname]
        u.nick = newname
        self.users[newname] = u
        del self.users[oldname]
        for k, v in self.channels.items():
            if oldname in v:
                v[newname] = v[oldname]
                del v[oldname]
    
    def userLeft(self, user, channel):
        if user not in self.users:
            return
        del self.users[user]
        for k, v in self.channels.items():
            if user in v:
                del v[user]
    
    def userKicked(self, kickee, channel, kicker, message):
        if kickee not in self.users:
            return
        del self.users[kickee]
        for k, v in self.channels.items():
            if user in v:
                del v[user]
    
    def userQuit(self, user, quitMessage):
        if user not in self.users:
            return
        del self.users[user]
        for k, v in self.channels.items():
            if user in v:
                del v[user]

    def privmsg(self, user, channel, msg):
        pass

    def action(self, user, channel, msg):
        pass

    def irc_AUTHENTICATE(self, prefix, params):
        self.sasl_continue(params[0])

    def sasl_send(self, data):
        while data and len(data) >= 400:
            en, data = data[:400].encode('base64').replace('\n', ''), data[400:]
            self.sendLine("AUTHENTICATE " + en)
        if data:
            self.sendLine("AUTHENTICATE " + data.encode('base64').replace('\n', ''))
        else:
            self.sendLine("AUTHENTICATE +")

    def sasl_start(self, cap_list):
        if 'sasl' not in cap_list:
            return
        self.request_cap('sasl')
        self.sasl_result = defer.Deferred()
        self.sasl_mechanisms = list(SASL_MECHANISMS)

    def sasl_next(self):
        mech = None
        while not mech or not mech.is_valid():
            if not self.sasl_mechanisms:
                return False
            self.sasl_auth = mech = self.sasl_mechanisms.pop(0)(self.ns_username, self.ns_password)
        self.sendLine("AUTHENTICATE " + self.sasl_auth.name)
        return True

    def sasl_continue(self, data):
        if data == '+':
            data = ''
        else:
            data = data.decode('base64')
        if len(data) == 400:
            self.sasl_buffer += data
        else:
            response = self.sasl_auth.respond(self.sasl_buffer + data)
            if response is False:  # abort
                self.sendLine("AUTHENTICATE *")
            else:
                self.sasl_send(response)
            self.sasl_buffer = ""

    def sasl_finish(self):
        if self.sasl_result:
            self.sasl_result.callback(True)
            self.sasl_result = None

    def sasl_failed(self, whine=True):
        if self.sasl_login is False:
            return
        if self.sasl_next():
            return
        self.sasl_login = False
        self.sendLine("AUTHENTICATE *")
        self.sasl_finish()
        if whine:
            print("irc: failed to log in.")

    def irc_904(self, prefix, params):
        self.sasl_failed()

    def irc_905(self, prefix, params):
        self.sasl_failed()

    def irc_906(self, prefix, params):
        self.sasl_failed(False)

    def irc_907(self, prefix, params):
        self.sasl_failed(False)

    def irc_900(self, prefix, params):
        self.sasl_login = params[2]
        print("irc: logged in as '{0}' (using {1})".format(self.sasl_login, self.sasl_auth.name))

    def irc_903(self, prefix, params):
        self.sasl_finish()

    def alterCollidedNick(self, nickname):
        return nickname + '_'

    def cancel_hilights(self, channel, text):
        def hl(match):
            s = match.group(0)
            if len(s) >= 2 and s in self.get_channel(channel):
                return s[:-1] + '*' + s[-1]
            else:
                return s
        return re.sub(r"\b.+?\b", hl, text)

    def translate_colors(self, text):
        tr = {
            "0": "\x0301",
            "1": "\x0302",
            "2": "\x0303",
            "3": "\x0310",
            "4": "\x0304",
            "5": "\x0306",
            "6": "\x0308",
            "7": "\x0315",
            "8": "\x0314",
            "9": "\x0312",
            "a": "\x0309",
            "b": "\x0311",
            "c": "\x0304",
            "d": "\x0313",
            "e": "\x0308",
            "f": "\x0F",
        }
        return re.sub(ur"\u00a7([0-9a-f])", lambda m: tr.get(m.group(1), ""), text)

    def irc_relay(self, channel, message):
        message = message.decode('utf8')
        self.say(channel, self.translate_colors(self.cancel_hilights(channel, message)))


class IRCBotFactory(protocol.ClientFactory):
    protocol = IRCBot
    client = None
    reconnect = True

    def __init__(self, parent):
        self.parent = parent

    def clientConnectionLost(self, connector, reason):
        if self.reconnect:
            print("irc: lost connection with server: %s" % reason.getErrorMessage())
            print("irc: reconnecting...")
            connector.connect()

    def clientConnectionFailed(self, connector, reason):
        print("irc: connection attempt failed: %s" % reason.getErrorMessage())
    
    def buildProtocol(self, addr):
        p = IRCBot(self, self.parent)
        return p
    
    def irc_relay(self, channel, message):
        if self.client:
            self.client.irc_relay(channel, message)


class IRC(object):
    #connection
    host               = "vimes.rozznet.net"
    port               = 6667
    server_password    = ""
    channel            = ""
    certificate        = ""
    ssl                = False
    server_fingerprint = ""

    #user
    nickname = "MC-Relay"
    realname = "MC-Relay"
    ident    = "relay"
    username = ""
    password = ""

    def start(self):
        self.factory = IRCBotFactory(self)
        if self.ssl:
            if have_ssl:
                cf = RelayContextFactory(self,
                                         cert=self.certificate,
                                         fingerprint=self.server_fingerprint)
                reactor.connectSSL(self.host, self.port, self.factory, cf)
            else:
                print("Couldn't load SSL for IRC!")
                return
        else:
            reactor.connectTCP(self.host, self.port, self.factory)

    def stop(self):
        self.factory.reconnect = False
        if self.factory.client:
            self.factory.client.quit("Relay stopping.")


class Manager(object):
    def __init__(self):
        with open("config.yml") as f:
            self.config = yaml.load(f)

        channels = set()
        self.channel_map = {}

        self.servers = {}

        for name, cfg in self.config['servers'].items():
            self.servers[name] = irc = IRC()
            for k, v in cfg.items():
                setattr(irc, k, v)
            for k, v in irc.channel_map.items():
                irc.channel_map[k] = v = "mcrelay:" + v
                channels.add(v)
                self.channel_map.setdefault(v, set()).add(irc)
            irc.start()

        self.redis_factory = RedisFactory(self, channels)
        reactor.connectTCP(self.config['redis_host'], self.config['redis_port'], self.redis_factory)

        reactor.addSystemEventTrigger("before", "shutdown", self.on_shutdown)

    def on_shutdown(self):
        for irc in self.servers.values():
            irc.stop()

    def handle_message(self, channel, data):
        relays = self.channel_map.get(channel, [])
        for irc in relays:
            for k, v in irc.channel_map.items():
                if v == channel:
                    irc.factory.irc_relay(k, data)


if __name__ == '__main__':
    m = Manager()
    reactor.run()
