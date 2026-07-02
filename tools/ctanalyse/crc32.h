/* ctanalyse — zlib-compatible CRC-32 (reflected, poly 0xEDB88320). */
#ifndef CTA_CRC32_H
#define CTA_CRC32_H

#include <stddef.h>
#include <stdint.h>

void crc32_init(void);
uint32_t crc32_update(uint32_t crc, const uint8_t *buf, size_t len);
/* full-buffer convenience: crc32(buf) == zlib.crc32(buf) */
uint32_t crc32_buf(const uint8_t *buf, size_t len);

#endif
