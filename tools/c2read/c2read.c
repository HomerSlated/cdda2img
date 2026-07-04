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
#define SUB_RAW_BYTES 96         /* raw P-W subcode, interleaved */
#define SUB_Q_BYTES 16           /* formatted Q sub-channel block */
#define MAX_XFER 65535           /* keep one READ CD transfer under 64 KiB */
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

/* One READ CD command for nsec sectors from lba into buf (nsec * sector_len bytes).
 * submode: CDB byte 10 sub-channel selection — 0 none, 1 raw P-W (96 B), 2 formatted
 * Q (16 B). Returned per-sector field order is audio, C2, sub (probed on the PX-716A:
 * Q CRCs only validate at offset 2352+294; matches redumper SectorOrder::DATA_C2_SUB). */
static int read_cd(int fd, long lba, unsigned nsec, unsigned sector_type,
                   unsigned c2mode, unsigned submode, void *buf,
                   unsigned sector_len, unsigned char *sense) {
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
    cdb[10] = (unsigned char)(submode & 0x07);           /* sub-channel selection */
    cdb[11] = 0x00;
    return scsi_in(fd, cdb, sizeof(cdb), buf, nsec * sector_len, sense);
}

#define OP_GET_CONFIG 0x46
#define OP_START_STOP 0x1B
#define FEATURE_CD_READ 0x001E

/* SCSI START STOP UNIT with START=0, LOEJ=0: spin the spindle DOWN without ejecting.
 * More authoritative than the block-layer CDROMSTOP ioctl (which the sr driver can
 * issue with O_NONBLOCK quirks); this goes straight to the drive. Returns 0 on GOOD. */
static int drive_stop(int fd) {
    unsigned char cdb[6] = {OP_START_STOP, 0, 0, 0, 0x00, 0};
    unsigned char sense[SENSE_LEN];
    sg_io_hdr_t io;
    memset(&io, 0, sizeof(io));
    memset(sense, 0, SENSE_LEN);
    io.interface_id = 'S';
    io.dxfer_direction = SG_DXFER_NONE;
    io.cmd_len = sizeof(cdb);
    io.mx_sb_len = SENSE_LEN;
    io.cmdp = cdb;
    io.sbp = sense;
    io.timeout = 30000;
    if (ioctl(fd, SG_IO, &io) < 0) {
        fprintf(stderr, "c2read: START STOP UNIT ioctl: %s\n", strerror(errno));
        return -1;
    }
    if ((io.info & SG_INFO_OK_MASK) != SG_INFO_OK) {
        if (io.sb_len_wr > 0) print_sense(sense, "START STOP UNIT");
        return -1;
    }
    return 0;
}

/* GET CONFIGURATION, CD Read feature (0x1E) → the drive's *claimed* C2/DAP/CD-Text
 * support bits. Returns 0 if the descriptor was found (fills the out-params), -1
 * otherwise. This is only a claim — drives are known to advertise C2 they don't
 * honour, which is why probe_features() also does a functional smoke read. */
static int get_cdread_feature(int fd, int *dap, int *c2, int *cdtext, int *current) {
    unsigned char cdb[10] = {0};
    unsigned char buf[64] = {0};
    unsigned char sense[SENSE_LEN];

    cdb[0] = OP_GET_CONFIG;
    cdb[1] = 0x02;                    /* RT = 10b: return only the named feature */
    cdb[2] = 0x00;
    cdb[3] = (unsigned char)FEATURE_CD_READ;
    cdb[7] = 0x00;
    cdb[8] = (unsigned char)sizeof(buf);

    if (scsi_in(fd, cdb, sizeof(cdb), buf, sizeof(buf), sense) != 0)
        return -1;
    /* 8-byte feature header, then the first feature descriptor. */
    unsigned code = ((unsigned)buf[8] << 8) | buf[9];
    if (code != FEATURE_CD_READ)
        return -1;
    *current = buf[10] & 0x01;         /* active for the currently-loaded medium */
    unsigned flags = buf[12];          /* feature-specific byte 0 */
    *dap = (flags >> 7) & 1;
    *c2 = (flags >> 1) & 1;            /* C2 Flags supported */
    *cdtext = flags & 1;
    return 0;
}

