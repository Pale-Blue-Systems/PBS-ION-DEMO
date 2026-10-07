# PBS-DTN-MAP mapping profile of the PBS ION demo

Both endpoints request ION class-of-service treatment on the basis of PBS
priority. PBS-DTN-MAP-01 Section 6.3 therefore requires a mapping profile
with the four items of PBS-DTN-MAP-02 Section 5, and PBS-DTN-MAP-02 Section 4
requires the profile to document the no-expiry lifetime. This document is
that profile. It applies to Endpoint A (`endpoint-python/`) and Endpoint B
(`endpoint-c/`), which act as the gateways of PBS-DTN-MAP-01 for their own
traffic (SPEC-DIGEST.md A27), and to the BP agent they use.

| Item | Value |
|---|---|
| BP agent | NASA/JPL ION 4.2.0, tag `ion-open-source-4.2.0`, commit `568df887cb9f18aa8ec013b1a566d83215e499ae`, built by `ion/build_ion.sh` |
| Nodes | rover `ipn:99`, LUNAR GATEWAY `ipn:50` (relay), Earth `ipn:23`; BPv7 over LTP over UDP (`ion/`) |
| Profile version | the commit of this repository that holds this file |

ION source references are to that commit.

## 1. Treatment requested per priority (PBS-DTN-MAP-01 6.3)

The gateway requests the Expedited, Normal and Bulk classes of RFC 4838
Section 3.5 through ION's class of service, with the PBS-DTN-MAP-01 Section
6.3 table:

| PBS Priority | DTN class | ION `classOfService` | ION ordinal | ION egress queue |
|---|---|---|---|---|
| 0 CRITICAL | Expedited | 2 (`BP_EXPEDITED_PRIORITY`) | 0 | urgent |
| 1 HIGH | Expedited | 2 | 0 | urgent |
| 2 NORMAL | Normal | 1 (`BP_STD_PRIORITY`) | 0 | standard |
| 3 LOW | Bulk | 0 (`BP_BULK_PRIORITY`) | 0 | bulk |
| 4 BULK | Bulk | 0 | 0 | bulk |

No other treatment is requested: no custody transfer, no status reports, no
ordinal other than 0, no data label (`bp_send()` ancillary data NULL;
SPEC-DIGEST.md A31). The envelope, including its Priority byte, is the
bundle payload and is not modified (PBS-DTN-MAP-01 Sections 6.2 and 7.2).
Both endpoints log the class they passed in every `send` record
(`class_of_service`); criterion S1-10 and S4-11 check it against this table
in every run.

## 2. BP QoS mechanism (PBS-DTN-MAP-02 Section 5, item 1)

- Interface: the `classOfService` argument of ION's `bp_send()`
  (`bpv7/include/bp.h`, constants lines 52 to 54). Endpoint A calls it
  through ctypes (`endpoint-python/pbs_endpoint_a/ion.py`), Endpoint B
  directly (`endpoint-c/src/pbs_earth.c`).
- Carriage between nodes: ION's Quality of Service extension block, block
  type 193 (`QualityOfServiceBlk`, `bp.h` line 309). It is serialized as
  the CBOR array `[flags, classOfService, ordinal, dataLabel]`
  (`bpv7/library/ext/bpq/bpq.c`, `qos_serialize`) and parsed back into the
  bundle's class of service at the next ION node (`qos_parse`), so the relay
  and the destination node queue the bundle in the same class. RFC 9171
  defines no block type 193 and no class-of-service field
  (PBS-DTN-MAP-01 Section 6.3).
- The block is offered with block processing control flags 1 only, "block
  must be replicated in every fragment" (`bpq.c` `qos_offer`,
  `BLK_MUST_BE_COPIED`; `bpv7/library/bpP.h` line 258).

## 3. Queue and scheduling treatment (item 2)

- Each egress plan (one per neighbour node) has three queues: urgent,
  standard and bulk. ION inserts a bundle into the queue of its class
  (`bpv7/library/libbpP.c`, from about line 11991).
- ION's convergence-layer manager `bpclm` takes the next bundle for a
  neighbour as follows (`bpv7/daemon/bpclm.c`, `selectNextBundleForTransmission`,
  lines 95 to 157, and lines 561 to 567):
  - the urgent queue is served first whenever it holds a bundle;
  - otherwise the standard and bulk queues share the link by bytes sent,
    with service factors 2 (standard) and 1 (bulk): `bpclm` picks the
    non-empty queue with the smaller `bytes sent / service factor`, so while
    both hold bundles the standard queue gets about twice the bytes of the
    bulk queue. Standard does not have strict priority over bulk.
  - Within a queue, bundles leave in the order they were queued (ordinal 0
    for all).
- The rate of each queue's departures is limited by the contact plan's
  transmission rate for the neighbour (`a contact ... <rate>`), which `bpclm`
  meters; LTP then sends each bundle as one block (`ion/nodes/*/node.ltprc`).

## 4. Behaviour under congestion (item 3)

