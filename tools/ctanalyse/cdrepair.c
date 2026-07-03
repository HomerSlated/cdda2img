/* ctanalyse — syndrome pass, offset detection, decode, corrections.
 * Model per ALGORITHMS.md; conventions pinned empirically against CTDB entry
 * 67116 (2026-07-02). See cdrepair.h.
 */

#include "cdrepair.h"

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "crc32.h"
#include "galois16.h"
#include "rs16.h"

/* ---- pass 1: canonical data syndromes ---------------------------------- */

/* split tables for "multiply by alpha^i": mul(x, a^i) = lo[i][x&255] ^ hi[i][x>>8] */
static uint16_t (*syn_lo)[256];
static uint16_t (*syn_hi)[256];

static void build_syn_tables(int npar)
{
	syn_lo = malloc((size_t)npar * sizeof(*syn_lo));
	syn_hi = malloc((size_t)npar * sizeof(*syn_hi));
	for (int i = 0; i < npar; i++) {
		syn_lo[i][0] = syn_hi[i][0] = 0;
		for (int b = 1; b < 256; b++) {
			syn_lo[i][b] = (uint16_t)gf_mulexp(b, i);
			syn_hi[i][b] = (uint16_t)gf_mulexp(b << 8, i);
		}
	}
}

typedef struct {
	const cta_ctx *c;
	uint16_t *S1;
	int part_a, part_b;
} syn_job;

static void *syn_worker(void *arg)
{
	syn_job *j = arg;
	const cta_ctx *c = j->c;
	const int npar = c->npar;
	const uint16_t *pcm = c->pcm;

	for (int r = 1; r <= c->stridecount; r++) {
		const uint16_t *row = pcm + (size_t)r * c->stride;
		for (int part = j->part_a; part < j->part_b; part++) {
			uint16_t d = row[part];
			uint16_t *S = j->S1 + (size_t)part * npar;
			for (int i = 0; i < npar; i++) {
				uint16_t s = S[i];
				S[i] = d ^ syn_lo[i][s & 0xFF] ^ syn_hi[i][s >> 8];
			}
		}
	}
	return NULL;
}

void cta_syndromes(const cta_ctx *c, uint16_t *S1)
{
	build_syn_tables(c->npar);
	memset(S1, 0, (size_t)c->stride * c->npar * sizeof(uint16_t));

	int nt = c->threads;
	if (nt < 1)
		nt = 1;
	if (nt > c->stride)
		nt = c->stride;

	pthread_t *tid = malloc((size_t)nt * sizeof(*tid));
	syn_job *jobs = malloc((size_t)nt * sizeof(*jobs));
	int per = c->stride / nt;
	for (int t = 0; t < nt; t++) {
		jobs[t] = (syn_job){c, S1, t * per, (t == nt - 1) ? c->stride : (t + 1) * per};
		pthread_create(&tid[t], NULL, syn_worker, &jobs[t]);
	}
	for (int t = 0; t < nt; t++)
		pthread_join(tid[t], NULL);
	free(tid);
	free(jobs);
}

/* ---- column window at an offset ---------------------------------------- */

/* Error syndrome E[npar] for DB column part2 at offset delta (stereo samples):
 * E_i = S_window,i ^ Sdb[part2][i], where the window is our words
 * start + j*stride, start = stride + part2 + 2*delta. The window equals a
 * canonical column slid up/down by at most one row, so it is derived from S1
 * with two boundary-word reads (ALGORITHMS.md §5). Returns start word. */
