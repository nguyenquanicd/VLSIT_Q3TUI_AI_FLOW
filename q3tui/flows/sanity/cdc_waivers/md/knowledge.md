# CDC knowledge

Cases a CDC violation can be waived under. Each one gives the pattern to recognise, the **conditions** that must all hold, the evidence to
check (RTL, SDC, spec) and what to do when a condition cannot be verified. A rule is applied only when its conditions are verified and
cited; otherwise the violation is not waived: ask the user (`ask`), or fix the RTL / ask the spec. Project cases (`sanity/cdc_knowledge.md`,
ids `U1`, `U2`, …) are the user's additions and win over these when they disagree.

## K1 — Valid / data crossing (data path without synchronizer, valid synchronized)

Pattern: a data (bus) path from clock domain A to B has no synchronizer; a `valid` (or request / enable / handshake) signal from the same
domain A crosses to B through a synchronizer and then controls the use of the data (it selects / enables the capture of the data path).
Typical: handshakes, ADB400-style bridges.

Conditions (all):
- the synchronized valid really is the control that gates the data at the receiver (check the RTL: the data is captured / used only when the synchronized valid is seen);
- the data is stable from before valid is sent until the receiver has captured it (check the RTL and the spec's handshake: data is not changed while valid is high / before the acknowledge);
- the data path has `set_max_delay` of **1 to 1.5 receiving-clock periods** (so the data reaches the destination flops before the synchronized valid does: valid takes the synchronizer's latency).

Evidence: the RTL of the sender and the receiver; the SDC (`sdc/*.tcl`, `set_max_delay` on the data path); the spec's protocol.
Constraint to give when the SDC lacks it: `set_max_delay <1.0 to 1.5 × receiving clock period> -from <data launch flops> -to <data capture flops>` (datapath-only).
When unclear (the handshake or the stability is not shown in the RTL / spec): ask the user; never assume it.

## K2 — Quasi-static signal (no synchronizer needed)

Pattern: a signal that is configured before the IP is enabled and does not change afterwards, typically a CSR / configuration register.

Conditions:
- the spec's operation says it is written only before enable / while the receiver is idle and is static during operation (check the spec: configuration sequence, "write only when disabled", lock bits);
- or the user confirmed it (see Answers).

If the spec does not mention it: **ask the user to confirm** that the signal is quasi-static (suggested answers: "Yes, static after enable" / "No, it can change during operation"); do not waive on a guess. If it is not static: fix the RTL (synchronizer) instead.

## K3 — Gray-coded counter / pointer crossing (multi-bit synchronized)

Pattern: a Gray counter (async FIFO pointer, …) crosses from one clock domain to another, every bit through its own synchronizer (the tool flags the multi-bit crossing).

Conditions (all):
- the value is really Gray-coded (one bit changes per increment: check the RTL conversion binary → Gray before the crossing) and registered before the crossing;
- the bits cross with `set_max_delay` of **at most 1 sending-clock period** (it may be written against the receiving clock as long as the value set does not exceed one sending-clock period).

Constraint to give when the SDC lacks it: `set_max_delay <value ≤ 1 sending clock period> -from <Gray register> -to <first synchronizer flops>` (datapath-only).
If the value is not Gray (binary counter, or more than one bit can change): not waivable: fix the RTL.

## K4 — Convergence of independently synchronized error / flag signals

Pattern: several error (status, flag) signals, each crossing through its own synchronizer (possibly different ones), are combined (OR) into one summary signal; the tool reports the convergence (the synchronized signals meet in one gate).

Why it is fine: whichever error asserts makes the summary assert, and that is the expectation; the order in which the errors arrive does not matter. Check that the combination is a pure OR of the signals (an AND for an **active-low** summary: any error low makes the summary low) and nothing else uses the signals together (no encoding of the combination, no pairing of two errors as one value).
Similar cases with the same property (any one of the signals asserting is enough, and the arrival order is irrelevant) can be treated the same way; say in the reason why it is the same.
If the combined value is used as a multi-bit value (e.g. an error code), this rule does not apply.

## K5 — Multi-bit crossing protected by compare-before / compare-after

Pattern: a multi-bit value crosses with a different protection: the value before the crossing and the value after the synchronizer are compared (before the data is flagged valid) to make sure the data is stable after synchronization.

Conditions:
- the RTL contains this compare (the synchronized copy equals the source value, stable over the compare) and the data is used only when it holds (valid / enable depends on the compare result);
- the compare really covers every bit of the crossing.

When the circuit is only partly like this (some bits not compared, the compare result not gating the use): not waivable by this rule: ask the user or fix the RTL.
