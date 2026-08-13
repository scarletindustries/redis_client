#!/usr/bin/env python3
"""TLS-terminating RESP peer that counts client-to-server records.

A pipeline over TLS is one `tls.write` in redis.scrl. test/tls.scrl cannot
see that: N separate writes produce the same RESP replies in the same order.
This process parses the 5-byte TLS record headers off the raw TCP stream
before decrypting, which is the side of the wire that can tell 1 from 3.

TLS 1.3 encrypts post-ServerHello handshake traffic as outer type 23, so
counting every application-data record from byte 0 folds in the client's
Finished. Handshake records are not counted. HELLO 3 (the dial) is not
counted. Only records that arrive while reading the three-command pipeline
are counted.

Not Redis. Speaks just enough RESP3 for connect_tls + pipeline(SET, INCR, GET).
Uses ipsan.crt so the client can dial 127.0.0.1 (server.crt has no IP SAN).

Run through scripts/tls-records, not by hand against the compose Redis.
"""

from __future__ import annotations

import argparse
import ssl
import socket
import sys
import time


CONTENT_TYPE_APP = 23


def take_record(buf: bytes) -> tuple[bytes | None, bytes]:
    if len(buf) < 5:
        return None, buf
    length = int.from_bytes(buf[3:5], "big")
    need = 5 + length
    if len(buf) < need:
        return None, buf
    return buf[:need], buf[need:]


def parse_commands(buf: bytes) -> tuple[list[list[bytes]], bytes]:
    commands: list[list[bytes]] = []
    pos = 0
    while True:
        cmd, nxt = parse_one(buf, pos)
        if cmd is None:
            break
        commands.append(cmd)
        pos = nxt
    return commands, buf[pos:]


def parse_one(buf: bytes, i: int) -> tuple[list[bytes] | None, int]:
    if i >= len(buf) or buf[i] != ord("*"):
        return None, i
    end = buf.find(b"\r\n", i)
    if end < 0:
        return None, i
    try:
        n = int(buf[i + 1 : end])
    except ValueError as e:
        raise ValueError(f"bad array length at {i}") from e
    if n < 0:
        return None, i
    pos = end + 2
    parts: list[bytes] = []
    for _ in range(n):
        if pos >= len(buf) or buf[pos] != ord("$"):
            return None, i
        end = buf.find(b"\r\n", pos)
        if end < 0:
            return None, i
        try:
            length = int(buf[pos + 1 : end])
        except ValueError as e:
            raise ValueError(f"bad bulk length at {pos}") from e
        pos = end + 2
        if length < 0 or pos + length + 2 > len(buf):
            return None, i
        parts.append(buf[pos : pos + length])
        pos += length
        if buf[pos : pos + 2] != b"\r\n":
            raise ValueError("bulk string missing CRLF")
        pos += 2
    return parts, pos


def flush_out(outgoing: ssl.MemoryBIO, sock: socket.socket) -> None:
    data = outgoing.read()
    if data:
        sock.sendall(data)


def try_read(obj: ssl.SSLObject) -> bytes:
    chunks: list[bytes] = []
    while True:
        try:
            data = obj.read()
        except ssl.SSLWantReadError:
            break
        except ssl.SSLZeroReturnError:
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


def feed_records(
    leftover: bytes,
    incoming: ssl.MemoryBIO,
    outgoing: ssl.MemoryBIO,
    sock: socket.socket,
    count_app: bool,
) -> tuple[bytes, int]:
    counted = 0
    while True:
        rec, leftover = take_record(leftover)
        if rec is None:
            break
        if count_app and rec[0] == CONTENT_TYPE_APP:
            counted += 1
        incoming.write(rec)
        flush_out(outgoing, sock)
    return leftover, counted


def recv_more(sock: socket.socket, leftover: bytes) -> tuple[bytes, bool]:
    try:
        chunk = sock.recv(65536)
    except socket.timeout:
        return leftover, True
    if not chunk:
        return leftover, True
    return leftover + chunk, False


def reply_for(cmd: list[bytes], store: dict[bytes, bytes]) -> bytes:
    if not cmd:
        return b"-ERR empty command\r\n"
    name = cmd[0].upper()
    if name == b"HELLO":
        return b"+OK\r\n"
    if name == b"SET" and len(cmd) >= 3:
        store[cmd[1]] = cmd[2]
        return b"+OK\r\n"
    if name == b"INCR" and len(cmd) >= 2:
        raw = store.get(cmd[1], b"0")
        try:
            n = int(raw) + 1
        except ValueError:
            return b"-ERR value is not an integer or out of range\r\n"
        store[cmd[1]] = str(n).encode("ascii")
        return f":{n}\r\n".encode("ascii")
    if name == b"GET" and len(cmd) >= 2:
        val = store.get(cmd[1])
        if val is None:
            return b"$-1\r\n"
        return f"${len(val)}\r\n".encode("ascii") + val + b"\r\n"
    if name == b"PING":
        return b"+PONG\r\n"
    return b"-ERR unknown command\r\n"


