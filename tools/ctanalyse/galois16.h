/* ctanalyse — GF(2^16) arithmetic.
 *
 * Port of Galois/Galois16 from CUETools.Parity (GPL), itself from Masayuki
 * Miyazaki's Reed-Solomon library (sourceforge.jp/projects/reedsolomon/).
 * See ALGORITHMS.md §2.
 */
#ifndef CTA_GALOIS16_H
#define CTA_GALOIS16_H

#include <stdint.h>

#define GF_POLY 0x1100B
#define GF_MAX 0xFFFF /* field size - 1; also the log of alpha^0 wrap */

/* exp table is doubled so mul/mulexp never need a mod */
extern uint16_t gf_exp[2 * GF_MAX];
extern uint16_t gf_log[GF_MAX + 1];

void gf16_init(void);

/* = a * b */
static inline int gf_mul(int a, int b)
{
	return (a == 0 || b == 0) ? 0 : gf_exp[gf_log[a] + gf_log[b]];
}

/* = a * alpha^k, k in [0, GF_MAX) */
static inline int gf_mulexp(int a, int k)
{
	return (a == 0) ? 0 : gf_exp[gf_log[a] + k];
}

/* = a / alpha^k, k in [0, GF_MAX) */
static inline int gf_divexp(int a, int k)
{
	return (a == 0) ? 0 : gf_exp[gf_log[a] - k + GF_MAX];
}

/* = a / b, b != 0 */
static inline int gf_div(int a, int b)
{
	return (a == 0) ? 0 : gf_exp[gf_log[a] - gf_log[b] + GF_MAX];
}

/* Chien root value -> position from sequence start (length n) */
static inline int gf_topos(int n, int a)
{
	return n - 1 - gf_log[a];
}

#endif
