/* ctanalyse — CTDB Reed-Solomon repair analysis for whole-disc CD-DA PCM.
 *
 * Analyse-only: reads PCM + a CTDB syndrome ("parity") file, reports the
 * corrections as JSON on stdout; never writes audio. The Python driver
 * (tools/ctdb_repair.py) owns lookup, splicing and verification.
 *
 * Exit 0 = analysis completed (including can_recover=false); 1 = operational
 * error; 2 = self-test failure.
 *
 * GPL. RS core ported from Masayuki Miyazaki's Reed-Solomon library via
 * CUETools.Parity/CUETools.CDRepair (see ALGORITHMS.md).
 */

#define _POSIX_C_SOURCE 200809L

#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include "cdrepair.h"
#include "crc32.h"
#include "galois16.h"
#include "rs16.h"

static void die(const char *msg)
{
	fprintf(stderr, "ctanalyse: %s\n", msg);
	exit(1);
}

/* Read a whole file into a malloc'd buffer; *size gets its length. Dies on error. */
static uint8_t *read_file(const char *path, size_t *size)
{
	FILE *f = fopen(path, "rb");
	if (!f)
		die("cannot open file");
	if (fseek(f, 0, SEEK_END) != 0)
		die("cannot seek file");
	long sz = ftell(f);
	if (sz < 0)
		die("cannot size file");
	rewind(f);
	uint8_t *buf = malloc((size_t)sz ? (size_t)sz : 1);
	if (fread(buf, 1, (size_t)sz, f) != (size_t)sz)
		die("short read");
	fclose(f);
	*size = (size_t)sz;
	return buf;
}

/* ---- self-test ----------------------------------------------------------- */

/* log-domain polynomial product, as Galois.gfconv (for the TestParity vectors) */
static void gfconv(const int *a, int alen, const int *b, int blen, int *c, int clen)
{
	int seki[16] = {0};
	for (int ia = 0; ia < alen; ia++) {
		if (a[ia] == -1)
			continue;
		int ib2 = blen < clen - ia ? blen : clen - ia;
		for (int ib = 0; ib < ib2; ib++)
			if (b[ib] != -1)
				seki[ia + ib] ^= gf_exp[a[ia] + b[ib]];
	}
	for (int i = 0; i < clen; i++)
		c[i] = seki[i] == 0 ? -1 : gf_log[seki[i]];
}

static uint32_t lcg;
static uint16_t lcg_next(void)
{
	lcg = lcg * 1103515245u + 12345u;
	return (uint16_t)(lcg >> 16);
}

/* slow reference data syndrome of one column (Horner) */
static void ref_syndrome(const uint16_t *d, int n, int npar, int *S)
{
	for (int i = 0; i < npar; i++) {
		int wk = 0;
		for (int j = 0; j < n; j++)
			wk = d[j] ^ gf_mulexp(wk, i);
		S[i] = wk;
	}
}

