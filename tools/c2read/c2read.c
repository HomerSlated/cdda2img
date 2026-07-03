/*
 * c2read.c — raw MMC READ CD (0xBE) audio reader that surfaces the drive's
 * per-byte C2 error pointers to the host.
 *
 * This is the metal half of the C2 experiment (docs, item 8): a standalone,
 * dependency-free reader whose only job is to issue READ CD with the C2 error
 * field set and dump, per sector, the 2352 bytes of audio PCM plus the 294-byte
 * C2 flag bitmap the drive reports alongside it. All policy — multi-read
 * consensus, oracle diffing, the confusion matrix, timing — lives in the Python
 * driver (c2bench.py). This tool decides nothing; it reads and reports.
 *
 * The C2 bitmap: 294 bytes = 2352 bits, one bit per audio byte, MSB-first
 * within each byte (bit 7 of C2 byte 0 == audio byte 0). A set bit means the
 * drive's CIRC decoder could not correct that byte (it would be interpolated on
 * playback). A *fired* flag is trustworthy; a *clear* flag is not (RS
 * miscorrection can silently pass a wrong byte) — quantifying that asymmetry is
 * the whole experiment, so this tool never trusts either; it only records.
 *
 * CDB layout pinned from redumper scsi/mmc.ixx (CDB12_ReadCD + the
 * READ_CD_ExpectedSectorType / READ_CD_ErrorField enums), built with explicit
 * shifts rather than C bitfields so the byte layout is compiler-independent.
 *
 * Access: SG_IO on /dev/srN with a read-only fd; READ CD is a read-class opcode
 * the kernel's sg command filter permits without CAP_SYS_RAWIO (cd-paranoia
 * relies on the same), so no root is needed for a user in the cdrom group.
 *
 * Build:  make -C tools/c2read
 * Usage:  c2read [--device DEV] [--start LBA] [--count N | --full]
 *                [--any] [--c2beb] [--chunk NSEC] [--speed X]
 *                [--pcm FILE] [--c2 FILE] [--ranges] [-q]
 */

#include <errno.h>
#include <fcntl.h>
#include <linux/cdrom.h>
#include <scsi/sg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>

#define OP_READ_CD 0xBE
#define OP_READ_TOC 0x43

#define RAW_BYTES 2352            /* CD-DA user data per sector */
#define C2_BYTES 294             /* 2352 bits, one per audio byte */
#define C2_BEB_BYTES 296         /* C2 + block-error bits variant */
#define LEADOUT_TRACK 0xAA
#define SENSE_LEN 32
#define SG_TIMEOUT_MS 60000      /* generous — a defect can trigger long internal retries */

/* CDROM_SELECT_SPEED: unprivileged Nx read-speed set (same path drive_speed.py uses). */
#ifndef CDROM_SELECT_SPEED
#define CDROM_SELECT_SPEED 0x5322
#endif

typedef struct {
    unsigned long long sectors_read;
    unsigned long long sectors_flagged;   /* >=1 C2 bit set */
    unsigned long long c2_bits;           /* total set C2 bits across the run */
    unsigned max_bits_in_sector;
    long first_flagged_lba;
    long last_flagged_lba;
    unsigned long long read_errors;       /* sectors we could not read at all (sense) */
} stats_t;

/* ---- SCSI plumbing --------------------------------------------------------- */

static void print_sense(const unsigned char *sense, const char *what) {
    unsigned code = sense[0] & 0x7f;
    if (code == 0x70 || code == 0x71) {
        unsigned key = sense[2] & 0x0f;
        unsigned asc = sense[12];
        unsigned ascq = sense[13];
        fprintf(stderr, "c2read: %s: sense key=0x%x asc=0x%02x ascq=0x%02x\n",
                what, key, asc, ascq);
    } else {
        fprintf(stderr, "c2read: %s: sense response 0x%02x (descriptor/unknown)\n",
                what, sense[0]);
    }
}

