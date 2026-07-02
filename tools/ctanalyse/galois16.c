/* ctanalyse — GF(2^16) table construction. See galois16.h for provenance. */

#include "galois16.h"

uint16_t gf_exp[2 * GF_MAX];
uint16_t gf_log[GF_MAX + 1];

void gf16_init(void)
{
	int d = 1;
	for (int i = 0; i < GF_MAX; i++) {
		gf_exp[i] = gf_exp[GF_MAX + i] = (uint16_t)d;
		gf_log[d] = (uint16_t)i;
		d <<= 1;
		if ((d >> 16) & 1)
			d = (d ^ GF_POLY) & GF_MAX;
	}
	gf_log[0] = 0; /* log(0) is undefined; callers must guard */
}
