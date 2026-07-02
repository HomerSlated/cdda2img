/* ctanalyse — zlib-compatible CRC-32. */

#include "crc32.h"

static uint32_t tbl[256];

void crc32_init(void)
{
	for (uint32_t i = 0; i < 256; i++) {
		uint32_t c = i;
		for (int k = 0; k < 8; k++)
			c = (c & 1) ? 0xEDB88320u ^ (c >> 1) : c >> 1;
		tbl[i] = c;
	}
}

uint32_t crc32_update(uint32_t crc, const uint8_t *buf, size_t len)
{
	crc ^= 0xFFFFFFFFu;
	for (size_t i = 0; i < len; i++)
		crc = tbl[(crc ^ buf[i]) & 0xFF] ^ (crc >> 8);
	return crc ^ 0xFFFFFFFFu;
}

uint32_t crc32_buf(const uint8_t *buf, size_t len)
{
	return crc32_update(0, buf, len);
}
