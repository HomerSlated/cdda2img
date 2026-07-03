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

	/* sized for the errata locator (degree up to npar in the all-erasure case),
	 * not just the error-only npar/2. */
	int sg[RS_MAX_NPAR + 2];
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

/* Emit the jisu errata at Chien roots pos[] via Forney, then re-validate that the
 * found errata reproduce E exactly (guards against a silent miscorrection when the
 * errata exceed capacity). Returns jisu on success, -1 on failure. */
static int rs_emit_and_validate(int npar, const int *E, int n_data, int jisu,
                                const int *pos, const int *lambda, const int *omega,
                                int *err_j, int *err_val)
{
	for (int i = 0; i < jisu; i++) {
		int mask = rs_forney(jisu, pos[i], lambda, omega);
		if (mask < 0)
			return -1;
		err_j[i] = gf_topos(n_data, pos[i]);
		err_val[i] = mask;
	}
	/* Recompute the syndromes S_k = sum_i err_val[i] * pos[i]^k (pos[i] is the
	 * errata locator value alpha^(n-1-p)) and require them to equal E. */
	for (int k = 0; k < npar; k++) {
		int s = 0;
		for (int i = 0; i < jisu; i++)
			if (err_val[i])
				s ^= gf_mulexp(err_val[i], (gf_log[pos[i]] * k) % GF_MAX);
		if (s != E[k])
			return -1;
	}
	return jisu;
}

/* Error-only decode: modified Berlekamp-Massey -> Chien -> Forney. */
static int rs_decode_errors(int npar, const int *E, int n_data, int *err_j, int *err_val)
{
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

/* Errors-and-erasures decode (item 8, C2 pointers as erasures). With e erasures and
 * t unknown errors, correction holds when e + 2t <= npar — so each erasure is worth
 * half an error, doubling reach when the drive tells us *where* the damage is. */
static int rs_decode_erasures(int npar, const int *E, int n_data,
                              const int *erasures, int n_erasures,
                              int *err_j, int *err_val)
{
	int e = n_erasures;
	if (e > npar)
		return -1; /* more erasures than parity: hopeless */

	/* Erasure locator Gamma(x) = prod (1 - X_i x), X_i = alpha^(n_data-1-p_i). */
	int gamma[RS_MAX_NPAR + 1] = {0};
	gamma[0] = 1;
	int glen = 1;
	for (int i = 0; i < e; i++) {
		int p = erasures[i];
		if (p < 0 || p >= n_data)
			return -1; /* erasure position out of range */
		int X = gf_exp[n_data - 1 - p];
		for (int j = glen; j > 0; j--)
			gamma[j] ^= gf_mul(X, gamma[j - 1]);
		glen++;
	}

	/* Modified syndromes T = Gamma * E mod x^npar. The clean tail T[e..npar-1] is a
	 * syndrome sequence for the t unknown errors alone; BM on it finds sigma. */
	int T[RS_MAX_NPAR] = {0};
	rs_mul_poly(T, npar, gamma, e + 1, E, npar);

	int sigma[RS_MAX_NPAR + 2] = {0};
	int t;
	int np2 = npar - e;
	if (np2 <= 0) {
		sigma[0] = 1; /* all parity consumed by erasures: no room for errors */
		t = 0;
	} else {
		t = rs_calc_sigma_mbm(np2, &T[e], sigma);
		if (t < 0 || 2 * t > np2)
			return -1; /* beyond e + 2t <= npar */
	}

	/* Combined errata locator Lambda = sigma * Gamma (degree jisu = e + t). */
	int lambda[RS_MAX_NPAR + 2] = {0};
	rs_mul_poly(lambda, e + t + 1, sigma, t + 1, gamma, e + 1);
	int jisu = e + t;
	if (jisu <= 0 || jisu > npar)
		return -1;

	/* Errata evaluator Omega = Lambda * E mod x^jisu (deg Omega < jisu). */
	int omega[RS_MAX_NPAR + 2] = {0};
	rs_mul_poly(omega, jisu, lambda, e + t + 1, E, npar);

	int pos[RS_MAX_NPAR + 1];
	if (!rs_chien_search(pos, n_data, jisu, lambda))
		return -1; /* fewer roots than errata: uncorrectable */

	return rs_emit_and_validate(npar, E, n_data, jisu, pos, lambda, omega, err_j,
	                            err_val);
}

int rs_decode_column(int npar, const int *E, int n_data,
                     const int *erasures, int n_erasures,
                     int *err_j, int *err_val)
{
	int has_err = 0;
	for (int i = 0; i < npar; i++)
		has_err |= E[i];
	if (!has_err)
		return 0; /* syndromes all zero: column clean (even any flagged positions) */

	if (n_erasures == 0)
		return rs_decode_errors(npar, E, n_data, err_j, err_val);
	return rs_decode_erasures(npar, E, n_data, erasures, n_erasures, err_j, err_val);
}
