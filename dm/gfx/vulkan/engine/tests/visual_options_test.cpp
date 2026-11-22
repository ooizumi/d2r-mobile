#include "../xbg_visual_options.h"
#include <cassert>
#include <cstdio>
#include <initializer_list>

int main() {
    // A menu toggle during a frame cannot disable half a Bloom chain or use
    // mismatched filtering for the two copies of the same scene frame.
    xbg_visual_latch l{};
    assert(!xbg_visual_latch_frame(&l, 50, 0));
    assert(!xbg_visual_latch_frame(&l, 50, 3) && l.flags == 0);
    assert(xbg_visual_latch_frame(&l, 51, 3) && l.flags == 3);
    assert(!xbg_visual_latch_frame(&l, 51, 0) && l.flags == 3);
    assert(xbg_visual_latch_frame(&l, 52, 0) && l.flags == 0);
    assert(!xbg_visual_latch_frame(&l, 53, 0));

    const char *bloom[] = {"2938a5b71b8e21b2", "019ce2f82edbda84", "ca25cbb66636f0c0",
        "2e2d6700a2569795", "cc264583f2000c81", "9e67673694fd5f8d",
        "755bd6554b0c93ef", "5aef0768a0c8d6b5", "b65f6060a609a3c5"};
    for (auto key : bloom) assert(xbg_visual_bloom_shader(key));
    assert(!xbg_visual_bloom_shader(nullptr));
    assert(!xbg_visual_bloom_shader("2938a5b71b8e21b2-other"));
    // FXAA, final upscale and composition are required dependencies, not Bloom.
    for (auto key : {"ebcfcd1fa0a77dbc", "52aac1f2306b8fc5", "9a650561e5c40791", ""})
        assert(!xbg_visual_bloom_shader(key));

    assert(xbg_visual_pixel_binding(2, "52aac1f2306b8fc5", 1, 10, 1));
    assert(xbg_visual_pixel_binding(3, "52aac1f2306b8fc5", 1, 10, 1));
    assert(!xbg_visual_pixel_binding(0, "52aac1f2306b8fc5", 1, 10, 1));
    assert(!xbg_visual_pixel_binding(2, "52aac1f2306b8fc5", 1, 11, 2));
    assert(!xbg_visual_pixel_binding(2, "52aac1f2306b8fc5", 1, 10, 2));
    assert(!xbg_visual_pixel_binding(2, "52aac1f2306b8fc5", 0, 10, 1));
    assert(!xbg_visual_pixel_binding(2, "52aac1f2306b8fc5-other", 1, 10, 1));
    assert(!xbg_visual_pixel_binding(2, nullptr, 1, 10, 1));
    std::puts("PASS: frame-boundary latch, Bloom whitelist, pixel sampling isolation");
}
