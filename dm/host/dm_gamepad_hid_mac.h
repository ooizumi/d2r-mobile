#pragma once
#include <stdint.h>

/* Nonexclusive macOS fallback for the verified GameSir 3537:1104 profile. */
typedef void (*dm_hid_pad_publish)(const void *, uint64_t, int32_t, int32_t, int32_t, int32_t, int);
typedef void (*dm_hid_pad_remove)(const void *);
void dm_gamepad_hid_mac_start(dm_hid_pad_publish publish, dm_hid_pad_remove remove,
                              void (*ownership_changed)(void));
int dm_gamepad_hid_mac_claims_vendor(const char *name);
void dm_gamepad_hid_mac_focus(int focused);