static int selftest(void)
{
	int fails = 0;

	/* TestParity gfconv vectors (log domain, -1 = zero coefficient) */
	static const int a[] = {1, 2, 3};
	static const int b1[] = {4, 3, 2}, want1[] = {5, 33657, 33184, 33657, 5};
	static const int b2[] = {4, -1, 2}, want2[] = {5, 6, 1774, 4, 5};
	int got[5];
	gfconv(a, 3, b1, 3, got, 5);
	for (int i = 0; i < 5; i++)
		if (got[i] != want1[i])
			fails++, printf("FAIL gfconv1[%d]: got %d want %d\n", i, got[i], want1[i]);
	gfconv(a, 3, b2, 3, got, 5);
	for (int i = 0; i < 5; i++)
		if (got[i] != want2[i])
			fails++, printf("FAIL gfconv2[%d]: got %d want %d\n", i, got[i], want2[i]);

	/* decode round-trips: reference column vs corrupted copy */
	enum { N = 2000, NPAR = 16 };
	static uint16_t ref[N], bad[N];
	int Sref[NPAR], Sbad[NPAR], E[NPAR];
	int err_j[NPAR / 2], err_val[NPAR / 2];

	for (int k = 0; k <= NPAR / 2 + 1; k++) {
		lcg = 0x00C0FFEE;
		for (int i = 0; i < N; i++)
			ref[i] = lcg_next();
		memcpy(bad, ref, sizeof(ref));
		for (int e = 0; e < k; e++) /* distinct spread positions, nonzero masks */
			bad[(e * 397 + 11) % N] ^= (uint16_t)(0x1D + e * 3);

		ref_syndrome(ref, N, NPAR, Sref);
		ref_syndrome(bad, N, NPAR, Sbad);
		for (int i = 0; i < NPAR; i++)
			E[i] = Sref[i] ^ Sbad[i];

		int n = rs_decode_column(NPAR, E, N, NULL, 0, err_j, err_val);
		if (k <= NPAR / 2) {
			if (n != k) {
				fails++, printf("FAIL roundtrip k=%d: decode returned %d\n", k, n);
				continue;
			}
			for (int i = 0; i < n; i++)
				bad[err_j[i]] ^= (uint16_t)err_val[i];
			if (memcmp(bad, ref, sizeof(ref)) != 0)
				fails++, printf("FAIL roundtrip k=%d: repaired != reference\n", k);
		} else if (n >= 0) {
			fails++, printf("FAIL overload k=%d: expected refusal, got %d\n", k, n);
		}
	}

	/* ---- erasure decode: e known erasures + t unknown errors, e + 2t <= NPAR ---- */
	{
		static const int ecase[][2] = {{4, 6}, {8, 4}, {16, 0}, {6, 5}, {1, 7}};
		int err_j2[NPAR], err_val2[NPAR], eras[NPAR];
		for (size_t ci = 0; ci < sizeof(ecase) / sizeof(ecase[0]); ci++) {
			int e = ecase[ci][0], t = ecase[ci][1], tot = e + t;
			lcg = 0x00C0FFEE;
			for (int i = 0; i < N; i++)
				ref[i] = lcg_next();
			memcpy(bad, ref, sizeof(ref));
			for (int q = 0; q < tot; q++) { /* first e corrupted positions are flagged */
				int p = (q * 397 + 11) % N;
				bad[p] ^= (uint16_t)(0x1D + q * 3);
				if (q < e)
					eras[q] = p;
			}
			ref_syndrome(ref, N, NPAR, Sref);
			ref_syndrome(bad, N, NPAR, Sbad);
			for (int i = 0; i < NPAR; i++)
				E[i] = Sref[i] ^ Sbad[i];
			int n = rs_decode_column(NPAR, E, N, eras, e, err_j2, err_val2);
			if (n != tot) {
				fails++, printf("FAIL eras e=%d t=%d: decode returned %d\n", e, t, n);
				continue;
			}
			for (int i = 0; i < n; i++)
				bad[err_j2[i]] ^= (uint16_t)err_val2[i];
			if (memcmp(bad, ref, sizeof(ref)) != 0)
				fails++, printf("FAIL eras e=%d t=%d: repaired != reference\n", e, t);
		}

		/* over-capacity: e=2, t=8 -> e + 2t = 18 > NPAR -> must refuse */
		{
			lcg = 0x00C0FFEE;
			for (int i = 0; i < N; i++)
				ref[i] = lcg_next();
			memcpy(bad, ref, sizeof(ref));
			for (int q = 0; q < 10; q++) {
				int p = (q * 397 + 11) % N;
				bad[p] ^= (uint16_t)(0x1D + q * 3);
				if (q < 2)
					eras[q] = p;
			}
			ref_syndrome(ref, N, NPAR, Sref);
			ref_syndrome(bad, N, NPAR, Sbad);
			for (int i = 0; i < NPAR; i++)
				E[i] = Sref[i] ^ Sbad[i];
			int n = rs_decode_column(NPAR, E, N, eras, 2, err_j2, err_val2);
			if (n >= 0)
				fails++, printf("FAIL eras overload: expected refusal, got %d\n", n);
		}

		/* false-positive erasure: a clean position flagged among the erasures
		 * (e=3 = 2 real + 1 false, t=5 -> 3 + 10 = 13). Recovers, false flag mask 0. */
		{
			lcg = 0x00C0FFEE;
			for (int i = 0; i < N; i++)
				ref[i] = lcg_next();
			memcpy(bad, ref, sizeof(ref));
			for (int q = 0; q < 7; q++) {
				int p = (q * 397 + 11) % N;
				bad[p] ^= (uint16_t)(0x1D + q * 3);
				if (q < 2)
					eras[q] = p;
			}
			eras[2] = (900 * 397 + 11) % N; /* clean, uncorrupted position */
			ref_syndrome(ref, N, NPAR, Sref);
			ref_syndrome(bad, N, NPAR, Sbad);
			for (int i = 0; i < NPAR; i++)
				E[i] = Sref[i] ^ Sbad[i];
			int n = rs_decode_column(NPAR, E, N, eras, 3, err_j2, err_val2);
			if (n < 0) {
				fails++, printf("FAIL eras false-positive: unexpected refusal\n");
			} else {
				for (int i = 0; i < n; i++)
					bad[err_j2[i]] ^= (uint16_t)err_val2[i];
				if (memcmp(bad, ref, sizeof(ref)) != 0)
					fails++, printf("FAIL eras false-positive: repaired != reference\n");
			}
		}
	}

	printf(fails ? "selftest: %d FAILURE(S)\n" : "selftest: all passed\n", fails);
	return fails ? 2 : 0;
}