/* Returns 0 on GOOD, -1 on ioctl/host/driver failure, 1 on CHECK CONDITION. */
static int scsi_in(int fd, const unsigned char *cdb, int cdb_len,
                   void *buf, unsigned buf_len, unsigned char *sense) {
    sg_io_hdr_t io;
    memset(&io, 0, sizeof(io));
    memset(sense, 0, SENSE_LEN);
    io.interface_id = 'S';
    io.dxfer_direction = SG_DXFER_FROM_DEV;
    io.cmd_len = (unsigned char)cdb_len;
    io.mx_sb_len = SENSE_LEN;
    io.dxfer_len = buf_len;
    io.dxferp = buf;
    io.cmdp = (unsigned char *)cdb;
    io.sbp = sense;
    io.timeout = SG_TIMEOUT_MS;

    if (ioctl(fd, SG_IO, &io) < 0)
        return -1;

    if ((io.info & SG_INFO_OK_MASK) != SG_INFO_OK) {
        /* sb_len_wr > 0 means the drive returned sense — a CHECK CONDITION we can
         * decode; anything else (host/driver/transport) is a hard failure. */
        if (io.sb_len_wr > 0)
            return 1;
        return -1;
    }
    return 0;
}

/* READ TOC (format 0, LBA) → lead-out LBA, or -1 on failure. */
static long read_leadout(int fd) {
    unsigned char cdb[10] = {0};
    unsigned char buf[1024] = {0};
    unsigned char sense[SENSE_LEN];
    unsigned alloc = sizeof(buf);

    cdb[0] = OP_READ_TOC;
    cdb[1] = 0x00;                 /* MSF=0 → LBA addresses */
    cdb[2] = 0x00;                 /* format 0 = TOC */
    cdb[6] = 0x01;                 /* starting track */
    cdb[7] = (unsigned char)(alloc >> 8);
    cdb[8] = (unsigned char)(alloc & 0xff);

    int rc = scsi_in(fd, cdb, sizeof(cdb), buf, alloc, sense);
    if (rc != 0) {
        if (rc == 1) print_sense(sense, "READ TOC");
        else fprintf(stderr, "c2read: READ TOC ioctl failed: %s\n", strerror(errno));
        return -1;
    }

    unsigned data_len = ((unsigned)buf[0] << 8) | buf[1];  /* bytes following this field */
    unsigned end = data_len + 2;
    if (end > sizeof(buf)) end = sizeof(buf);
    for (unsigned off = 4; off + 8 <= end; off += 8) {
        if (buf[off + 2] == LEADOUT_TRACK) {
            return ((long)buf[off + 4] << 24) | ((long)buf[off + 5] << 16) |
                   ((long)buf[off + 6] << 8) | (long)buf[off + 7];
        }
    }
    fprintf(stderr, "c2read: READ TOC returned no lead-out (0xAA) descriptor\n");
    return -1;
}

/* Dump every TOC track descriptor as "track N lba L" (plus "leadout lba L") to
 * stdout — machine-parseable, and issued via the same READ TOC command so it
 * never throttles the drive the way cd-paranoia -Q does. Returns 0 on success. */
static int dump_toc(int fd) {
    unsigned char cdb[10] = {0};
    unsigned char buf[1024] = {0};
    unsigned char sense[SENSE_LEN];
    unsigned alloc = sizeof(buf);

    cdb[0] = OP_READ_TOC;
    cdb[1] = 0x00;                 /* LBA addresses */
    cdb[2] = 0x00;                 /* format 0 = TOC */
    cdb[6] = 0x01;                 /* starting track */
    cdb[7] = (unsigned char)(alloc >> 8);
    cdb[8] = (unsigned char)(alloc & 0xff);

    int rc = scsi_in(fd, cdb, sizeof(cdb), buf, alloc, sense);
    if (rc != 0) {
        if (rc == 1) print_sense(sense, "READ TOC");
        else fprintf(stderr, "c2read: READ TOC ioctl failed: %s\n", strerror(errno));
        return -1;
    }

    unsigned data_len = ((unsigned)buf[0] << 8) | buf[1];
    unsigned end = data_len + 2;
    if (end > sizeof(buf)) end = sizeof(buf);
    for (unsigned off = 4; off + 8 <= end; off += 8) {
        unsigned adr_ctrl = buf[off + 1];
        unsigned track = buf[off + 2];
        long lba = ((long)buf[off + 4] << 24) | ((long)buf[off + 5] << 16) |
                   ((long)buf[off + 6] << 8) | (long)buf[off + 7];
        if (track == LEADOUT_TRACK)
            printf("leadout lba %ld\n", lba);
        else
            printf("track %u lba %ld ctrl 0x%x\n", track, lba, adr_ctrl & 0x0f);
    }
    return 0;
}

