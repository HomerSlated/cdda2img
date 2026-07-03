# c2read — raw MMC READ CD (0xBE) audio + C2 error-pointer reader.
# Pure C99, no dependencies (Linux SG_IO). Mirrors tools/ctanalyse's build style.

CC      ?= cc
CFLAGS  ?= -O2 -std=c99 -Wall -Wextra -D_GNU_SOURCE

c2read: c2read.c
	$(CC) $(CFLAGS) -o $@ c2read.c

clean:
	rm -f c2read

.PHONY: clean