/* ---- JSON output ---------------------------------------------------------- */

static void emit_json(const cta_ctx *c, const cta_result *r)
{
	printf("{\n");
	printf("  \"can_recover\": %s,\n", r->can_recover ? "true" : "false");
	if (r->offset_found)
		printf("  \"offset\": %d,\n", r->offset);
	else
		printf("  \"offset\": null,\n");
	printf("  \"npar\": %d,\n", c->npar);
	printf("  \"dirty_columns\": %d,\n", r->dirty_columns);
	printf("  \"corrected_errors\": %d,\n", r->corrected_errors);
	printf("  \"erasure_columns\": %d,\n", r->erasure_columns);

	printf("  \"corrections\": [");
	for (size_t i = 0; i < r->n_corr; i++)
		printf("%s\n    {\"byte\": %zu, \"old\": %u, \"new\": %u}",
		       i ? "," : "", r->corr[i].word * 2, r->corr[i].old, r->corr[i].new_);
	printf("%s],\n", r->n_corr ? "\n  " : "");

	printf("  \"affected_sectors\": [");
	size_t prev = (size_t)-1, nout = 0;
	for (size_t i = 0; i < r->n_corr; i++) {
		size_t sec = r->corr[i].word / 1176;
		if (sec != prev)
			printf("%s%zu", nout++ ? ", " : "", sec), prev = sec;
	}
	printf("],\n");

	if (r->offset_found) {
		printf("  \"crc_before\": \"%08x\",\n", r->crc_before);
		printf("  \"crc_after\": \"%08x\"\n", r->crc_after);
	} else {
		printf("  \"crc_before\": null,\n");
		printf("  \"crc_after\": null\n");
	}
	printf("}\n");
}

/* ---- main ----------------------------------------------------------------- */

static const char *usage =
    "usage: ctanalyse --pcm F --parity F --npar N --stride N(wire) [--toc a:b:...]\n"
    "                 [--erasures F] [--threads N] [--max-offset N] [--impl auto]\n"
    "                 | --selftest\n";