/* One READ CD command for nsec sectors from lba into buf (nsec * sector_len bytes). */
static int read_cd(int fd, long lba, unsigned nsec, unsigned sector_type,
                   unsigned c2mode, void *buf, unsigned sector_len,
                   unsigned char *sense) {
    unsigned char cdb[12] = {0};
    cdb[0] = OP_READ_CD;
    cdb[1] = (unsigned char)(sector_type << 2);          /* expected_sector_type, bits 4-2 */
    cdb[2] = (unsigned char)((lba >> 24) & 0xff);
    cdb[3] = (unsigned char)((lba >> 16) & 0xff);
    cdb[4] = (unsigned char)((lba >> 8) & 0xff);
    cdb[5] = (unsigned char)(lba & 0xff);
    cdb[6] = (unsigned char)((nsec >> 16) & 0xff);
    cdb[7] = (unsigned char)((nsec >> 8) & 0xff);
    cdb[8] = (unsigned char)(nsec & 0xff);
    cdb[9] = (unsigned char)(0x10 | (c2mode << 1));      /* include_user_data | error_flags */
    cdb[10] = 0x00;                                      /* no sub-channel */
    cdb[11] = 0x00;
    return scsi_in(fd, cdb, sizeof(cdb), buf, nsec * sector_len, sense);
}

/* ---- driver ---------------------------------------------------------------- */

static void set_speed(int fd, int nx) {
    if (nx <= 0) return;
    if (ioctl(fd, CDROM_SELECT_SPEED, nx) < 0)
        fprintf(stderr, "c2read: CDROM_SELECT_SPEED(%d) failed: %s (continuing)\n",
                nx, strerror(errno));
}

static void usage(const char *me) {
    fprintf(stderr,
        "usage: %s [--device DEV] [--start LBA] [--count N | --full]\n"
        "          [--any] [--c2beb] [--chunk NSEC] [--speed X]\n"
        "          [--pcm FILE] [--c2 FILE] [--ranges] [-q]\n"
        "\n"
        "  --device DEV   optical device (default /dev/sr0)\n"
        "  --start LBA    first sector (default 0)\n"
        "  --count N      sectors to read; 0/omitted with --full = whole audio area\n"
        "  --full         read [start, lead-out) via READ TOC\n"
        "  --toc          dump track boundaries (READ TOC) to stdout and exit\n"
        "  --any          expected sector type ALL_TYPES (default CD_DA)\n"
        "  --c2beb        request C2+block-error-bits (296 B) instead of C2 (294 B)\n"
        "  --chunk NSEC   sectors per READ CD command (default 24, keeps xfer <64K)\n"
        "  --speed X      set drive read speed to Nx first (best-effort)\n"
        "  --pcm FILE     write raw s16 PCM (2352 B/sector) here\n"
        "  --c2 FILE      write raw C2 bitmap (294 B/sector) here\n"
        "  --ranges       print coalesced flagged-LBA ranges to stderr\n"
        "  -q             quiet: suppress the periodic progress line\n",
        me);
}

