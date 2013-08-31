## example / live demo

* [survival](http://glin.es/chat/survival) / [pve](http://glin.es/chat/pve) /
  [creative](http://glin.es/chat/creative) WebSocket relays
* irc.gamesurge.net #RedditMC-<em>[</em>S<em>/</em>P<em>/</em>C<em>]</em>

## things

- `chat.html` - Web client, originally by hansihe.

- `client.js` - Minecraft client. invoke as `node client.js <server>`.
  In need of serious refactoring.
- `ircbot.py` - Redis client -> IRC bot. Mostly stolen from
  [mark2](https://github.com/mcdevs/mark2/blob/master/mk2/plugins/irc.py).
- `websocket-server.py` - Redis client -> WebSocket server.
- `tx_redis.py` - Redis protocol implementation for Twisted. Cobbled together
  from stuff I wrote for a never-finished project called mark2-web.


## dependencies

### python

```
twisted
hiredis
pyyaml
txws
```

### node.js

```
minecraft-protocol
redis
js-yaml
properties
```


## disclaimer

This is a horrible hack I threw together in a couple of days. It's messy. If
it doesn't work, doesn't do what you want, or is in any other way
unsatisfactory... I honestly don't care. You can ping me about it on IRC if you
*really* want.
