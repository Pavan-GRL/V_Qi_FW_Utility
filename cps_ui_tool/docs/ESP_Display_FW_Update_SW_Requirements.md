# ESP Display FW Update Integration Document


## Scope
Applies to ESP display FW update over:

- Ethernet
- USB HS

The same SW flashing flow shall work across all three transports.

## Background
Earlier working firmware used a raw bridge model where TCP/USB traffic was forwarded to the ESP ROM bootloader more directly.

The current STM firmware expects the PC utility to use an STM API session for display FW update:

1. `API_ESP_FW_START` to arm the session
2. `API_ESP_FW_WRITE` for each ESP SLIP frame
3. `API_ESP_FW_SET_BAUD` after a successful ESP `CHANGE_BAUD` ACK, when high-baud mode is enabled other than `115200`, by default `115200` shall be used, later we can optimize to `921500`.
4. `API_ESP_FW_END` on completion or abort

Because of this, the PC utility must no longer assume a raw transparent ESP bridge on Ethernet/USB.

## STM API Frame Format
The STM API frame shall use this base header:

- Byte `0`: packet type
- Bytes `1:2`: total API frame length, little-endian
- Byte `3`: sequence number, increment every API frame, wrap after `255`
- Byte `4`: reserved
- Byte `5`: API function/purpose, `0xB1` for FW update
- Byte `6`: reserved
- Byte `7`: parameter type
- Byte `8`:
  - bits `[3:0]`: controller type
  - bits `[7:4]`: control/sub-operation
- Byte `9` : `SLIP` Protocol follows as per previous method of fw update.

For display FW update:
- controller type: `0x04`
- API function: `0xB1`

Current expected sub-operations:

- `0x00`: `API_ESP_FW_END`
- `0x01`: `API_ESP_FW_START`
- `0x02`: `API_ESP_FW_WRITE`
- `0x03`: `API_ESP_FW_SET_BAUD`
- `0x04` or another FW-agreed value: optional USB-safe encoded `API_ESP_FW_WRITE` variant, if required by USB validation

## Functional Requirements


### 1. Session Start and End
The utility shall explicitly manage session arming and teardown.

Requirements:

- Before `SYNC`, send `API_ESP_FW_START` on the same transport handle that will carry the rest of flashing traffic.
- After sending `API_ESP_FW_START`, allow a short settle delay before issuing `SYNC`.
- On successful completion, cancellation, or failure, send `API_ESP_FW_END` in a `finally`/cleanup path whenever the session was started successfully.

### 2. ESP SLIP Frame Wrapping
Each ESP ROM command shall be wrapped inside `API_ESP_FW_WRITE`.

Requirements:

- `api_buf[9:10]` shall contain the ESP SLIP frame length in little-endian format.
- For Ethernet and raw-write USB validation, `api_buf[11..]` shall contain the raw SLIP bytes exactly as produced by the ESP flasher logic.
- The utility shall preserve the existing ESP SLIP framing and checksum behavior.
- The FW API transport layer shall unwrap API replies and return only raw ESP response bytes back to the flasher core.
- The ESP ROM bootloader shall always receive the exact original SLIP bytes, regardless of whether the PC-to-STM transport carries those bytes raw or encoded.

### 3. Sequence Number Handling
The utility shall maintain STM API sequence numbers per session.

Requirements:

- Start from a valid `uint8` value.
- Increment for every STM API frame sent.
- Wrap naturally after `255`.
- Not mandatory but good to have.

### 4. Baud Handling
The utility shall support both:

- a safe validation mode at `115200`
- an optional high-baud mode using ESP `CHANGE_BAUD`

Requirements:

- In initial phases of development, runtime flash baud shall be `115200`, therfore `CHANGE_BAUD` step shall be skipped.
- Later, FW SW shall optimize this step and runtime flash baud shall be updated to is greater than `115200`, the utility shall:
  - send ESP `CHANGE_BAUD`
  - wait for a successful ESP ACK
  - then send `API_ESP_FW_SET_BAUD` with the new baud in `api_buf[9:12]` little-endian
- The utility shall support a temporary SW override that forces runtime flash baud back to `115200` for bring-up/debug when high-baud mode is unstable.

Note:
Current validation has shown that the `115200` path is reliable, while high-baud behavior still needs coordinated SW/FW validation.

### 5. STM API Size Constraint
The utility shall respect the current STM API frame size limit.

Known current constraint:

- STM API max frame length: `512` bytes
- STM `WRITE` overhead before raw ESP bytes: `11` bytes
- maximum raw SLIP bytes carried inside one STM API frame: `501` bytes

Requirements:

- The utility shall reject oversized wrapped ESP frames before transmission.
- Block sizing shall be chosen so the practical SLIP frame stays below the STM API limit.

### 6. FLASH_DATA Block Size
Block size is the main utility-side speed knob, but it must stay compatible with the STM API limit.

Requirements:

- The utility shall support a conservative block size that is proven safe with the current STM API framing.
- `256` bytes is the safe baseline and must remain available as the fallback value.
- A cautiously larger block size may be used for performance, but the utility must keep enough margin for SLIP escaping overhead.
- If a larger block size causes oversize STM API frames or write instability, the utility shall fall back to `256`.

Recommended implementation direction:

- keep `256` as the compatibility baseline
- allow a higher default only if field validation is stable
- consider future dynamic sizing based on the actual SLIP-encoded frame length

### 7. Logging and UI Behavior
For debug purposes, FW update shall stay debuggable without excessive slowdown. So SW shall have the below feasibility in initial builds atleast.

Requirements:

- Step-by-step verification shall be available, but default to `OFF`.
- When step mode is `OFF`, the flasher shall behave in automated mode from the start.
- Per-block success logging shall be throttled in automated mode.
- Progress/status updates shall be throttled in automated mode to reduce UI overhead.
- Failure, timeout, and retry-related logs shall always remain visible.

### 8 Retry and Error Handling
The utility shall preserve the existing retry model for critical operations.

Requirements:

- `SYNC`, `SPI_ATTACH`, `SPI_SET_PARAMS`, `FLASH_BEGIN`, and `FLASH_DATA` failures shall continue to support retry/abort behavior.
- STM timeout/reject sentinel responses shall be translated into meaningful utility-side errors.
- Cleanup shall still send `API_ESP_FW_END` when possible.
- USB transport write failures shall support retry without tearing down the whole application connection.

### 9. Diagnostic and Replay Support
The utility shall provide diagnostics that make USB/FW boundary failures reproducible.

Requirements:

- A user-selectable checkbox shall enable dumping replayable STM API hex frames after selecting a display firmware `.bin`.
- The dump shall include STM API header bytes and payload bytes for `START`, each ESP ROM command, each `FLASH_DATA` block, `FLASH_END`, and `END`.
- The dump shall identify `FLASH_DATA` block number, flash offset, data size, checksum, API frame length, and raw hex.
- USB echo benchmark support shall remain available for isolating endpoint transfer reliability from ESP UART behavior.

## Non-Functional Requirements

### Performance

- The utility shall minimize avoidable logging and UI churn during `FLASH_DATA`.
- The utility shall avoid unnecessary connect/disconnect cycles when a shared session already exists.
- The utility shall keep the stable `115200` mode available even if high-baud mode remains under investigation.


## Open Items

- Re-validate high-baud flashing after STM FW and utility timing are finalized.
- Decide whether the production default block size should remain conservative at `256` or move higher after field validation.
- Consider dynamic block-size selection based on real SLIP-encoded frame length instead of one fixed global size.
- Finalize the encoded write sub-operation value and encoded payload format with STM FW.
