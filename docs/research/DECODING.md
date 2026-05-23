# CD-DA Physical Decoding Reference

Summary of `private/code/redbook/` — a Python implementation that decodes compact disc
digital audio from the raw optical pickup signal to PCM samples. Author: carrotIndustries
(Lukas). Upstream: https://github.com/carrotIndustries/redbook

---

## What it does

Decodes a raw analog waveform captured from a DVD player's optical pickup (4 MSa at 20 MSa/s
via oscilloscope) all the way through to correct 16-bit stereo PCM, verifying at each stage
against reference rips and Q-channel CRC checks.

---

## Pipeline (in `analyze.py` → `decode.py`)

### 1. Interpolation and threshold detection (`analyze.py`)

The oscilloscope signal is a two-level analog waveform corresponding to pits and lands. Linear
interpolation at 20× oversampling gives sub-sample timing precision for clock recovery; a
hysteresis comparator converts the interpolated signal to a bitstream.

### 2. Clock recovery PLL (`analyze.py`)

A software PLL tracks the disc's embedded clock (EFM encoding guarantees ≥3, ≤11 consecutive
identical bits, providing transitions for the PLL to lock to):

- **VCO**: Numerically Controlled Oscillator (NCO) — phase accumulator wraps at `acc_size`;
  samples the input on wrap.
- **Phase detector**: Reads accumulator value at every input transition; targets 180° (half
  period) at transitions.
- **Loop filter**: First-order IIR low-pass (`alpha = 0.005`).
- **Integrator**: Added for zero steady-state phase error with a non-zero frequency offset.
  Clamp is essential for relock after dropouts.

Locked FTW ≈ 42.6 (→ ~4.33 MBit/s), matching the theoretical CD bit rate of 44.1 kHz / 6 ×
588 = 4.3218 MBit/s. Note: the DVD player reads at ~4× nominal speed with periodic seek-back
dropouts to maintain correct output data rate — the decoded audio duration (0.78 s) is ~4×
longer than the capture (0.2 s).

### 3. NRZI decoding (`analyze.py`)

NRZ-I (Non-Return-to-Zero Inverted) on the disc: a `1` is a transition, a `0` is no
transition. Decoded as `nrz_bits[i] = sampled_bits[i] != sampled_bits[i-1]`.

### 4. Framing (`analyze.py`)

Each CD channel frame is 588 bits: 24-bit sync pattern + 564 payload bits. The sync pattern is
chosen to be absent from all valid EFM data, making framing unambiguous. Frames are written one
per line to `data/frames.txt`.

### 5. EFM decoding (`decode.py`, `efm.txt`)

Eight-to-Fourteen Modulation maps each data byte to a 14-bit channel word to enforce run-length
constraints. The LUT is read from `efm.txt` (OCR'd from the IEC 60908 standard; a textual
representation is also available in ECMA-130 — see `ECMA-130_2nd_edition_june_1996.txt`).
Three merging bits between consecutive EFM words ensure DC balance and run-length validity; they
are not part of the data and are discarded.

### 6. Subcode extraction and Q-channel decoding (`decode.py`)

Each frame carries 1 bit for each of 8 subcode channels (P–W). 98 consecutive frames form a
subcode block (96 payload bits), delimited by S₀/S₁ sync words that fall outside the EFM LUT.

Q-channel (bit 6 of each subcode byte) is decoded:

- ADR nibble `0001` = Mode 1 (track position): track number, index, running time, absolute time
  — all BCD-encoded.
- CRC-16 CCITT (`x¹⁶+x¹²+x⁵+1`, parity stored inverted). All blocks validated to zero
  syndrome — an important sanity check that EFM decoding is correct before attempting audio.

### 7. CIRC deinterleaving (without Reed-Solomon FEC) (`decode.py`)

Audio bytes are scrambled across frames by Cross-Interleaved Reed-Solomon Coding (CIRC) to
spread burst errors. The decoder implements the inverse transform in three stages:

1. First delay column: `Delay(0)` or `Delay(1)` alternating across 32 symbols.
2. Second delay column: `Delay(i*4)` for i from 27 down to 0 (28 symbols).
3. Deinterleave shuffle via `deinterleave_tab` (24-element permutation).
4. Third delay column: `Delay(2)` for positions in `(4,5,6,7,12,13,14,15,20,21,22,23)`,
   else `Delay(0)`.

The Reed-Solomon C₁/C₂ decoders are bypassed (pass-throughs) — this is valid for undamaged
discs since FEC only adds/removes parity bytes, leaving the data symbols unchanged.

### 8. Sample reconstruction and output (`decode.py`)

24 output bytes per frame → 6 stereo sample pairs. Each pair is combined from two bytes as
big-endian two's complement 16-bit signed integer. Output is written as a 44.1 kHz s16le WAV.

**Verification**: The decoded audio was aligned to a reference rip in Audacity using Q-channel
timestamps; inverting and summing gave silence — byte-perfect decoding confirmed.

---

## Key constants

| Constant | Value | Source |
|---|---|---|
| CD bit rate | 4.3218 MBit/s | 44.1 kHz / 6 samples × 588 bits |
| Subcode block rate | 75 Hz | 44.1 kHz / 6 / 98 |
| Frames per second | 7350 | 44.1 kHz / 6 |
| EFM minimum run | 3 channel bits | NRZ-I + EFM guarantee |
| EFM maximum run | 11 channel bits | NRZ-I + EFM guarantee |
| CRC polynomial | `0x1021` | CCITT CRC-16 |
| Parity storage | inverted | Zero syndrome on valid data |

---

## Relevance to cdda2img

- Confirms the frame structure and subcode bit layout used in `toc_parser.py` and `cdrdao_ripper.py`.
- The Q-channel Mode 1/2/3 ADR values documented in `CLAUDE.md` are directly visible in the
  decode output.
- The 75 Hz block rate explains why CD timecodes use 75 frames/second.
- The deinterleave implementation serves as a reference for understanding why cdrdao BIN output
  is s16be (the raw interleaved/descrambled byte order from the drive) while the PCM standard
  is s16le.

---

Source: https://github.com/carrotIndustries/redbook
