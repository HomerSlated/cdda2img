// cdreset.c — minimal CDROMRESET helper for paranoia_recovery_test.py.
//
// CDROMRESET (hard-reset the drive) requires CAP_SYS_ADMIN, which the kernel checks
// unconditionally. Rather than run the whole test tool as root, this single-purpose
// helper carries the capability so the rest of the tool stays unprivileged.
//
// Build + grant once:
//   make -C tools cdreset
//   doas setcap cap_sys_admin+ep tools/cdreset
//
// Usage: cdreset [/dev/srX]   (default /dev/sr0)

#include <errno.h>
#include <fcntl.h>
#include <linux/cdrom.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

int main(int argc, char **argv) {
    const char *dev = argc > 1 ? argv[1] : "/dev/sr0";

    int fd = open(dev, O_RDONLY | O_NONBLOCK);
    if (fd < 0) {
        fprintf(stderr, "cdreset: open %s: %s\n", dev, strerror(errno));
        return 1;
    }

    int rc = ioctl(fd, CDROMRESET);
    close(fd);
    if (rc < 0) {
        fprintf(stderr, "cdreset: CDROMRESET %s: %s\n", dev, strerror(errno));
        return 1;
    }
    return 0;
}
