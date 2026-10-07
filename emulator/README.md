# Link emulator

`linkemu.py` is a user-space UDP proxy that stands between the three ION
nodes. Every LTP datagram between two nodes passes through it. It applies
one-way delay, random loss, a rate limit with a drop-tail queue, and
blackout windows, and it writes one JSON line for every datagram event. It
replaces `ion/udp_forward.py` (fixed delay only) in the scenarios. It needs
Python 3 and no other package, and no root privileges.

## Where it sits

Each node's LTP link service output (`udplso`) sends to the emulator port of
its direction; the emulator forwards to the peer's LTP link service input
(`udplsi`). The ports are in `ion/topology.json` and in each node's
`node.ltprc`; `scenarios/test_scenarios.py` checks that the two agree.

| Link (direction) | Emulator listens on | Forwards to |
|---|---|---|
| rover-gateway | 127.0.0.1:29950 | gateway udplsi 127.0.0.1:15013 |
| gateway-rover | 127.0.0.1:25099 | rover udplsi 127.0.0.1:19913 |
| gateway-earth | 127.0.0.1:25023 | earth udplsi 127.0.0.1:12313 |
| earth-gateway | 127.0.0.1:22350 | gateway udplsi 127.0.0.1:15013 |

## Link model

Each direction is modelled on its own. For a datagram of `n` bytes that
arrives at time `t`:

1. **Queue.** With a rate limit `rate_Bps`, the link sends one datagram at a
   time. The datagram waits until the previous one has been sent. If the
   bytes already waiting plus `n` exceed `queue_bytes`, it is dropped
   (`queue_full`). Without a rate limit there is no queue.
2. **Transmission start** = `max(t, end of the previous transmission)`.
   Transmission takes `n / rate_Bps` seconds.
3. **Blackout.** If the transmission start falls inside a blackout window,
   the datagram is lost (`blackout`). A datagram already sent before the
   window opens is still delivered.
4. **Loss.** With probability `loss` the datagram is lost (`loss`). Each
   link has its own pseudo-random generator, seeded with
   `"<seed>/<link name>"`, so a link's sequence of draws does not depend on
   traffic on the other links. Which datagram receives which draw depends on
   the order in which ION sends them, so the loss pattern is not the same in
   two runs with the same seed.
5. **Delivery** at transmission end + `delay_s` + a uniform draw from
   `[0, jitter_s]`. Datagrams are never reordered: a delivery time earlier
   than the previous delivery on the link is raised to it.

A lost datagram still occupies the link for its transmission time (the
sender does not know it is lost).

Scheduled outages (known to ION through the contact plan) and unscheduled
ones (not in the contact plan) are both blackout windows here; the scenario
file says which kind each one is (`scenarios/README.md`).

## Configuration

```json
{"seed": 3,
 "links": [
  {"name": "gateway-earth", "listen": 25023, "dest": 12313,
   "delay_s": 1.3, "jitter_s": 0.0, "loss": 0.05,
   "rate_Bps": null, "queue_bytes": null,
   "blackouts": [{"start_s": 40, "end_s": 55, "kind": "unscheduled",
                  "label": "unannounced loss of the Gateway-Earth link"}]}]}
```

Times are seconds relative to `--t0` (Unix epoch seconds). The scenario
runner writes this file from a scenario definition (`scenarios/scenario.py`)
and passes the scenario's traffic origin as `--t0`.

```sh
python3 emulator/linkemu.py --config emulator.json --t0 1791338844 \
    --log emulator.jsonl --stats emulator-stats.json --ready-file emulator.ready
```

`--ready-file` is created when all sockets are bound. SIGTERM or SIGINT stops
the emulator; it then writes `--stats` and exits with status 0.

## Clock

Times come from the monotonic clock plus an offset to Unix epoch time taken
once at start, so a step of the system clock during a run does not move
scheduled deliveries. The endpoints read `CLOCK_REALTIME`; on the demo host
the two differ only by the drift of the realtime clock during a run.

## Measured forwarding accuracy

The time each datagram was sent minus its scheduled delivery time, over
S1 to S7 of the two recorded runs of commit `7095caa`
(`scenarios/README.md`):

| Run | Datagrams forwarded | Median | 99th percentile | Maximum |
|---|---|---|---|---|
| run E (`results/`) | 3461 | 0.773 ms | 2.235 ms | 18.831 ms |
| run D (`results/repeat/run-D/`) | 3388 | 0.759 ms | 2.215 ms | 15.394 ms |

The one-way delays the emulator adds are therefore 0.25 s and 1.3 s plus
0.8 ms at the median. The largest value of each run was in S2 (18.8 ms and
15.4 ms); every other scenario stayed at or below 8.1 ms (run E) and
5.2 ms (run D). REPORT.md lists the figures per scenario.

## Event log (`--log`)

JSON Lines, one object per event, schema `pbs-ion-demo-linkemu/1`:

| `ev` | When | Fields |
|---|---|---|
| `start` | first line | `t_us`, `t0`, `pid`, `config` (the whole configuration) |
| `rx` | datagram received from a node | `t_us`, `link`, `n` (index of the datagram on the link, from 1), `len`; `ltp` (decoded LTP header, below) when the datagram parses as an LTP segment; `ltp_retx: true` when it is a data segment whose session, offset and length were already seen on this link |
| `drop` | datagram lost | `t_us`, `link`, `n`, `len`, `reason` (`loss`, `blackout`, `queue_full`, `send_error`); `window` and `window_kind` for `blackout` |
| `tx` | datagram forwarded | `t_us` (actual send), `link`, `n`, `len`, `due_us` (scheduled delivery time) |
| `stop` | last line | `t_us` |

`ltp` fields (RFC 5326 Section 3): `type` (segment type code), `kind` (`DS`
red data, `CP` red data checkpoint, `GD` green data, `RS` report, `RA`
report acknowledgement, `CS`/`CAS`/`CR`/`CAR` cancel), `eng` (session
originator engine number), `ses` (session number); data segments add `off`
and `dlen`, checkpoints add `cp` and `rsn`; reports add `rs`, `cp`, `ub`,
`lb`, `claims`; acknowledgements add `rs`; cancels add `reason`. The bundle
inside an LTP block is not decoded.

## Counters (`--stats`)

Per link: the configuration, `rx`, `rx_bytes`, `tx`, `tx_bytes`,
`drop_loss`, `drop_blackout`, `drop_queue_full`, `send_error` (the peer port
refused the datagram, for example before the node started),
`pending_at_stop`, `max_queue_delay_s`, `max_queue_bytes`, `ltp_segments`
(count per `kind`) and `ltp_data_retransmissions`.

## Tests

```sh
python3 emulator/test_linkemu.py -v
```

16 tests: LTP header decoding (red data, checkpoint, report, report
acknowledgement, header extension, non-LTP input), the link model (delay,
serialization at the rate limit, drop-tail queue, blackout window edges,
blackout applied at transmission start, seeded loss rate and repeatability,
independence of links, jitter without reordering, retransmission counter,
invalid configuration) and one run of `linkemu.py` as a process on two free
ports (forwarding delay, loss, log and counters). All 16 passed on
2026-10-07 (Python 3.13.16).
