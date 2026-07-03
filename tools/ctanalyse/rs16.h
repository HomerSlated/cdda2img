/* ctanalyse — Reed-Solomon decode over GF(2^16).
 *
 * Port of RsDecode from CUETools.Parity (GPL) / Masayuki Miyazaki's RS library.
 * Modified Berlekamp-Massey -> Chien search -> Forney. See ALGORITHMS.md §6.
 */
#ifndef CTA_RS16_H
#define CTA_RS16_H

#include <stdbool.h>

#define RS_MAX_NPAR 32

/* Modified Berlekamp-Massey. syn[npar] in, sigma[npar/2+2] out.
 * Returns the degree of sigma (number of errors), or -1 on failure. */
int rs_calc_sigma_mbm(int npar, const int *syn, int *sigma);

/* omega = sigma * syn mod z^len (value-domain polynomial product prefix) */
void rs_mul_poly(int *seki, int lens, const int *a, int lena, const int *b, int lenb);

/* Find the jisu roots of sigma within sequence length n.
 * pos[jisu] receives the root *values* (alpha^m). Returns false if fewer
 * than jisu roots exist in range. */
bool rs_chien_search(int *pos, int n, int jisu, const int *sigma);

/* Error magnitude at root value ps. Returns -1 if the formal derivative is
 * zero (degenerate — treat the column as uncorrectable). */
int rs_forney(int jisu, int ps, const int *sigma, const int *omega);

/* Decode one column given its error syndrome E[npar] (== S_ours ^ S_db).
 * n_data = number of data symbols in the column (stridecount).
 * On success returns the number of errata found and fills err_j[]/err_val[] with
 * data indices (0-based, oldest-first) and XOR masks. Returns -1 if uncorrectable,
 * 0 if the column is clean (all syndromes zero).
 *
 * erasures/n_erasures: C2 error pointers as erasure positions (item 8). With e
 * erasures and t unknown errors, correction holds when e + 2t <= npar (each erasure
 * is worth half an error). n_erasures == 0 is the pure error-only path.
 *
 * Buffer contract: err_j[]/err_val[] must hold up to npar/2 entries when
 * n_erasures == 0, and up to npar entries when n_erasures > 0 (an all-erasure
 * column returns npar errata). */
int rs_decode_column(int npar, const int *E, int n_data,
                     const int *erasures, int n_erasures,
                     int *err_j, int *err_val);

#endif
