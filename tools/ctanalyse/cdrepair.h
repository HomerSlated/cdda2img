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
	cta_correction *corr;
	size_t n_corr;
	uint32_t crc_before;
	uint32_t crc_after;
} cta_result;

/* Pass 1: canonical data syndromes S1[part*npar + i] over rows 1..stridecount.
 * S1 must hold stride*npar u16. Threaded over column ranges. */
void cta_syndromes(const cta_ctx *c, uint16_t *S1);

/* Sweep candidate offsets (spiral outward from 0, |offset| <= max_offset) using
 * DB column 0 only, escalating allowed errors like CDRepair.FindOffset.
 * Returns true and sets *offset on success. */
bool cta_find_offset(const cta_ctx *c, const uint16_t *S1, const uint16_t *Sdb,
                     int max_offset, int *offset);

/* Full verify + decode at *offset*. Fills res (corr is malloc'd, caller frees). */
void cta_verify(const cta_ctx *c, const uint16_t *S1, const uint16_t *Sdb,
                int offset, cta_result *res);

/* Region CRC32 (consensus window at *offset*), with corrections patched in
 * when corr != NULL. */
uint32_t cta_region_crc(const cta_ctx *c, int offset,
                        const cta_correction *corr, size_t n_corr);

#endif
