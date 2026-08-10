<img width="128" src="https://github.com/scarletindustries.png" />

### Redis client

A pure Scarlet client for Redis — RESP2 over TCP, no bindings.

[Documentation](https://scarlet.industries) • [Redis commands](https://redis.io/docs/latest/commands/)

---

275 of the server's 288 core commands, behind typed functions.

## Installing

Scarlet has no package registry, so this is consumed as a git submodule. Every
module sits at the repo root, which means the submodule directory *is* the
import prefix and there is no `src/` in the middle of your import lines.

```
git submodule add https://github.com/scarletindustries/redis_client lib/redis
```

```scarlet
import scarlet/result
import ./lib/redis/redis.{Conn}
import ./lib/redis/strings
import ./lib/redis/keys

fn greet(c Conn) Result(Nil, redis.RedisError) {
	_ <- result.then(strings.set(c, 'greeting', 'hello from scarlet'))
	value <- result.then(strings.get(c, 'greeting'))
	println(value or '(nil)')
	Ok(Nil)
}

redis.with_conn('127.0.0.1', 6379, greet)
```

**The directory you check it out into must be a valid Scarlet identifier** —
letters, digits and underscores. `lib/redis` and `vendor/scarlet_redis` both
work; `vendor/scarlet-redis` fails to parse, because an import path segment is
an identifier and the hyphen reads as subtraction.

Call sites are qualified by module — `redis.with_conn`, `strings.get`,
`keys.expire` — so `lib/redis/redis` in an import line buys you `redis.` on
every call. Check out into `vendor/scarlet_redis` instead if you would rather
not write the repetition.

`conn <- redis.with_conn(..)` hands the rest of the block to `with_conn`, so
the connection closes on the way out however the block ends. `value <-
result.then(..)` binds on `Ok` and short-circuits the whole script on `Err`.

## Working on the client itself

```
docker compose up -d          # redis on :5379
scarlet run example/tour.scrl # a tour of the client
scarlet run test/suite.scrl   # 145 assertions against the live server
```

## Layout

Flat by design: a nested `src/redis/strings.scrl` would have made every
consumer write `./lib/redis/src/redis/strings`.

| | |
|---|---|
| `redis.scrl` | Connection, the command cycle, and the `expect_*` decoders. |
| `resp.scrl` | RESP2 codec. Sans-IO: a `Binary` buffer and an offset, never a socket. |
| `number.scrl` | Signed-int and float parsing, and the `Score` type. |
| `options.scrl` | Option types shared across groups — TTLs, conditions, units. |
| `strings.scrl`, `keys.scrl`, `hashes.scrl`, … | One module per Redis command group. |
| `example/`, `test/` | Not imported by anything; ignore them in a parent repo. |

Every typed command is one line over `redis.command` plus a decoder, so
anything not covered is still reachable:

```scarlet
redis.command(conn, ['OBJECT', 'ENCODING', key])       // -> resp.Value
redis.command_raw(conn, [<<'SET'>>, key, jpeg_bytes])  // binary-safe
```

## Design

**A `Conn` is just a socket.** Under strict request/response there is no
buffer to carry: we write one command and read until exactly one reply is
complete, Redis replies once per command and never speaks unbidden, so bytes
past the end of that reply cannot belong to anything we asked for. Rather than
thread a buffer that is provably empty at every command boundary, `read_reply`
checks for those bytes and reports the desync — a silent corruption becomes a
loud, typed one.

**Pub/Sub gets its own type** because it is the case that breaks the
invariant. `Subscription` owns a read buffer and is threaded through each call,
since several pushed messages really can share one TCP segment.

**One connection per process.** Two processes writing down one socket would
interleave requests and read each other's replies. `with_conn` makes a
per-process connection a one-liner; `example/tour.scrl` spawns workers that way.

**Sentinels become constructors.** A TTL is `Expires(n) | NoExpiry | Missing`,
not `-1` and `-2` waiting to be used in arithmetic. A key's type is `KeyType`.
A hash field's expiry outcome is `FieldExpiry`. Redis's own `0`/`1`/`-2` codes
never reach a caller.

**Scores are `Score`, not `Float`.** Redis scores can be infinite; Scarlet
floats cannot — `1.0 / 0.0` is `0.0`, not `inf`. Returning a Float would render
an infinite score as zero, so `Score` is `Finite(Float) | PosInf | NegInf`.
`ZADD key +inf member` round-trips.

**Mutually exclusive options are sum types.** `SET key v EX 60 KEEPTTL` is a
server error, so `SetExpiry` makes it unwritable rather than merely
ill-advised. Same for score bounds — `Inclusive`/`Exclusive` over a `Score`,
which is where the `(` in `(1.5` comes from.

**Errors split three ways.** `Net` means the connection is gone, `Server` means
Redis understood and refused, `Protocol` means the bytes were not the RESP we
expected. An error reply arrives as `Err(Server(..))` rather than an `Ok`
holding an error variant, so "wrong number of arguments" cannot be read as
success.

## Coverage

275 of 288 core commands. Not included, and why:

- **Protocol-mode changes** — `MONITOR`, `PSYNC`, `REPLCONF`. Each turns the
  connection into a one-way stream, the same thing pub/sub needed its own type
  for. Sending one down a `Conn` would desync it.
- **Internal** — `DEBUG`, `PFDEBUG`, `PFSELFTEST`, `XIDMPRECORD`,
  `RESTORE-ASKING`.
- **Redis 8.10 containers with no published argument schema** — `HIMPORT`,
  `HOTKEYS`, `BACKUP`, `TRIMSLOTS`. `COMMAND DOCS` lists their subcommands but
  not their arguments, and guessing a wire format is worse than leaving the
  escape hatch.

The 159 module commands (`FT.*`, `JSON.*`, `TS.*`, `BF.*`) are separate
products and out of scope; `redis.command` reaches them.

Cluster commands are implemented, but this client does not route: a key on
another shard comes back as a `Server` error carrying the MOVED redirect.
`cluster.scrl` is the parts a routing layer would be built from.

## Known limits

- **RESP2 only.** The decoders assume RESP2 shapes — a map is a flat array, a
  double is a bulk string. `connection.hello` deliberately takes no version.
- **No pipelining.** One command in flight per connection. Pipelining would
  need the read buffer that `Conn` is able to prove it does not need.
- **Float parsing is exact to 15 significant digits**, which covers everything
  Redis's shortest-round-trip formatting emits for all but a small minority of
  doubles. A 16- or 17-digit value rounds twice and can land one ULP out;
  closing that needs arbitrary-precision arithmetic. See `number.scrl`.

## Notes on the language

Two stdlib gaps this ran into, both worked around in `number.scrl`:
`binary.parse_int` reads unsigned digits only, so RESP's `:-1` and `$-1` need
their own parse; and nothing anywhere turns `'1.5'` into a `Float`. Scarlet
also has no string comparison operator (`<` is numeric-only), and no message
passing between processes — `scheduler.spawn` is fire-and-forget with no
mailbox, which is why the connection is a value rather than an actor.
