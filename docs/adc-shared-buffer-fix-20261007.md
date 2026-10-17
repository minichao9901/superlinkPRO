# ADC cold-start initialization and SPI bulk handover

This supersedes the unsafe buffer-reclamation policy introduced in `54854b6`.
It retains the ADC pipeline/cache changes from `1d1821d`.

## Root cause and limits of the evidence

`sb_state_t` is in `.ahb_sram (NOLOAD)`, outside startup's cleared BSS sections.
The old `spi_bridge_init()` initialized only selected fields and omitted `hw_req`.
`84a05b6` included that field in ADC CAPS busy bit1. `5690cc8` left it outside the
service gate, so a nonzero uninitialized value could block ADC indefinitely with
SPI disabled. The October 6 field record contains flags=2 but no individual
hw_req dump; this reproduces a matching cold-start failure, not proof of the
specific byte value in that historical incident.

## Ownership contract

- Initialize the entire SPI state and service gate before USB starts.
- CAPS bit0 means a native OUT receive remains armed; retire it with a host ZLP
  after disabling the bridge, then read CAPS again.
- CAPS bit1 includes live IN DMA, queued data, deferred cleanup and owners.
- ADC OPEN must refuse any nonzero CAPS flags and leave all USB bookkeeping
  unchanged. Generation counters do not cancel native DMA.
- Disable/ABORT retain native in-flight flags until completion. Only actual USB
  bus reset may invalidate transfers without callbacks.
- Disabled bridge cleanup drops hardware reconfiguration and releases CS.
  The existing service gate remains authoritative; no idle background polling
  or global USB reset is added.

The matching Web changes retain IN timeout reads for the next pump, propagate
USB/control transport failures, wait for native OUT completions even after a
caller timeout, and re-read CAPS before OPEN. An abandoned ADC owner is stopped,
drained and closed before SPI control is attempted. SPI status error bits report
the previous frame error, not a per-command return code; successful retirement
is established by the disabled state and fresh CAPS/OPEN admission.

## Validation

Production state/init/ownership/completion functions are compiled in the host
regression with poisoned NOLOAD memory. Tests verify complete initialization,
USB startup ordering, refusal without bookkeeping mutation, and delayed native
IN/OUT completion before ownership transfer. Web tests cover cold CAPS, delayed
retirement, stale ADC CLOSE retries, errors, Worker read reuse and late OUT
completion after a caller timeout.

`make test-host` and Web `make test` are the offline suites. The HID-only
`make adc-owner-hw` now checks that unread DRAIN transfers block OPEN; it does
not prove a successful bulk handover and requires a clean starting state.

Hardware release checks still required: cold power-up ADC start; SPI -> ADC ->
SPI with outstanding IN/OUT requests; disconnect/error recovery; sustained
1M/2MSa/s acquisition; RTT/JScope throughput. No new board measurements or
complete firmware build are claimed by these host regressions.
