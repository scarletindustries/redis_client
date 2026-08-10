<img width="128" src="https://github.com/scarletindustries.png" />

### Redis client

Talk to Redis from the Scarlet programming language.

[Documentation](https://scarlet.industries) • [Redis commands](https://redis.io/docs/latest/commands/)

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
  other's replies.
- RESP3, so Redis 6 or newer. The connection negotiates it at connect and
  fails loudly rather than quietly misreading a RESP2 server's replies.

## Hacking on it

```
docker compose up -d          # redis on :5379
scarlet run example/tour.scrl
scarlet run test/suite.scrl   # 162 assertions against a live server
```