static size_t column_error_syndrome(const cta_ctx *c, const uint16_t *S1,
                                    const uint16_t *Sdb, int part2, int delta,
                                    int *E)
{
	const int npar = c->npar, stride = c->stride, sc = c->stridecount;
	const uint16_t *pcm = c->pcm;
	int q = part2 + 2 * delta; /* in (-stride, 2*stride) for |2*delta| < stride */
	int part, base;

	if (q < 0) {
		part = q + stride;
		base = 0;
	} else if (q >= stride) {
		part = q - stride;
		base = 2;
	} else {
		part = q;
		base = 1;
	}

	const uint16_t *S = S1 + (size_t)part * npar;
	int wexp = (sc - 1) % GF_MAX;
	for (int i = 0; i < npar; i++) {
		int s = S[i];
		if (base == 0) {
			/* slide up one row: drop row sc, prepend row 0 */
			s = gf_divexp(s ^ pcm[(size_t)sc * stride + part], i);
			s ^= gf_mulexp(pcm[part], (i * wexp) % GF_MAX);
		} else if (base == 2) {
			/* slide down one row: drop row 1, append row sc+1 */
			s = gf_mulexp(s ^ gf_mulexp(pcm[(size_t)stride + part], (i * wexp) % GF_MAX), i);
			s ^= pcm[(size_t)(sc + 1) * stride + part];
		}
		E[i] = s ^ Sdb[(size_t)part2 * npar + i];
	}
	return (size_t)base * stride + part; /* == start word of the window */
}

/* ---- offset detection (CDRepair.FindOffset semantics) ------------------- */

bool cta_find_offset(const cta_ctx *c, const uint16_t *S1, const uint16_t *Sdb,
                     int max_offset, int *offset)
{
	const int npar = c->npar;
	int E[RS_MAX_NPAR];
	int sigma[RS_MAX_NPAR / 2 + 2];
	int pos[RS_MAX_NPAR / 2];

	if (max_offset > c->stride / 2 - 1)
		max_offset = c->stride / 2 - 1;

	for (int allowed = 0; allowed < npar / 2; allowed++) {
		for (int k = 0; k <= 2 * max_offset; k++) {
			int delta = (k % 2) ? -(k + 1) / 2 : k / 2; /* 0,-1,1,-2,2,… */
			column_error_syndrome(c, S1, Sdb, 0, delta, E);

			int err = 0;
			for (int i = 0; i < npar; i++)
				err |= E[i];
			if (allowed == 0) {
				if (err == 0) {
					*offset = delta;
					return true;
				}
				continue;
			}
			if (err == 0)
				continue; /* clean would have matched at allowed == 0 */
			int jisu = rs_calc_sigma_mbm(npar, E, sigma);
			if (jisu == allowed &&
			    rs_chien_search(pos, c->stridecount, jisu, sigma)) {
				*offset = delta;
				return true;
			}
		}
	}
	return false;
}

/* ---- erasure buckets (C2 pointers -> grid rows) ------------------------- */

void cta_build_erasures(const cta_ctx *c, int offset, const uint8_t *bits,
                        size_t nbits, cta_erasures *er)
{
	const int stride = c->stride, cap = c->npar;
	er->stride = stride;
	er->cap = cap;
	er->total = 0;
	er->count = calloc((size_t)stride, sizeof(int));
	er->rows = malloc((size_t)stride * cap * sizeof(int));

	/* Invert word = (row+1)*stride + part2 + 2*offset: u = w - 2*offset - stride,
	 * then part2 = u mod stride, row = u / stride — the same transform, backwards. */
	const long shift = 2L * offset + stride;
	if (nbits > c->nwords)
		nbits = c->nwords;
	const size_t nbytes = (nbits + 7) / 8;
	for (size_t byte = 0; byte < nbytes; byte++) {
		uint8_t bb = bits[byte];
		if (!bb)
			continue;
		for (int bit = 0; bit < 8; bit++) {
			if (!(bb & (1u << bit)))
				continue;
			size_t w = byte * 8 + bit;
			if (w >= nbits)
				break;
			long u = (long)w - shift;
			if (u < 0)
				continue;
			int part2 = (int)(u % stride);
			long row = u / stride;
			if (row >= c->stridecount)
				continue;
			int *cnt = &er->count[part2];
			if (*cnt < cap) {
				er->rows[(size_t)part2 * cap + *cnt] = (int)row;
				(*cnt)++;
				er->total++;
			}
		}
	}
}

void cta_free_erasures(cta_erasures *er)
{
	free(er->rows);
	free(er->count);
	er->rows = NULL;
	er->count = NULL;
}

/* ---- full verify + decode ----------------------------------------------- */

static int corr_cmp(const void *a, const void *b);