When bundles arrive faster than the contact rate, they wait in ION's egress
queues at the sending node; nothing is dropped for congestion by ION or by
the emulator in the demo's runs (S4-10: the emulator queue never
overflowed). The order of departure follows Section 3: CRITICAL and HIGH
leave ahead of everything queued; NORMAL leaves ahead of most of a bulk
backlog because of the 2:1 share; LOW and BULK share one queue, so a LOW
envelope waits behind every BULK envelope queued before it, and the
relative order of LOW and BULK is not preserved as priority.

Measured in S4 (rover-Gateway contact rate 4000 bytes/s, a backlog of 40
BULK envelopes of 3000 bytes queued at 2 s, then five small envelopes each
of CRITICAL, HIGH, NORMAL and LOW): see Section 7 for the values of the
recorded run, and REPORT.md, section S4, for every run in `results/`.

A bundle whose lifetime ends while it waits is deleted by ION and its
envelope is lost (S5; PBS-DTN-MAP-01 Section 6.1.1 calls this bundle
lifetime expiry, not TTL expiry).

## 5. Behaviour when the requested treatment is unavailable (item 4)

- At an ION node the treatment is always applied: every `bp_send()` class
  0, 1 or 2 has a queue.
- At a BP agent that does not implement ION's block type 193, the class is
  not available. Block 193 has none of the flags "transmit status report if
  block can't be processed", "delete bundle if block can't be processed" or
  "discard block if it can't be processed" set (RFC 9171 Section 4.2.4;
  `qos_offer` sets only flag 1), so such an agent neither deletes the bundle
  nor removes the block, and queues the bundle by its own policy, without
  the class. The envelope still carries its Priority byte unchanged, and the
  next ION node, or the destination endpoint, recovers all five classes from
  it (PBS-DTN-MAP-01 Section 6.3). The demo has no non-ION agent; this item
  is stated from the ION source and RFC 9171 and is not exercised.
- When no contact to the next node exists, the bundle waits in ION storage
  until a contact or the end of its lifetime (S2, S5).

## 6. No-expiry lifetime (PBS-DTN-MAP-02 Section 4)

- Value: 2147483647 s (2147483647000 ms), selected for ION 4.2.0.
- Reason: `bp_send()` takes the lifetime as an `int` number of seconds
  (`bp.h` line 554) and passes `lifespan * 1000` ms to `bpSend()`
  (`libbp.c` line 1561); 2147483647 is the largest value it accepts. The
  PBS-DTN-MAP-02 Section 4 preferred value of 4294967295000 ms cannot be
  passed through this interface.
- Conditions of PBS-DTN-MAP-02 Section 4: greater than 0 ms; not less than
  any lifetime assigned to an envelope with TTL > 0 (those are clamped to
  2147483647 s, SPEC-DIGEST.md 7.4); not greater than 4294967295000 ms; ION
  computes the expiration time as a 64-bit `time_t` in seconds
  (`libbpP.c` lines 5852 to 5857), which does not overflow.
- Applied when the envelope TTL is 0 and no Service Intent frame is present
  (the demo sends none, SPEC-DIGEST.md A23) and no mission expiry policy sets
  a finite limit (the demo has none).
- `BpDelivery.timeToLive` at the receiving application is an unsigned 32-bit
  count of ms and shows 4294966296 for this lifetime (SPEC-DIGEST.md 7.4);
  the receivers log it as `bundle_ttl_field_ms` and do not use it.
- Checked in every run: S1-8 (TTL 0 envelopes sent with 2147483647 s), S1-9
  and S5-5 (delivered, also after a 30 s outage), PBS-C02-T016.

## 7. Recorded measurement

S4 of run F, the run in `results/` (fresh clone of `25ffe97`, 2026-10-07
07:20 UTC; REPORT.md, section S4). Rover-Gateway contact rate 4000 bytes/s;
emulator 4400 bytes/s with a 65536-byte queue; 40 BULK envelopes of 3000
bytes queued from 2 s; five envelopes each of CRITICAL, HIGH, NORMAL and LOW
from 6 s, 3 s apart. Latency is receipt at Earth minus the envelope
Timestamp; the propagation alone is 1.55 s.

| Priority (ION queue) | Envelopes | Latency min / median / max (s) |
|---|---|---|
| CRITICAL (urgent) | 5 | 2.096 / 2.388 / 2.663 |
| HIGH (urgent) | 5 | 2.593 / 2.645 / 2.917 |
| NORMAL (standard) | 5 | 2.132 / 2.181 / 2.457 |
| LOW (bulk) | 5 | 16.764 / 22.694 / 28.620 |
| BULK (bulk) | 40 | 2.302 / 16.968 / 31.856 |

Every CRITICAL, HIGH and NORMAL envelope (and the two PING replies, HIGH)
arrived before 18 to 33 BULK envelopes that had been sent before it (S4-7);
no LOW envelope arrived before a BULK envelope sent before it (S4-9); the
emulator queue never overflowed (S4-10) and held no datagram longer than
1.668 s (S4-15), while BULK envelopes took up to 31.856 s, so the backlog
waited in ION, not in the emulated link.

Run G (`runs/run-G/`, a second fresh clone of `25ffe97`, 07:35 UTC) gave
the same picture: every range above within 0.05 s of run F's, except one
HIGH envelope that took 1.671 s (HIGH 1.671 / 2.146 / 2.920 s).