int main(int argc, char **argv) {
    const char *device = "/dev/sr0";
    const char *pcm_path = NULL, *c2_path = NULL;
    long start = 0, count = -1;
    int full = 0, any = 0, c2beb = 0, chunk = 24, speed = 0, ranges = 0, quiet = 0, toc = 0;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (!strcmp(a, "--device") && i + 1 < argc) device = argv[++i];
        else if (!strcmp(a, "--start") && i + 1 < argc) start = strtol(argv[++i], NULL, 0);
        else if (!strcmp(a, "--count") && i + 1 < argc) count = strtol(argv[++i], NULL, 0);
        else if (!strcmp(a, "--full")) full = 1;
        else if (!strcmp(a, "--any")) any = 1;
        else if (!strcmp(a, "--c2beb")) c2beb = 1;
        else if (!strcmp(a, "--chunk") && i + 1 < argc) chunk = (int)strtol(argv[++i], NULL, 0);
        else if (!strcmp(a, "--speed") && i + 1 < argc) speed = (int)strtol(argv[++i], NULL, 0);
        else if (!strcmp(a, "--pcm") && i + 1 < argc) pcm_path = argv[++i];
        else if (!strcmp(a, "--c2") && i + 1 < argc) c2_path = argv[++i];
        else if (!strcmp(a, "--ranges")) ranges = 1;
        else if (!strcmp(a, "--toc")) toc = 1;
        else if (!strcmp(a, "-q")) quiet = 1;
        else { usage(argv[0]); return 2; }
    }
    if (chunk < 1) chunk = 1;

    unsigned sector_type = any ? 0u : 1u;          /* ALL_TYPES vs CD_DA */
    unsigned c2mode = c2beb ? 2u : 1u;
    unsigned c2_len = c2beb ? C2_BEB_BYTES : C2_BYTES;
    unsigned sector_len = RAW_BYTES + c2_len;

    int fd = open(device, O_RDONLY | O_NONBLOCK);
    if (fd < 0) {
        fprintf(stderr, "c2read: open %s: %s\n", device, strerror(errno));
        return 1;
    }

    if (toc) {
        int rc = dump_toc(fd);
        close(fd);
        return rc == 0 ? 0 : 1;
    }

    set_speed(fd, speed);

    if (full || count < 0) {
        long leadout = read_leadout(fd);
        if (leadout < 0) { close(fd); return 1; }
        if (start >= leadout) {
            fprintf(stderr, "c2read: start %ld >= lead-out %ld — nothing to read\n",
                    start, leadout);
            close(fd);
            return 1;
        }
        count = leadout - start;
        if (!quiet)
            fprintf(stderr, "c2read: lead-out at LBA %ld; reading %ld sectors from %ld\n",
                    leadout, count, start);
    }

    FILE *pcm_fp = NULL, *c2_fp = NULL;
    if (pcm_path && !(pcm_fp = fopen(pcm_path, "wb"))) {
        fprintf(stderr, "c2read: open %s: %s\n", pcm_path, strerror(errno));
        close(fd); return 1;
    }
    if (c2_path && !(c2_fp = fopen(c2_path, "wb"))) {
        fprintf(stderr, "c2read: open %s: %s\n", c2_path, strerror(errno));
        if (pcm_fp) fclose(pcm_fp);
        close(fd);
        return 1;
    }

    unsigned char *buf = malloc((size_t)chunk * sector_len);
    unsigned char *sense = malloc(SENSE_LEN);
    if (!buf || !sense) { fprintf(stderr, "c2read: out of memory\n"); return 1; }

    stats_t st = {0, 0, 0, 0, -1, -1, 0};
    long range_start = -1, range_end = -1;  /* open coalesced flagged range */
    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);

    long lba = start, remaining = count;
    while (remaining > 0) {
        unsigned n = (unsigned)(remaining < chunk ? remaining : chunk);
        int rc = read_cd(fd, lba, n, sector_type, c2mode, buf, sector_len, sense);
        if (rc != 0) {
            /* A whole-chunk failure: the drive could not return these sectors at
             * all (distinct from a C2 flag, which returns data). Record and skip
             * past this chunk so the scan completes rather than stalling. */
            if (rc == 1) print_sense(sense, "READ CD");
            else fprintf(stderr, "c2read: READ CD ioctl at LBA %ld: %s\n",
                         lba, strerror(errno));
            st.read_errors += n;
            lba += n; remaining -= n;
            continue;
        }

        for (unsigned s = 0; s < n; s++) {
            const unsigned char *sec = buf + (size_t)s * sector_len;
            const unsigned char *c2 = sec + RAW_BYTES;
            unsigned bits = 0;
            for (unsigned b = 0; b < c2_len; b++)
                bits += (unsigned)__builtin_popcount(c2[b]);

            st.sectors_read++;
            st.c2_bits += bits;
            if (bits) {
                long cur = lba + (long)s;
                st.sectors_flagged++;
                if (bits > st.max_bits_in_sector) st.max_bits_in_sector = bits;
                if (st.first_flagged_lba < 0) st.first_flagged_lba = cur;
                st.last_flagged_lba = cur;
                if (range_start < 0) { range_start = range_end = cur; }
                else if (cur == range_end + 1) { range_end = cur; }
                else {
                    if (ranges)
                        fprintf(stderr, "  flagged: LBA %ld..%ld (%ld sectors)\n",
                                range_start, range_end, range_end - range_start + 1);
                    range_start = range_end = cur;
                }
            }
            if (pcm_fp) fwrite(sec, 1, RAW_BYTES, pcm_fp);
            if (c2_fp) fwrite(c2, 1, c2_len, c2_fp);
        }

        lba += n; remaining -= n;
        if (!quiet && (st.sectors_read % 4096 < (unsigned)n))
            fprintf(stderr, "\r  read %llu / %ld sectors  (flagged %llu, C2 bits %llu) ",
                    st.sectors_read, count, st.sectors_flagged, st.c2_bits);
    }
    if (ranges && range_start >= 0)
        fprintf(stderr, "  flagged: LBA %ld..%ld (%ld sectors)\n",
                range_start, range_end, range_end - range_start + 1);
    if (!quiet) fprintf(stderr, "\n");

    clock_gettime(CLOCK_MONOTONIC, &t1);
    double secs = (double)(t1.tv_sec - t0.tv_sec) + (double)(t1.tv_nsec - t0.tv_nsec) / 1e9;

    if (pcm_fp) fclose(pcm_fp);
    if (c2_fp) fclose(c2_fp);
    free(buf); free(sense); close(fd);

    /* ---- summary + verdict ------------------------------------------------- */
    fprintf(stderr, "\nc2read summary (%s)\n", device);
    fprintf(stderr, "  sectors read     : %llu (%.1f s, %.1f sectors/s)\n",
            st.sectors_read, secs, st.sectors_read / (secs > 0 ? secs : 1));
    fprintf(stderr, "  hard read errors : %llu sectors (no data returned)\n", st.read_errors);
    fprintf(stderr, "  C2-flagged       : %llu sectors, %llu bits total, max %u bits/sector\n",
            st.sectors_flagged, st.c2_bits, st.max_bits_in_sector);
    if (st.sectors_flagged)
        fprintf(stderr, "  flagged span     : LBA %ld .. %ld\n",
                st.first_flagged_lba, st.last_flagged_lba);

    if (st.sectors_flagged > 0) {
        fprintf(stderr,
            "\n  VERDICT: CANDIDATE — the drive reports C2 pointers on this disc.\n"
            "           Re-run with --pcm/--c2 to capture data for the confusion matrix.\n");
        return 0;
    }
    if (st.read_errors > 0) {
        fprintf(stderr,
            "\n  VERDICT: HARD-UNREADABLE regions but NO C2 flags — the drive fails the\n"
            "           read outright rather than flagging bytes. Different failure mode;\n"
            "           not the C2-hint case. Try another disc.\n");
        return 3;
    }
    fprintf(stderr,
        "\n  VERDICT: NO C2 REPORTED — pristine disc, or this drive does not surface\n"
        "           C2 for this read. Try another disc.\n");
    return 3;
}