/* Functional check: does READ CD with this C2/sub-channel field combination return
 * data (not a CHECK CONDITION)? Reads 3 CD-DA sectors from LBA 0. Returns 0 on ok. */
static int combo_smoke(int fd, unsigned c2mode, unsigned submode) {
    unsigned len = RAW_BYTES + (c2mode ? C2_BYTES : 0) +
                   (submode == 1 ? SUB_RAW_BYTES : submode == 2 ? SUB_Q_BYTES : 0);
    unsigned char sense[SENSE_LEN];
    unsigned char *buf = malloc((size_t)3 * len);
    if (!buf)
        return -1;
    int rc = read_cd(fd, 0, 3, 1, c2mode, submode, buf, len, sense);
    free(buf);
    return rc == 0 ? 0 : -1;
}

/* --features: report C2 capability (claim + functional smoke) in a machine-parseable
 * form and exit 0 IFF C2 is clearly usable, so the pipeline can gate on the exit code.
 * Conservative: anything short of "advertised AND functional" exits non-zero, so an
 * unreliable/unadvertised C2 falls back to the non-C2 recovery path by default.
 * Also smoke-tests every C2/sub-channel combination so the single-pass capture path
 * (audio + C2 + subcode in one READ CD) can be gated per drive. */
static int probe_features(int fd) {
    int dap = 0, c2 = 0, cdtext = 0, current = 0;
    int have_feat = get_cdread_feature(fd, &dap, &c2, &cdtext, &current);
    if (have_feat == 0)
        printf("cd_read_feature present current=%d dap=%d c2_flags=%d cd_text=%d\n",
               current, dap, c2, cdtext);
    else
        printf("cd_read_feature absent (GET CONFIGURATION 0x1E unavailable)\n");

    int smoke = combo_smoke(fd, 1, 0);
    printf("c2_read_smoke %s\n", smoke == 0 ? "ok" : "failed");

    static const struct { const char *name; unsigned c2mode, submode; } combos[] = {
        {"c2", 1, 0},         {"sub_raw", 0, 1},    {"sub_q", 0, 2},
        {"c2+sub_raw", 1, 1}, {"c2+sub_q", 1, 2},
    };
    for (unsigned i = 0; i < sizeof(combos) / sizeof(combos[0]); i++)
        printf("combo %s %s\n", combos[i].name,
               combo_smoke(fd, combos[i].c2mode, combos[i].submode) == 0
                   ? "ok" : "failed");

    const char *verdict;
    int usable;
    if (smoke != 0) {
        verdict = "C2_UNSUPPORTED";   /* can't even read with the C2 field */
        usable = 0;
    } else if (have_feat == 0 && c2 == 1) {
        verdict = "C2_SUPPORTED";     /* advertised AND functional */
        usable = 1;
    } else {
        verdict = "C2_UNVERIFIED";    /* reads accepted but not advertised — don't trust */
        usable = 0;
    }
    printf("verdict %s\n", verdict);
    return usable ? 0 : 1;
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
        "          [--any] [--c2beb] [--sub raw|q] [--chunk NSEC] [--speed X]\n"
        "          [--pcm FILE] [--c2 FILE] [--subf FILE] [--ranges] [-q]\n"
        "\n"
        "  --device DEV   optical device (default /dev/sr0)\n"
        "  --start LBA    first sector (default 0)\n"
        "  --count N      sectors to read; 0/omitted with --full = whole audio area\n"
        "  --full         read [start, lead-out) via READ TOC\n"
        "  --toc          dump track boundaries (READ TOC) to stdout and exit\n"
        "  --features     probe C2 capability (claim + smoke + combos); exit 0 iff usable\n"
        "  --stop         spin the spindle down (START STOP UNIT, no eject) and exit\n"
        "  --any          expected sector type ALL_TYPES (default CD_DA)\n"
        "  --c2beb        request C2+block-error-bits (296 B) instead of C2 (294 B)\n"
        "  --sub raw|q    also capture the sub-channel: raw P-W (96 B) or formatted Q (16 B)\n"
        "  --chunk NSEC   sectors per READ CD command (default 24, clamped to keep xfer <64K)\n"
        "  --speed X      set drive read speed to Nx first (best-effort)\n"
        "  --pcm FILE     write raw s16 PCM (2352 B/sector) here\n"
        "  --c2 FILE      write raw C2 bitmap (294 B/sector) here\n"
        "  --subf FILE    write the sub-channel stream here (needs --sub)\n"
        "  --ranges       print coalesced flagged-LBA ranges to stderr\n"
        "  -q             quiet: suppress the periodic stderr progress line\n"
        "\n"
        "  Read mode always emits machine-parseable 'progress <done> <total>' lines\n"
        "  on stdout (rate-limited); hard-unreadable sectors are zero-filled in the\n"
        "  PCM (C2 bitmap all-ones, sub zeroed) so the output files never desync.\n",
        me);
}

