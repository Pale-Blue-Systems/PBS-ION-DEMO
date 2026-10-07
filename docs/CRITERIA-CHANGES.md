# Changes to the scenario criteria

This file lists every change to a scenario's pass/fail criteria
(`scenarios/S*.json`, key `criteria`) made after the scenarios were first
committed in `25983b5`, the reason for each, and whether it made the
criterion weaker or stronger. Each run copies the definition it used into
`results/<S>/scenario.json`, so a recorded run can always be checked
against the criteria it was judged by.

| Commit (2026-10-07, UTC) | Criterion | Before | After | Effect |
|---|---|---|---|---|
| `ce737fa` (03:05) | S2-16 to S2-18, S3-11 to S3-13 | none | LTP span counters from `ltpstats -a` | added, informative only |
| `cba2de1` (03:35) | S5-21 | informative; listed TTL 3 and B's zero-age TTL 2 envelope with the expected outcome NOT_DELIVERED | informative; lists the observed outcome without an expected one | no effect on any verdict (informative before and after) |
| `88747a8` (03:51) | S3-8 | `no_unmatched`: no envelope received twice | `no_unknown`: every receipt is an envelope that was sent; repeats counted and listed | weaker; made after the criterion failed in the 03:35 run |
| `88747a8` (03:51) | S3-14 | none | Gateway `bpstats` re-forwarded bundles | added, informative only |
| `9dfb41f` (04:37) | S3-8 | repeats counted and listed | a repeat passes only when it is the same bundle as the first delivery (bundle source EID, creation time and count) and ION re-forwarded at least as many bundles (`bpstats` `rfw`, all nodes) | stronger than `88747a8`; still admits a repeat that ION explains |
| `25ffe97` (07:19) | S2-1, S2-2 and every other delivery criterion | receipts until the endpoints were stopped at the end of the traffic phase | receipts until the end of the drain: after the traffic phase the runner keeps the endpoints running until every envelope handed to ION has been received or its bundle lifetime has ended, at most `drain_max_s` (30 s) | weaker as a timing requirement; made after review because in run A Earth's LTP completed the blocks of two envelopes 0.29 s after the endpoints had stopped (105.33 s against 105.04 s, `scenarios/README.md` observation 6) and S2 counted them as lost. No delivery deadline is a pass/fail criterion; S2-19 and S3-15 report the margin |
| `25ffe97` | S2-19, S3-15 | none | last receipt, its margin to the end of the traffic phase, receipts during the drain | added, informative only |
| `25ffe97` | S6-1, S6-3 | 22 commands | 25 commands: MUX `frame_count` lowered to 1 and to 0 after signing (expected `REJECT SEC_MISSING`, rule R8a of SPEC-DIGEST.md A38) and a validly signed command naming an authority the rover policy does not know (expected `REJECT AUTHZ_DENIED`) | stronger (three more cases) |
| `25ffe97` | S6-8 | none (the number of rejections before S6-21 was a fixed text, 19, in the conformance mapping) | S6-21 accepted after at least one rejection; the number of rejections is measured from the rover's log | added |
| `25ffe97` | S7-4, S7-5 | exit status of the cross-check tools, which passed with envelopes over 4096 bytes not decoded (no `envelope_hex`) and did not check that every own envelope was delivered | both endpoints log the bytes of every S7 envelope (`--log-hex-max 65580`); each tool fails when a peer send record has no bytes or an own envelope was not delivered | stronger |
| `25ffe97` | S7-6, S7-8 | at least 13 envelopes | at least 15: Endpoint A also sends an unprotected envelope with an unknown frame type | traffic added |
| `25ffe97` | S1, S5, S7 traffic | Endpoint B's telemetry, status, event and frames envelopes unsigned | signed (rule R8a rejects an unsigned Earth-MCC envelope with a payload) | traffic changed to meet the new receive rule; expected dispositions unchanged |
| `25ffe97` | S7-1 (through `docs/test-vectors.json`) | RX-20 (padding, no Security frame) expected `ACCEPT OK` | expected `REJECT SEC_MISSING`; RX-23, CMD-19 to CMD-22 and SB-22 to SB-25 added | rule R8a (A38) |

## S3-8: why a repeated delivery is admitted

In the 03:35 run (`cba2de1`) Earth received one BULK envelope twice, 0.61 s
apart, with the same bundle creation time; the Gateway had cancelled an LTP
block towards Earth and BP had forwarded the bundle again
(`scenarios/README.md`, observation 4; that run's files were not kept). ION 4.2.0
cancels an LTP export session when its checkpoint has timed out maxTimeouts
times (`ltp/library/libltpP.c`, about line 9665); every node logs "max
timeouts 5" at start. ION delivered both copies to the endpoint; the nodes
run without custody transfer. PBS-ENV-01 Section 8 asks receivers to track
sequence numbers to detect lost packets and has no rule to discard a
repeated telemetry envelope, and PBS-SVC-01 Section 5 defines an
AT_LEAST_ONCE delivery mode in which delivery may repeat. A higher LTP
retry limit would make such cancellations rarer but, under random loss,
not impossible, and nothing in this configuration removes the second copy
at the destination. S3 therefore admits a repeat that ION explains instead
of requiring that none occurs. No envelope of the demo declares a delivery
mode (no Service Intent frames, SPEC-DIGEST.md A23), so AT_LEAST_ONCE is an
analogy here, not the applicable mode.

The criterion still fails for any receipt that is not an envelope that
was sent, for a repeat that is a different bundle (the sender handed the
envelope to ION twice), and for more repeats than bundles ION re-forwarded.
`scenarios/test_scenarios.py` (class `RepeatedDelivery`) checks these
cases on synthetic logs. In every run since `9dfb41f` the number of
repeats is reported in S3-8's measured value; REPORT.md prints the note
stored with the criterion.

## Failures fixed without changing a criterion

| Run | Scenario | Cause | Fix |
|---|---|---|---|
| fresh clone of `9dfb41f`, 04:37 (run A) | S2 (S2-1, S2-4): two rover BULK envelopes reached Earth after the traffic phase | ION discarded red LTP segments at Earth while 10 blocks waited for delivery to BP (`m maxbacklog`, default 10); see `ion/README.md` item 15 and `scenarios/README.md` observation 6 | `de92b9c`: `m maxbacklog` set to the blocks the node's peers can have open (32 at the rover and Earth, 64 at the Gateway). S2 then passed in an S2-only run (04:51) and in the full fresh-clone runs B, C, D and E; four of these five had the same screening at Earth that preceded run A's failure (33 or 34 segments), and none discarded a segment for backlog |

Run A's files are not in the repository (`scenarios/README.md`, "All runs
on 2026-10-07"). `runs/control/` holds two S2 runs with the default
limit: both passed, and they discarded 1 and 5 segments for backlog.

## Repeated deliveries in S3 since the change of `9dfb41f`

S3-8 in its current form was applied to the complete runs A to G
(`scenarios/README.md`). Runs D, E and F each had one repeated delivery (a
BULK envelope received twice with the same bundle creation time and count;
the Gateway had re-forwarded 2, 3 and 3 bundles), and S3-8 passed; runs A,
B, C and G had none. REPORT.md lists the repeated deliveries of the run in
`results/` under S3.
