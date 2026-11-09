#ifndef BRIDGE_MEDIA_VALIDATION_H
#define BRIDGE_MEDIA_VALIDATION_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

enum { BRIDGE_MEDIA_NONE, BRIDGE_MEDIA_STICKER, BRIDGE_MEDIA_IMAGE, BRIDGE_MEDIA_FILE };

static int bridge_media_kind(uint32_t action) {
    if (action==4||action==5) return BRIDGE_MEDIA_STICKER;
    if (action==6||action==7) return BRIDGE_MEDIA_IMAGE;
    if (action==8||action==9) return BRIDGE_MEDIA_FILE;
    return BRIDGE_MEDIA_NONE;
}

static uint64_t bridge_message_type(int kind) {
    if (kind==BRIDGE_MEDIA_STICKER) return 47;
    if (kind==BRIDGE_MEDIA_IMAGE) return 3;
    if (kind==BRIDGE_MEDIA_FILE) return UINT64_C(0x600000031);
    return 1;
}

/* Byte limits are plugin policy. The host fully decodes images and checks pixels. */
static int bridge_media_valid(int kind, uint64_t size, const unsigned char *header, size_t n) {
    if (!size) return 0;
    if (kind==BRIDGE_MEDIA_FILE) return size<=100*UINT64_C(1024)*1024;
    if (!header) return 0;
    if (kind==BRIDGE_MEDIA_STICKER) return size<=10*UINT64_C(1024)*1024&&n>=6&&
        (!memcmp(header,"GIF87a",6)||!memcmp(header,"GIF89a",6));
    if (kind==BRIDGE_MEDIA_IMAGE) return size<=20*UINT64_C(1024)*1024&&
        ((n>=8&&!memcmp(header,"\x89PNG\r\n\x1a\n",8))||
         (n>=3&&header[0]==0xff&&header[1]==0xd8&&header[2]==0xff));
    return 0;
}

#endif