int main(int argc, char **argv) {
    const char *device = "/dev/sr0";
    const char *pcm_path = NULL, *c2_path = NULL, *sub_path = NULL;
    long start = 0, count = -1;
    int full = 0, any = 0, c2beb = 0, chunk = 24, speed = 0, ranges = 0, quiet = 0, toc = 0;
    int features = 0, stop = 0;
    unsigned submode = 0;                          /* 0 none, 1 raw P-W, 2 formatted Q */

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (!strcmp(a, "--device") && i + 1 < argc) device = argv[++i];
        else if (!strcmp(a, "--start") && i + 1 < argc) start = strtol(argv[++i], NULL, 0);
        else if (!strcmp(a, "--count") && i + 1 < argc) count = strtol(argv[++i], NULL, 0);
        else if (!strcmp(a, "--full")) full = 1;
        else if (!strcmp(a, "--any")) any = 1;
        else if (!strcmp(a, "--c2beb")) c2beb = 1;
        else if (!strcmp(a, "--sub") && i + 1 < argc) {
            const char *m = argv[++i];
            if (!strcmp(m, "raw")) submode = 1;
            else if (!strcmp(m, "q")) submode = 2;
            else { usage(argv[0]); return 2; }
        }
        else if (!strcmp(a, "--chunk") && i + 1 < argc) chunk = (int)strtol(argv[++i], NULL, 0);
        else if (!strcmp(a, "--speed") && i + 1 < argc) speed = (int)strtol(argv[++i], NULL, 0);
        else if (!strcmp(a, "--pcm") && i + 1 < argc) pcm_path = argv[++i];
        else if (!strcmp(a, "--c2") && i + 1 < argc) c2_path = argv[++i];
        else if (!strcmp(a, "--subf") && i + 1 < argc) sub_path = argv[++i];
        else if (!strcmp(a, "--ranges")) ranges = 1;
        else if (!strcmp(a, "--toc")) toc = 1;
        else if (!strcmp(a, "--features")) features = 1;
        else if (!strcmp(a, "--stop")) stop = 1;
        else if (!strcmp(a, "-q")) quiet = 1;
        else { usage(argv[0]); return 2; }
    }
    if (chunk < 1) chunk = 1;
    if (sub_path && !submode) {
        fprintf(stderr, "c2read: --subf requires --sub raw|q\n");
        return 2;
    }

    unsigned sector_type = any ? 0u : 1u;          /* ALL_TYPES vs CD_DA */
    unsigned c2mode = c2beb ? 2u : 1u;
    unsigned c2_len = c2beb ? C2_BEB_BYTES : C2_BYTES;
    unsigned sub_len = submode == 1 ? SUB_RAW_BYTES : submode == 2 ? SUB_Q_BYTES : 0;
    unsigned sector_len = RAW_BYTES + c2_len + sub_len;

    /* One transfer must stay under 64 KiB (sg one-shot buffer comfort zone); the
     * retry/zero-fill path also assumes chunk fits a 64-bit sector mask. */
    int maxchunk = (int)(MAX_XFER / sector_len);
    if (maxchunk > 63) maxchunk = 63;
    if (chunk > maxchunk) {
        fprintf(stderr, "c2read: --chunk %d clamped to %d (%u B/sector)\n",
                chunk, maxchunk, sector_len);
        chunk = maxchunk;
    }

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

    if (features) {
        int rc = probe_features(fd);
        close(fd);
        return rc;
    }

    if (stop) {
        int rc = drive_stop(fd);
        if (rc == 0 && !quiet) fprintf(stderr, "c2read: spindle stopped\n");
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

    FILE *pcm_fp = NULL, *c2_fp = NULL, *sub_fp = NULL;
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
    if (sub_path && !(sub_fp = fopen(sub_path, "wb"))) {
        fprintf(stderr, "c2read: open %s: %s\n", sub_path, strerror(errno));
        if (pcm_fp) fclose(pcm_fp);
        if (c2_fp) fclose(c2_fp);
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
    double last_prog = 0.0;                 /* rate limit for stdout progress lines */

    long lba = start, remaining = count;
    while (remaining > 0) {
        unsigned n = (unsigned)(remaining < chunk ? remaining : chunk);
        unsigned long long hard_mask = 0;   /* sectors zero-filled below (chunk <= 63) */
        int rc = read_cd(fd, lba, n, sector_type, c2mode, submode, buf, sector_len, sense);
        if (rc != 0) {
            /* Whole-chunk failure: the drive could not return these sectors at all
             * (distinct from a C2 flag, which returns data). Narrow to per-sector
             * reads (one retry each); a sector that still fails is ZERO-FILLED —
             * PCM zeros, C2 all-ones, sub zeros — so the output files never desync
             * and downstream treats the span as all-erasures. */
            if (rc == 1) print_sense(sense, "READ CD");
            else fprintf(stderr, "c2read: READ CD ioctl at LBA %ld: %s\n",
                         lba, strerror(errno));
            for (unsigned s = 0; s < n; s++) {
                unsigned char *sec = buf + (size_t)s * sector_len;
                long cur = lba + (long)s;
                int src = read_cd(fd, cur, 1, sector_type, c2mode, submode, sec,
                                  sector_len, sense);
                if (src != 0)
                    src = read_cd(fd, cur, 1, sector_type, c2mode, submode, sec,
                                  sector_len, sense);
                if (src != 0) {
                    memset(sec, 0, RAW_BYTES);
                    memset(sec + RAW_BYTES, 0xff, c2_len);
                    if (sub_len)
                        memset(sec + RAW_BYTES + c2_len, 0, sub_len);
                    hard_mask |= 1ull << s;
                    st.read_errors++;
                    fprintf(stderr, "c2read: hard %ld\n", cur);
                }
            }
        }

        for (unsigned s = 0; s < n; s++) {
            const unsigned char *sec = buf + (size_t)s * sector_len;
            const unsigned char *c2 = sec + RAW_BYTES;
            if (hard_mask & (1ull << s)) {
                /* Zero-filled sector: write it, but keep the synthetic all-ones C2
                 * out of the C2 stats so the verdict still reflects the drive. */
                if (pcm_fp) fwrite(sec, 1, RAW_BYTES, pcm_fp);
                if (c2_fp) fwrite(c2, 1, c2_len, c2_fp);
                if (sub_fp) fwrite(sec + RAW_BYTES + c2_len, 1, sub_len, sub_fp);
                continue;
            }
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
            if (sub_fp) fwrite(sec + RAW_BYTES + c2_len, 1, sub_len, sub_fp);
        }

        lba += n; remaining -= n;

        /* Machine-parseable progress on stdout for the pipeline TUI (<= ~4 Hz). */
        clock_gettime(CLOCK_MONOTONIC, &t1);
        double now = (double)t1.tv_sec + (double)t1.tv_nsec / 1e9;
        if (now - last_prog >= 0.25 || remaining == 0) {
            printf("progress %ld %ld\n", count - remaining, count);
            fflush(stdout);
            last_prog = now;
        }
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
    if (sub_fp) fclose(sub_fp);
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
