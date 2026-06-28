# Build helpers for the tools/ utilities.
#
# cdreset — minimal CDROMRESET helper used by paranoia_recovery_test.py. Needs
# CAP_SYS_ADMIN at runtime; grant it once after building:
#   make -C tools cdreset
#   doas setcap cap_sys_admin+ep tools/cdreset

CC ?= cc
CFLAGS ?= -Wall -Wextra -O2

cdreset: cdreset.c
	$(CC) $(CFLAGS) -o $@ $<

.PHONY: clean
clean:
	rm -f cdreset