def tls_send(
    obj: ssl.SSLObject,
    outgoing: ssl.MemoryBIO,
    sock: socket.socket,
    data: bytes,
) -> None:
    obj.write(data)
    flush_out(outgoing, sock)


def write_done(path: str | None, commands: int, records: int, why: str) -> None:
    line = f"PEER DONE commands={commands} records={records}"
    print(line, flush=True)
    if why:
        print(f"PEER DETAIL {why}", flush=True)
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{line}\n")
            if why:
                f.write(f"PEER DETAIL {why}\n")


def serve(args: argparse.Namespace) -> int:
    cert = f"{args.certs}/ipsan.crt"
    key = f"{args.certs}/ipsan.key"
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", args.port))
    listener.listen(1)
    listener.settimeout(args.timeout)
    port = listener.getsockname()[1]
    if args.port_file:
        with open(args.port_file, "w", encoding="utf-8") as f:
            f.write(f"{port}\n")
    print(f"listening 127.0.0.1:{port}", flush=True)

    try:
        conn, _ = listener.accept()
    except socket.timeout:
        write_done(args.result_file, 0, 0, "accept timed out")
        return 1
    finally:
        listener.close()

    conn.settimeout(args.timeout)
    incoming = ssl.MemoryBIO()
    outgoing = ssl.MemoryBIO()
    obj = ctx.wrap_bio(incoming, outgoing, server_side=True)

    leftover = b""
    deadline = time.monotonic() + args.timeout
    try:
        while True:
            if time.monotonic() > deadline:
                write_done(args.result_file, 0, 0, "handshake timed out")
                return 1
            try:
                obj.do_handshake()
                flush_out(outgoing, conn)
                break
            except ssl.SSLWantReadError:
                flush_out(outgoing, conn)
                leftover, eof = recv_more(conn, leftover)
                if eof:
                    write_done(args.result_file, 0, 0, "eof during handshake")
                    return 1
                leftover, _ = feed_records(
                    leftover, incoming, outgoing, conn, count_app=False
                )

        print("handshake ok", flush=True)

        store: dict[bytes, bytes] = {}
        plain = try_read(obj)
        leftover, _ = feed_records(
            leftover, incoming, outgoing, conn, count_app=False
        )
        plain += try_read(obj)

        hello_cmds: list[list[bytes]] = []
        while len(hello_cmds) < 1:
            if time.monotonic() > deadline:
                write_done(args.result_file, 0, 0, "timeout waiting for HELLO")
                return 1
            cmds, plain = parse_commands(plain)
            hello_cmds.extend(cmds)
            if len(hello_cmds) >= 1:
                break
            leftover, eof = recv_more(conn, leftover)
            if eof:
                write_done(args.result_file, 0, 0, "eof waiting for HELLO")
                return 1
            leftover, _ = feed_records(
                leftover, incoming, outgoing, conn, count_app=False
            )
            plain += try_read(obj)

        hello, extra = hello_cmds[0], hello_cmds[1:]
        if not hello or hello[0].upper() != b"HELLO":
            name = hello[0] if hello else b""
            write_done(
                args.result_file,
                0,
                0,
                f"first command was {name!r}, not HELLO",
            )
            return 1
        print("hello", flush=True)
        tls_send(obj, outgoing, conn, reply_for(hello, store))

        # Extra commands that rode in with HELLO are not the pipeline write.
        pipeline: list[list[bytes]] = list(extra)
        records = 0
        while len(pipeline) < 3:
            if time.monotonic() > deadline:
                write_done(
                    args.result_file,
                    len(pipeline),
                    records,
                    "timeout waiting for pipeline",
                )
                return 1
            cmds, plain = parse_commands(plain)
            pipeline.extend(cmds)
            if len(pipeline) >= 3:
                break
            leftover, eof = recv_more(conn, leftover)
            if eof:
                write_done(
                    args.result_file,
                    len(pipeline),
                    records,
                    "eof waiting for pipeline",
                )
                return 1
            leftover, nrec = feed_records(
                leftover, incoming, outgoing, conn, count_app=True
            )
            records += nrec
            plain += try_read(obj)

        # Drop anything past the three we asked for; the client sent a batch
        # of three and will read three replies.
        pipeline = pipeline[:3]
        print(
            f"pipeline commands={len(pipeline)} records={records}",
            flush=True,
        )
        replies = b"".join(reply_for(cmd, store) for cmd in pipeline)
        tls_send(obj, outgoing, conn, replies)
        write_done(args.result_file, len(pipeline), records, "")
        return 0 if len(pipeline) == 3 else 1
    except Exception as e:
        write_done(args.result_file, 0, 0, f"{type(e).__name__}: {e}")
        return 1
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--certs", default="test/tls")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--port-file", default="")
    p.add_argument("--result-file", default="")
    p.add_argument("--timeout", type=float, default=20.0)
    return serve(p.parse_args())


if __name__ == "__main__":
    sys.exit(main())
