<img width="128" src="https://github.com/scarletindustries.png" />

### Redis Client

Talk to Redis from the Scarlet programming language.

[Documentation](https://scarlet.industries/docs/redis) • [Redis commands](https://redis.io/docs/latest/commands/)

---

```
git submodule add https://github.com/scarletindustries/redis_client lib/redis
```

## Get and set

```scarlet
import scarlet/result
import ./lib/redis/redis.{Conn}
import ./lib/redis/strings

fn greet(c Conn) Result(Nil, redis.RedisError) {
	_ <- result.then(strings.set(c, 'greeting', 'hello'))
	value <- result.then(strings.get(c, 'greeting'))
	println(value or '(nil)')
	Ok(Nil)
}

redis.with_conn('127.0.0.1', 6379, greet)
```

`with_conn` closes the connection on the way out, however the block ends.
`<-` binds on success and bails on the first error, so there is no error
plumbing between the lines you care about.

A missing key is `None`, not `''`. A refused command is an `Err` you can match
on, and the connection stays usable afterwards.

## Pooling

One connection per process is a fine rule for a script and an awkward one for a
server, where the work is spread across processes that each need Redis and none
of them wants to own a connection. A pool inverts it — the connections sit in
processes of their own, and the *work* travels:

```scarlet
import ./lib/redis/pool

// A process with a job to do and no connection of its own. It takes one for as
// long as the command needs it and gives it straight back.
fn handle(p Conn, job String) Nil {
	match strings.incr(p, 'processed') {
		Ok(n) -> println('${job} done, ${n} so far')
		Err(e) -> println(redis.show_error(e))
	}
}

fn run(p Conn) Result(Nil, redis.RedisError) {
	// Both of these share the same four connections.
	_ = process.spawn(fn() handle(p, 'job:1'))
	_ = process.spawn(fn() handle(p, 'job:2'))
	Ok(Nil)
}

pool.with_pool('127.0.0.1', 6379, 4, run)
```

A pool *is* a `Conn` — the same type one socket is. That is what keeps it from
costing anything at the call site: every command module takes a `Conn`, so every
command works on a pool with no wrapper and nothing to re-export.

```scarlet
value <- result.then(strings.get(p, 'greeting'))
```

The line is identical whether `p` came from `redis.connect` or `pool.start`, so
a function written against one connection runs against eight unchanged. What
differs is only what the two promise: a socket-backed `Conn` belongs to one
process, while a pooled one is a mailbox address — copying it costs nothing, so
hand it to as many processes as you like.

One command is one checkout, and two commands may land on two connections. When
that matters, `with_conn` holds one for the length of a block, which is the unit
of exclusivity WATCH/MULTI needs:

```scarlet
pool.with_conn(p, fn(c) transactions.transaction(c, [
	['DECRBY', 'stock:42', '1'],
	['LPUSH', 'orders', 'order:99'],
]))
```

Given a plain connection that is just `next(c)`, so a block written for a pool
costs nothing when it runs unpooled.

`start` dials every connection before it returns, so a server that is down is
one error where the program can still act on it, rather than a surprise
attached to whichever request arrives first. After that the pool is a fixed set
of connections: a request arriving when all of them are busy waits its turn
rather than opening one more, which is what keeps a burst of traffic from
becoming a burst of connections for the server to police. `stop` drains what is
queued before it closes, and `with_pool` ties both ends to a block.

A connection the server hangs up on is not handed to the next caller. The
caller holding it at the time gets the error — the pool will not resend a
command it cannot prove was never applied — and the connection is dialled again
for the caller after that. A refused command is different: that is Redis
answering, so the connection goes straight back to the pool.

A pool that fails outright is the one case a caller cannot recover from, so it
is reported rather than waited on. The pool runs in processes of its own,
unlinked from whoever started it: a caller that crashes does not take the pool
down, and a fault inside the pool does not reach back into a caller that was
only ever going to be told about it. What every caller gets instead is
`Err(Pool(..))` — the pool ended before it could answer — and that includes the
commands already queued and `stop` itself. Nothing parks on a mailbox with
nobody left to send to.

What that error carries is a `PoolFault`, not a sentence: `Forgotten` for a pool
that is well and truly gone, `Killed`, `Stopped`, or `Crashed` holding the
runtime error it died of. Mostly it is `Forgotten`, because the runtime keeps
nothing about a process once it has finished, and the useful part is the same in
every case: there is no point asking again.

Subscribers still want a connection of their own. `pubsub.subscribe` takes the
socket over for as long as the `Subscription` lives, which is exactly what a
pooled connection cannot do — a connection lent for a subscription would never
come back, so subscribing on a pool is `Err(Misuse(..))` rather than a lease
that never returns.

## Pub/Sub

Publishing is an ordinary command:

```scarlet
_ <- result.then(pubsub.publish(c, 'news', 'hello'))
```

Subscribing takes the connection over, so give it one of its own:

```scarlet
import ./lib/redis/pubsub.{Message}

fn listen(c Conn) Result(Nil, redis.RedisError) {
	sub <- result.then(pubsub.subscribe(c, ['news']))
	(rest, message) = pubsub.next_message(sub)
	match message {
		Ok(Message(channel, payload)) -> println('${channel}: ${payload}')
		Ok(_) -> Nil
		Err(e) -> println(redis.show_error(e))
	}
	pubsub.close(rest)
	Ok(Nil)
}
```

`next_message` parks the calling process until something arrives — everything
else keeps running. `next_message_within(sub, ms)` gives up after a timeout.

## Pipelines

A round trip costs a network latency whether it carries one command or a
hundred. Send them together:

```scarlet
replies <- result.then(redis.pipeline(c, [
	['SET', 'hits', '0'],
	['INCR', 'hits'],
	['GET', 'hits'],
]))
```

Measured against a local server, 500 `SET`s: **152ms one at a time, 6ms
pipelined.**

You get one reply per command, in order, each its own result — so a single
command that fails doesn't sink the batch:

```scarlet
match replies {
	[_, incr, get] -> {
		n <- result.then(redis.expect_int(incr))
		v <- result.then(redis.expect_bulk(get))
		...
	}
	_ -> Err(redis.Protocol('expected three replies'))
}
```

## Transactions

Same idea, but atomic — MULTI, your commands, and EXEC go out as one pipeline:

```scarlet
replies <- result.then(transactions.transaction(c, [
	['DECRBY', 'stock:42', '1'],
	['LPUSH', 'orders', 'order:99'],
]))
```

`None` back means a `WATCH`ed key changed and Redis abandoned the whole thing —
read your values again and retry.

Worth knowing what a Redis transaction is: nothing interleaves with it, but a
command that fails at runtime doesn't roll back the ones before it. Isolation,
not rollback.

## The rest

275 commands, grouped the way Redis groups them:

`strings` `keys` `hashes` `lists` `sets` `sorted_sets` `streams` `pubsub`
`geo` `bitmaps` `hyperloglog` `arrays` `transactions` `scripting` `connection`
`server` `cluster`

```scarlet
hashes.hgetall(c, 'user:1')
sorted_sets.zadd(c, 'board', [('ada', Finite(120.0))])
lists.blpop(c, ['jobs'], 5.0)
```

Anything not wrapped, you can still send:

```scarlet
redis.command(c, ['OBJECT', 'ENCODING', 'user:1'])
redis.command_raw(c, [<<'SET'>>, key, jpeg])
```

## Worth knowing

- Check out into a directory Scarlet can parse as an identifier. `lib/redis`
  and `vendor/scarlet_redis` are fine; `vendor/scarlet-redis` is a parse error,
  because the hyphen reads as subtraction.
- One connection per process. Two processes sharing one would read each
  other's replies — which is what `pool` is for.
- RESP3, so Redis 6 or newer. The connection negotiates it at connect and
  fails loudly rather than quietly misreading a RESP2 server's replies.

## Hacking on it

```
docker compose up -d          # redis on :5379
scarlet run example/tour.scrl # one connection, one command after another
scarlet run example/pool.scrl # twelve processes over four connections
scarlet run test/suite.scrl   # 162 assertions against a live server
scarlet run test/pool.scrl    # the pool's own, which are about lifecycle
```

### TLS

`Conn` cannot yet be TLS-backed, so there is no `rediss://` to use. What exists
is the rig that the transport will be built against, because verification is
the hard part of TLS and a rig that cannot tell an encrypted connection from a
credulous one proves nothing:

```
./scripts/tls-certs.sh                                    # once, mints test/tls/
docker compose --profile tls up -d                        # redis TLS on :6380
SSL_CERT_FILE=test/tls/ca.crt scarlet run test/tls.scrl   # PONG + two refusals
scarlet run test/tls.scrl                                 # the untrusted-issuer refusal
```

**Trusting the test CA is one environment variable, on both macOS and Linux.**
`SSL_CERT_FILE` is what `rustls-native-certs` reads on Unix, so nothing is
installed into the machine's trust store, nothing needs root, and there is
nothing to undo after a run. It grants a root; it does not stop the checking —
the wrong-name check below fails *while it is set*, which is what proves it.
There is no way to ask Scarlet to skip verification, and the rig does not want
one.

The two invocations are two processes because the untrusted-issuer arm needs
`SSL_CERT_FILE` absent and the other three need it present. The program prints
which mode it ran: a run reporting one check is not a run that passed four.

Certificates are minted per checkout rather than committed — a private key in a
public repository is a private key everyone has — so `test/tls/` is ignored and
the TLS service sits behind a compose profile it cannot start without.

**Do not point the rig at the plaintext port.** Redis never answers a TLS
ClientHello there, and `tls.handshake` has no deadline, so it parks for ever
rather than erring — measured 3/3 at a 20s bound on linux x86_64, and the same
on macOS. `openssl s_client` hangs identically, so it is the peer's silence
rather than anything Scarlet does.