void cta_verify(const cta_ctx *c, const uint16_t *S1, const uint16_t *Sdb,
                int offset, const cta_erasures *er, cta_result *res)
{
	const int npar = c->npar;
	int E[RS_MAX_NPAR];
	/* npar (not npar/2): the erasure path can return up to npar errata. */
	int err_j[RS_MAX_NPAR];
	int err_val[RS_MAX_NPAR];

	size_t cap = 1024;
	res->corr = malloc(cap * sizeof(*res->corr));
	res->n_corr = 0;
	res->can_recover = true;
	res->dirty_columns = 0;
	res->corrected_errors = 0;
	res->erasure_columns = 0;

	for (int part2 = 0; part2 < c->stride; part2++) {
		size_t start = column_error_syndrome(c, S1, Sdb, part2, offset, E);

		int err = 0;
		for (int i = 0; i < npar; i++)
			err |= E[i];
		if (!err)
			continue;
		res->dirty_columns++;

		int e = (er && part2 < er->stride) ? er->count[part2] : 0;
		const int *eras = e ? er->rows + (size_t)part2 * er->cap : NULL;

		int n;
		if (e > 0 && e <= npar) {
			n = rs_decode_column(npar, E, c->stridecount, eras, e, err_j, err_val);
			if (n <= 0) /* erasures didn't help (over budget / false flags): error-only */
				n = rs_decode_column(npar, E, c->stridecount, NULL, 0, err_j, err_val);
			else
				res->erasure_columns++;
		} else {
			n = rs_decode_column(npar, E, c->stridecount, NULL, 0, err_j, err_val);
		}
		if (n <= 0) {
			res->can_recover = false;
			continue; /* keep scanning: dirty_columns stays informative */
		}
		res->corrected_errors += n;
		for (int i = 0; i < n; i++) {
			size_t word = start + (size_t)err_j[i] * c->stride;
			if (word >= c->nwords) {
				res->can_recover = false;
				continue;
			}
			if (res->n_corr == cap) {
				cap *= 2;
				res->corr = realloc(res->corr, cap * sizeof(*res->corr));
			}
			uint16_t old = c->pcm[word];
			res->corr[res->n_corr++] = (cta_correction){
				.word = word, .old = old, .new_ = old ^ (uint16_t)err_val[i]};
		}
	}
	/* word order: sequential splice for the driver, clean sector dedup in JSON */
	qsort(res->corr, res->n_corr, sizeof(*res->corr), corr_cmp);
}

static int corr_cmp(const void *a, const void *b)
{
	const cta_correction *ca = a, *cb = b;
	return (ca->word > cb->word) - (ca->word < cb->word);
}

/* ---- region CRC (consensus window) -------------------------------------- */

uint32_t cta_region_crc(const cta_ctx *c, int offset,
                        const cta_correction *corr, size_t n_corr)
{
	/* their words [stride, W - laststride) == our words shifted by 2*offset */
	ptrdiff_t a = (ptrdiff_t)c->stride + 2 * offset;
	ptrdiff_t b = (ptrdiff_t)c->nwords - c->laststride + 2 * offset;
	if (a < 0)
		a = 0;
	if (b > (ptrdiff_t)c->nwords)
		b = (ptrdiff_t)c->nwords;

	cta_correction *sorted = NULL;
	if (n_corr) {
		sorted = malloc(n_corr * sizeof(*sorted));
		memcpy(sorted, corr, n_corr * sizeof(*sorted));
		qsort(sorted, n_corr, sizeof(*sorted), corr_cmp);
	}

	uint32_t crc = 0;
	const uint8_t *bytes = (const uint8_t *)c->pcm;
	size_t pos = (size_t)a;
	for (size_t k = 0; k < n_corr; k++) {
		size_t w = sorted[k].word;
		if (w < (size_t)a || w >= (size_t)b)
			continue;
		if (w > pos)
			crc = crc32_update(crc, bytes + pos * 2, (w - pos) * 2);
		uint8_t patched[2] = {(uint8_t)(sorted[k].new_ & 0xFF),
		                      (uint8_t)(sorted[k].new_ >> 8)};
		crc = crc32_update(crc, patched, 2);
		pos = w + 1;
	}
	if ((size_t)b > pos)
		crc = crc32_update(crc, bytes + pos * 2, ((size_t)b - pos) * 2);
	free(sorted);
	return crc;
}
