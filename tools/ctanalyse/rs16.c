/* ctanalyse — Reed-Solomon decode over GF(2^16). See rs16.h for provenance. */

#include "rs16.h"

#include <stddef.h>

#include "galois16.h"

int rs_calc_sigma_mbm(int npar, const int *syn, int *sigma)
{
	int sg0[RS_MAX_NPAR + 1] = {0};
	int sg1[RS_MAX_NPAR + 1] = {0};
	int wk[RS_MAX_NPAR + 1] = {0};

	sg0[1] = 1;
	sg1[0] = 1;
	int jisu0 = 1;
	int jisu1 = 0;
	int m = -1;

	for (int n = 0; n < npar; n++) {
		int d = syn[n];
		for (int i = 1; i <= jisu1; i++)
			d ^= gf_mul(sg1[i], syn[n - i]);

		if (d != 0) {
			int logd = gf_log[d];
			for (int i = 0; i <= n; i++)
				wk[i] = sg1[i] ^ gf_mulexp(sg0[i], logd);
			int js = n - m;
			if (js > jisu1) {
				for (int i = 0; i <= jisu0; i++)
					sg0[i] = gf_divexp(sg1[i], logd);
				m = n - jisu1;
				jisu1 = js;
				jisu0 = js;
			}
			for (int i = 0; i < npar; i++)
				sg1[i] = wk[i];
		}
		for (int i = jisu0; i > 0; i--)
			sg0[i] = sg0[i - 1];
		sg0[0] = 0;
		jisu0++;
	}
	if (sg1[jisu1] == 0)
		return -1;
	int keep = npar / 2 + 2;
	if (keep > npar)
		keep = npar;
	for (int i = 0; i < keep; i++)
		sigma[i] = sg1[i];
	return jisu1;
}

void rs_mul_poly(int *seki, int lens, const int *a, int lena, const int *b, int lenb)
{
	for (int i = 0; i < lens; i++)
		seki[i] = 0;
	for (int ia = 0; ia < lena; ia++) {
		if (a[ia] != 0) {
			int loga = gf_log[a[ia]];
			int ib2 = lenb < lens - ia ? lenb : lens - ia;
			for (int ib = 0; ib < ib2; ib++)
				if (b[ib] != 0)
					seki[ia + ib] ^= gf_exp[loga + gf_log[b[ib]]];
		}
	}
}

bool rs_chien_search(int *pos, int n, int jisu, const int *sigma)
{
	/* sigma1 is the sum of all roots; peel found roots off it so the last
	 * root falls out for free (Miyazaki's optimisation). */
	int last = sigma[1];
	if (jisu == 1) {
		if (last == 0)
			return false;
		pos[0] = last;
		return gf_log[last] < n;
	}

	int sg[RS_MAX_NPAR / 2 + 2];
	for (int j = 1; j <= jisu; j++)
		sg[j] = sigma[j];
	int pos_idx = jisu - 1;

	for (int i = 0; i < n; i++) {
		/* evaluate sigma at alpha^-i (z-transform walk) */
		int wk = 1;
		for (int j = 1; j <= jisu; j++)
			wk ^= sg[j];
		for (int j = 1; j <= jisu; j++)
			sg[j] = gf_divexp(sg[j], j);
		if (wk == 0) {
			int pv = gf_exp[i];
			last ^= pv;
			pos[pos_idx--] = pv;
			if (pos_idx == 0) {
				if (last == 0)
					return false;
				pos[0] = last;
				return gf_log[last] < n;
			}
		}
	}
	return false;
}

int rs_forney(int jisu, int ps, const int *sigma, const int *omega)
{
	int zlog = GF_MAX - gf_log[ps];

	int ov = omega[0];
	for (int j = 1; j < jisu; j++)
		ov ^= gf_mulexp(omega[j], (zlog * j) % GF_MAX);

	int dv = sigma[1];
	for (int j = 2; j < jisu; j += 2)
		dv ^= gf_mulexp(sigma[j + 1], (zlog * j) % GF_MAX);

	if (dv == 0)
		return -1;
	return gf_mul(ps, gf_div(ov, dv));
}

int rs_decode_column(int npar, const int *E, int n_data,
                     const int *erasures, int n_erasures,
                     int *err_j, int *err_val)
{
	(void)erasures;
	if (n_erasures != 0)
		return -1; /* erasure decoding not implemented yet (item 8) */

	int has_err = 0;
	for (int i = 0; i < npar; i++)
		has_err |= E[i];
	if (!has_err)
		return 0;

	int sigma[RS_MAX_NPAR / 2 + 2] = {0};
	int omega[RS_MAX_NPAR / 2 + 1] = {0};
	int pos[RS_MAX_NPAR / 2];

	int jisu = rs_calc_sigma_mbm(npar, E, sigma);
	if (jisu <= 0 || jisu > npar / 2)
		return -1; /* beyond correction capacity (sigma is truncated past npar/2) */
	if (!rs_chien_search(pos, n_data, jisu, sigma))
		return -1;
	rs_mul_poly(omega, npar / 2 + 1, sigma, npar, E, npar);

	for (int i = 0; i < jisu; i++) {
		int mask = rs_forney(jisu, pos[i], sigma, omega);
		if (mask < 0)
			return -1;
		err_j[i] = gf_topos(n_data, pos[i]);
		err_val[i] = mask;
	}
	return jisu;
}
