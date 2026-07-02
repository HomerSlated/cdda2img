# ctanalyse — pure C99, x86-64-v1 baseline (nothing beyond what -O2 emits).
CC      ?= cc
CFLAGS  ?= -O2
CFLAGS  += -std=c99 -Wall -Wextra -pthread
LDFLAGS += -pthread

OBJS = main.o galois16.o rs16.o cdrepair.o crc32.o

ctanalyse: $(OBJS)
	$(CC) $(LDFLAGS) -o $@ $(OBJS)

%.o: %.c galois16.h rs16.h cdrepair.h crc32.h
	$(CC) $(CFLAGS) -c -o $@ $<

.PHONY: test clean
test: ctanalyse
	./ctanalyse --selftest

clean:
	rm -f ctanalyse $(OBJS)