int main(int argc, char **argv)
{
	const char *pcm_path = NULL, *par_path = NULL, *eras_path = NULL;
	int npar = 0, stride_wire = 0, threads = 0, max_offset = 0;
	int do_selftest = 0;

	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--selftest"))
			do_selftest = 1;
		else if (!strcmp(argv[i], "--pcm") && i + 1 < argc)
			pcm_path = argv[++i];
		else if (!strcmp(argv[i], "--parity") && i + 1 < argc)
			par_path = argv[++i];
		else if (!strcmp(argv[i], "--npar") && i + 1 < argc)
			npar = atoi(argv[++i]);
		else if (!strcmp(argv[i], "--stride") && i + 1 < argc)
			stride_wire = atoi(argv[++i]);
		else if (!strcmp(argv[i], "--threads") && i + 1 < argc)
			threads = atoi(argv[++i]);
		else if (!strcmp(argv[i], "--max-offset") && i + 1 < argc)
			max_offset = atoi(argv[++i]);
		else if (!strcmp(argv[i], "--erasures") && i + 1 < argc)
			eras_path = argv[++i];
		else if ((!strcmp(argv[i], "--toc") || !strcmp(argv[i], "--impl")) && i + 1 < argc)
			++i; /* accepted for contract compatibility; unused in v1 */
		else {
			fputs(usage, stderr);
			return 1;
		}
	}

	/* v1 assumes a little-endian host: PCM words and the syndrome file are
	 * read as native u16. (x86-64 and aarch64 are both LE.) */
	const uint16_t probe = 1;
	if (*(const uint8_t *)&probe != 1)
		die("big-endian hosts are not supported");

	gf16_init();
	crc32_init();

	if (do_selftest)
		return selftest();

	if (!pcm_path || !par_path || npar < 2 || npar > RS_MAX_NPAR || (npar & 1) ||
	    stride_wire <= 0) {
		fputs(usage, stderr);
		return 1;
	}
	int stride = stride_wire * 2;

	/* PCM: mmap read-only */
	int fd = open(pcm_path, O_RDONLY);
	if (fd < 0)
		die("cannot open --pcm file");
	struct stat st;
	if (fstat(fd, &st) < 0 || st.st_size <= 0 || (st.st_size & 1))
		die("--pcm file has odd or zero size");
	size_t nwords = (size_t)st.st_size / 2;
	const uint16_t *pcm =
	    mmap(NULL, (size_t)st.st_size, PROT_READ, MAP_PRIVATE, fd, 0);
	if (pcm == MAP_FAILED)
		die("mmap of --pcm failed");
	close(fd);

	if (nwords / stride < 3)
		die("PCM too short for this stride");
	int stridecount = (int)(nwords / stride) - 2;
	int laststride = stride + (int)(nwords % stride);
	if (stridecount + npar > GF_MAX)
		die("stride too small for this disc (codeword exceeds GF(2^16))");

	/* syndrome file: [i][part2] u16le on disk -> transpose to [part2][i] */
	FILE *pf = fopen(par_path, "rb");
	if (!pf)
		die("cannot open --parity file");
	size_t want = (size_t)npar * stride;
	uint16_t *filesyn = malloc(want * 2);
	if (fread(filesyn, 2, want, pf) != want)
		die("--parity file shorter than npar*stride*2 bytes");
	fclose(pf);
	uint16_t *Sdb = malloc(want * 2);
	for (int i = 0; i < npar; i++)
		for (int p = 0; p < stride; p++)
			Sdb[(size_t)p * npar + i] = filesyn[(size_t)i * stride + p];
	free(filesyn);

	long ncpu = sysconf(_SC_NPROCESSORS_ONLN);
	cta_ctx c = {
	    .pcm = pcm,
	    .nwords = nwords,
	    .npar = npar,
	    .stride = stride,
	    .stridecount = stridecount,
	    .laststride = laststride,
	    .threads = threads > 0 ? threads : (ncpu > 0 ? (int)ncpu : 1),
	};

	uint16_t *S1 = malloc((size_t)stride * npar * 2);
	cta_syndromes(&c, S1);

	cta_result res = {0};
	cta_erasures er = {0};
	int have_er = 0;
	res.offset_found =
	    cta_find_offset(&c, S1, Sdb, max_offset > 0 ? max_offset : stride / 2 - 1,
	                    &res.offset);
	if (res.offset_found) {
		/* Erasures depend on the detected offset, so bucket them only now. */
		if (eras_path) {
			size_t esz;
			uint8_t *bits = read_file(eras_path, &esz);
			cta_build_erasures(&c, res.offset, bits, esz * 8, &er);
			free(bits);
			have_er = 1;
			fprintf(stderr, "ctanalyse: %ld C2 erasures bucketed\n", er.total);
		}
		cta_verify(&c, S1, Sdb, res.offset, have_er ? &er : NULL, &res);
		res.crc_before = cta_region_crc(&c, res.offset, NULL, 0);
		res.crc_after = res.n_corr
		                    ? cta_region_crc(&c, res.offset, res.corr, res.n_corr)
		                    : res.crc_before;
	} else {
		res.can_recover = false;
		fprintf(stderr, "ctanalyse: no syndrome-consistent offset found\n");
	}

	emit_json(&c, &res);
	if (have_er)
		cta_free_erasures(&er);
	free(res.corr);
	free(S1);
	free(Sdb);
	return 0;
}
