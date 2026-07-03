/* ctanalyse — whole-disc syndrome pass, offset detection, decode, corrections.
 *
 * Grid model per ALGORITHMS.md §1/§4/§5, pinned empirically 2026-07-02:
 *   - one GF(2^16) symbol per 16-bit word; internal stride = 2 x wire stride
 *   - DB column part2 at detected offset delta (stereo samples) covers OUR
 *     words start_word + j*stride, j = 0..stridecount-1, where
 *     start_word = stride + part2 + 2*delta  (always in range for |2d| < stride)
 *   - the wire syndrome file IS the plain data syndrome, no scale factor
 */
#ifndef CTA_CDREPAIR_H
#define CTA_CDREPAIR_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
	const uint16_t *pcm; /* whole disc, s16le words */
	size_t nwords;       /* W */
	int npar;
	int stride;     /* internal, in words */
	int stridecount;
	int laststride; /* in words */
	int threads;
} cta_ctx;

typedef struct {
	size_t word;  /* offset into OUR pcm, in words */
	uint16_t old; /* current (damaged) value */
	uint16_t new_; /* corrected value */
} cta_correction;

typedef struct {
	bool can_recover;
	bool offset_found;
	int offset; /* stereo samples, driver sign convention */
	int corrected_errors;
	int dirty_columns;
	int erasure_columns; /* columns decoded with C2 erasures (item 8) */
	cta_correction *corr;
	size_t n_corr;
	uint32_t crc_before;
	uint32_t crc_after;
} cta_result;

/* Per-column erasure buckets: C2 error pointers mapped to grid rows at the detected
 * offset. rows[part2*cap + k] is the k-th erasure row (data index) of column part2;
 * count[part2] is that column's erasure count (capped at cap == npar). */
typedef struct {
	int *rows;
	int *count;
	int cap;
	int stride;
	long total;
} cta_erasures;

/* Pass 1: canonical data syndromes S1[part*npar + i] over rows 1..stridecount.
 * S1 must hold stride*npar u16. Threaded over column ranges. */
void cta_syndromes(const cta_ctx *c, uint16_t *S1);

/* Sweep candidate offsets (spiral outward from 0, |offset| <= max_offset) using
 * DB column 0 only, escalating allowed errors like CDRepair.FindOffset.
 * Returns true and sets *offset on success. */
bool cta_find_offset(const cta_ctx *c, const uint16_t *S1, const uint16_t *Sdb,
                     int max_offset, int *offset);

/* Build per-column erasure buckets from a per-word flag bitmap (LSB-first: word w
 * is flagged iff bits[w>>3] & (1<<(w&7))). Each flagged word maps to (column, row)
 * via the same start = stride + part2 + 2*offset transform the syndromes use, so
 * erasures land in the grid cells the syndromes describe. Caller frees via
 * cta_free_erasures. */
void cta_build_erasures(const cta_ctx *c, int offset, const uint8_t *bits,
                        size_t nbits, cta_erasures *er);
void cta_free_erasures(cta_erasures *er);

/* Full verify + decode at *offset*. When er != NULL, columns with C2 erasures use
 * errors-and-erasures decoding (falling back to error-only if that fails). Fills
 * res (corr is malloc'd, caller frees). */
void cta_verify(const cta_ctx *c, const uint16_t *S1, const uint16_t *Sdb,
                int offset, const cta_erasures *er, cta_result *res);

/* Region CRC32 (consensus window at *offset*), with corrections patched in
 * when corr != NULL. */
uint32_t cta_region_crc(const cta_ctx *c, int offset,
                        const cta_correction *corr, size_t n_corr);

#endif
