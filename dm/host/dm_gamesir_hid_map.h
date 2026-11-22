// GameSir X4A raw HID report -> D2R Mobile controller state (pure C).
// The descriptor seen on the target device is:
//   Generic Desktop: X/Y/Z/Rz usages 48/49/50/53, logical 0..255
//   Button page: usages 1..16, logical 0..1
//   Generic Desktop hat switch usage 57, logical 0..7 (observed null: 15)
//   Simulation Controls: Gas usage 196 and Brake usage 197, logical 0..255
// Y axes are inverted because HID reports increase downward while nn::hid's
// sticks increase upward. Unknown pages/usages and invalid ranges are ignored.
#pragma once

#include <stdint.h>

#include "dm_input.h"
#include "dm_padmap.h"

typedef struct {
    uint64_t buttons;
    int32_t axes[4];   // left X, left Y, right X, right Y
} dm_gamesir_hid_state;

static inline void dm_gamesir_hid_clear(dm_gamesir_hid_state *state) {
    unsigned i;

    if (!state) return;
    state->buttons = 0;
    for (i = 0; i < sizeof state->axes / sizeof state->axes[0]; ++i)
        state->axes[i] = 0;
}

static inline int dm_gamesir_hid_button_bit(uint32_t usage) {
    switch (usage) {
        case 1:  return dm_padmap_face_xbox(1, 0, 0, 0, 0);
        case 2:  return dm_padmap_face_xbox(0, 1, 0, 0, 0);
        case 4:  return dm_padmap_face_xbox(0, 0, 1, 0, 0);
        case 5:  return dm_padmap_face_xbox(0, 0, 0, 1, 0);
        case 7:  return DM_BTN_L;
        case 8:  return DM_BTN_R;
        case 11: return DM_BTN_MINUS;
        case 12: return DM_BTN_PLUS;
        case 14: return DM_BTN_LS;
        case 15: return DM_BTN_RS;
        default: return 0;
    }
}

static inline int dm_gamesir_hid_axis_index(uint32_t usage) {
    switch (usage) {
        case 48: return 0;
        case 49: return 1;
        case 50: return 2;
        case 53: return 3;
        default: return -1;
    }
}

static inline int dm_gamesir_hid_pressed(int64_t value, int64_t min, int64_t max) {
    long double lo = (long double)min;
    long double hi = (long double)max;
    long double v = (long double)value;

    if (v < lo) v = lo;
    if (v > hi) v = hi;
    return (v - lo) * 2.0L >= hi - lo;
}

static inline int32_t dm_gamesir_hid_axis_value(int64_t value, int64_t min,
                                                int64_t max, int invert) {
    long double lo = (long double)min;
    long double hi = (long double)max;
    long double span = hi - lo;
    long double v = (long double)value;
    long double mid = lo + span / 2.0L;
    long double scaled;
    int32_t out;

    if (v < lo) v = lo;
    if (v > hi) v = hi;

    // An unsigned 8-bit stick centred at 128 reports 127/128 around its exact
    // 127.5 midpoint. Treat that half-step as centre instead of inventing drift.
    if ((v < mid && mid - v <= 0.5L) || (v >= mid && v - mid <= 0.5L))
        return 0;

    scaled = (v - lo) / span * (2.0L * (long double)DM_STICK_MAX)
             - (long double)DM_STICK_MAX;
    out = scaled >= 0.0L ? (int32_t)(scaled + 0.5L)
                         : (int32_t)(scaled - 0.5L);
    return invert ? -out : out;
}

static inline int dm_gamesir_hid_hat_mask(int64_t value, uint64_t *mask) {
    switch (value) {
        case 0: *mask = DM_BTN_UP; break;
        case 1: *mask = DM_BTN_UP | DM_BTN_RIGHT; break;
        case 2: *mask = DM_BTN_RIGHT; break;
        case 3: *mask = DM_BTN_DOWN | DM_BTN_RIGHT; break;
        case 4: *mask = DM_BTN_DOWN; break;
        case 5: *mask = DM_BTN_DOWN | DM_BTN_LEFT; break;
        case 6: *mask = DM_BTN_LEFT; break;
        case 7: *mask = DM_BTN_UP | DM_BTN_LEFT; break;
        case 8: *mask = 0; break;
        default: return 0;
    }
    return 1;
}

static inline int dm_gamesir_hid_update(dm_gamesir_hid_state *state,
                                        uint32_t page, uint32_t usage,
                                        int64_t value, int64_t min,
                                        int64_t max) {
    const uint64_t dpad = DM_BTN_LEFT | DM_BTN_UP | DM_BTN_RIGHT | DM_BTN_DOWN;
    uint64_t old_buttons, bit = 0, hat = 0;
    int idx;
    int32_t axis;

    if (!state || min >= max) return 0;

    if (page == 0x09 && usage >= 1 && usage <= 16) {
        bit = (uint64_t)dm_gamesir_hid_button_bit(usage);
        if (!bit) return 0;
        old_buttons = state->buttons;
        if (dm_gamesir_hid_pressed(value, min, max))
            state->buttons |= bit;
        else
            state->buttons &= ~bit;
        return old_buttons != state->buttons;
    }

    if (page == 0x01) {
        if (usage == 57) {
            // A HID null position is outside the logical range, not necessarily
            // max+1: this GameSir sends 15. Publish release instead of retaining
            // the last direction, which otherwise overrides inventory sticks.
            if (value >= min && value <= max
                && !dm_gamesir_hid_hat_mask(value, &hat))
                return 0;
            old_buttons = state->buttons;
            state->buttons = (old_buttons & ~dpad) | hat;
            return old_buttons != state->buttons;
        }

        idx = dm_gamesir_hid_axis_index(usage);
        if (idx < 0) return 0;
        axis = dm_gamesir_hid_axis_value(value, min, max, idx == 1 || idx == 3);
        if (state->axes[idx] == axis) return 0;
        state->axes[idx] = axis;
        return 1;
    }

    if (page == 0x02 && (usage == 196 || usage == 197)) {
        bit = usage == 196 ? DM_BTN_ZR : DM_BTN_ZL;
        old_buttons = state->buttons;
        if (dm_gamesir_hid_pressed(value, min, max))
            state->buttons |= bit;
        else
            state->buttons &= ~bit;
        return old_buttons != state->buttons;
    }

    return 0;
}
