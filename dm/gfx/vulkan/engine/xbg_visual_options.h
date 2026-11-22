#pragma once
#include <stdint.h>
#include <string.h>

enum { XBG_VISUAL_NO_BLOOM = 1, XBG_VISUAL_PIXEL_SCENE = 2, XBG_VISUAL_MASK = 3 };
typedef struct xbg_visual_state {
    uint32_t requested, applied;
    uint64_t bloom_draws_skipped, pixel_upscale_draws;
} xbg_visual_state;
#ifdef __cplusplus
extern "C" {
#endif
/* UI-safe request/readback; rendering consumes requests at a frame boundary. */
int xbg_game_set_visual_options(int flags);
void xbg_game_get_visual_options(xbg_visual_state *out);
#ifdef __cplusplus
}
#endif

/* D2R's exact shader identities from shader_map.tsv. No prefix matching:
 * other effects, tone mapping, transparency, composition and UI stay intact. */
static inline int xbg_visual_bloom_shader(const char *key) {
    static const char *const keys[] = {
        "2938a5b71b8e21b2", "019ce2f82edbda84", "ca25cbb66636f0c0",
        "2e2d6700a2569795", "cc264583f2000c81", "9e67673694fd5f8d",
        "755bd6554b0c93ef", "5aef0768a0c8d6b5", "b65f6060a609a3c5"
    };
    if (!key) return 0;
    for (unsigned i = 0; i < sizeof keys / sizeof keys[0]; ++i)
        if (!strcmp(key, keys[i])) return 1;
    return 0;
}
static inline int xbg_visual_pixel_binding(uint32_t flags, const char *key,
                                         unsigned stage, unsigned slot, unsigned sampler_slot) {
    return (flags & XBG_VISUAL_PIXEL_SCENE) && key && stage == 1 && slot == 10 && sampler_slot == 1 &&
        !strcmp(key, "52aac1f2306b8fc5");
}
typedef struct xbg_visual_latch { uint64_t frame; uint32_t flags; int seen; } xbg_visual_latch;
static inline int xbg_visual_latch_frame(xbg_visual_latch *state, uint64_t frame, uint32_t requested) {
    if (state->seen && state->frame == frame) return 0;
    uint32_t old = state->flags;
    state->seen = 1; state->frame = frame; state->flags = requested;
    return old != requested;
}
