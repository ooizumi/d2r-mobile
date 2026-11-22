// Unit tests for the GameSir X4A raw HID report mapping. This test uses only
// the descriptor values measured on the target device and does not start the
// game or touch saves/processes.
#include <stdint.h>
#include <stdio.h>

#include "../dm_gamesir_hid_map.h"

static int checks;
static int failures;

#define CHECK(cond, ...) do {                                                   \
    ++checks;                                                                   \
    if (!(cond)) {                                                              \
        ++failures;                                                             \
        printf("  FAIL line %d: ", __LINE__);                                   \
        printf(__VA_ARGS__);                                                    \
        putchar('\n');                                                          \
    }                                                                           \
} while (0)

static void check_axis(dm_gamesir_hid_state *s, uint32_t usage, int64_t value,
                       int64_t min, int64_t max, int32_t want, int want_change) {
    unsigned i = (unsigned)dm_gamesir_hid_axis_index(usage);
    int got_change = dm_gamesir_hid_update(s, 0x01, usage, value, min, max);

    CHECK(got_change == want_change, "axis %u changed=%d want=%d", usage,
          got_change, want_change);
    CHECK(s->axes[i] == want, "axis %u value=%d want=%d", usage,
          s->axes[i], want);
}

static void test_measured_report_mapping(void) {
    static const struct {
        uint32_t usage;
        uint64_t bit;
        const char *name;
    } buttons[] = {
        { 1,  DM_BTN_A,      "A" },
        { 2,  DM_BTN_B,      "B" },
        { 4,  DM_BTN_Y,      "X by Xbox position" },
        { 5,  DM_BTN_X,      "Y by Xbox position" },
        { 7,  DM_BTN_L,      "left shoulder" },
        { 8,  DM_BTN_R,      "right shoulder" },
        { 11, DM_BTN_MINUS,  "back" },
        { 12, DM_BTN_PLUS,   "start" },
        { 14, DM_BTN_LS,     "left stick click" },
        { 15, DM_BTN_RS,     "right stick click" },
    };
    dm_gamesir_hid_state s;
    uint64_t all = 0;
    unsigned i;

    dm_gamesir_hid_clear(&s);
    for (i = 0; i < sizeof buttons / sizeof buttons[0]; ++i) {
        all |= buttons[i].bit;
        CHECK(dm_gamesir_hid_update(&s, 0x09, buttons[i].usage, 1, 0, 1) == 1,
              "button %s did not change", buttons[i].name);
        CHECK((s.buttons & buttons[i].bit) != 0,
              "button %s missing from mask %#llx", buttons[i].name,
              (unsigned long long)s.buttons);
    }
    CHECK(s.buttons == all, "full measured mask=%#llx want=%#llx",
          (unsigned long long)s.buttons, (unsigned long long)all);

    CHECK(dm_gamesir_hid_update(&s, 0x09, 13, 1, 0, 1) == 0,
          "Guide must be ignored");
    CHECK(s.buttons == all, "Guide changed the mask");

    for (i = 0; i < sizeof buttons / sizeof buttons[0]; ++i) {
        CHECK(dm_gamesir_hid_update(&s, 0x09, buttons[i].usage, 0, 0, 1) == 1,
              "button %s release was swallowed", buttons[i].name);
        CHECK((s.buttons & buttons[i].bit) == 0,
              "button %s still set after release", buttons[i].name);
    }
    CHECK(s.buttons == 0, "all measured buttons should be released");
}

static void test_hat_diagonals_and_neutral(void) {
    static const struct {
        int64_t value;
        uint64_t dpad;
    } cases[] = {
        { 0, DM_BTN_UP },
        { 1, DM_BTN_UP | DM_BTN_RIGHT },
        { 2, DM_BTN_RIGHT },
        { 3, DM_BTN_DOWN | DM_BTN_RIGHT },
        { 4, DM_BTN_DOWN },
        { 5, DM_BTN_DOWN | DM_BTN_LEFT },
        { 6, DM_BTN_LEFT },
        { 7, DM_BTN_UP | DM_BTN_LEFT },
    };
    dm_gamesir_hid_state s;
    unsigned i;

    dm_gamesir_hid_clear(&s);
    CHECK(dm_gamesir_hid_update(&s, 0x09, 1, 1, 0, 1) == 1,
          "A press setup failed");
    CHECK(dm_gamesir_hid_update(&s, 0x01, 48, 255, 0, 255) == 1,
          "left X setup failed");
    for (i = 0; i < sizeof cases / sizeof cases[0]; ++i) {
        CHECK(dm_gamesir_hid_update(&s, 0x01, 57, cases[i].value, 0, 7) == 1,
              "hat %lld did not update", (long long)cases[i].value);
        CHECK((s.buttons & (DM_BTN_LEFT | DM_BTN_UP | DM_BTN_RIGHT | DM_BTN_DOWN))
                  == cases[i].dpad,
              "hat %lld dpad=%#llx want=%#llx", (long long)cases[i].value,
              (unsigned long long)(s.buttons
                  & (DM_BTN_LEFT | DM_BTN_UP | DM_BTN_RIGHT | DM_BTN_DOWN)),
              (unsigned long long)cases[i].dpad);
        CHECK((s.buttons & DM_BTN_A) != 0 && s.axes[0] == DM_STICK_MAX,
              "hat %lld disturbed unrelated state", (long long)cases[i].value);
    }
    CHECK(dm_gamesir_hid_update(&s, 0x01, 57, 8, 0, 7) == 1,
          "neutral hat must clear previous diagonal");
    CHECK((s.buttons & (DM_BTN_LEFT | DM_BTN_UP | DM_BTN_RIGHT | DM_BTN_DOWN)) == 0,
          "neutral hat left dpad bits");
    CHECK(dm_gamesir_hid_update(&s, 0x01, 57, 8, 0, 7) == 0,
          "already-neutral hat should report no change");
    CHECK(dm_gamesir_hid_update(&s, 0x01, 57, 9, 0, 7) == 0,
          "another null on an already-neutral hat should report no change");
}

static void test_axes_y_inversion_centre_endpoints_and_clamp(void) {
    dm_gamesir_hid_state s;
    dm_gamesir_hid_state before;
    unsigned i;

    dm_gamesir_hid_clear(&s);
    check_axis(&s, 48, 0, 0, 255, -DM_STICK_MAX, 1);
    check_axis(&s, 48, 127, 0, 255, 0, 1);
    check_axis(&s, 48, 128, 0, 255, 0, 0);
    check_axis(&s, 48, 255, 0, 255, DM_STICK_MAX, 1);

    dm_gamesir_hid_clear(&s);
    check_axis(&s, 49, 0, 0, 255, DM_STICK_MAX, 1);
    check_axis(&s, 49, 128, 0, 255, 0, 1);
    check_axis(&s, 49, 255, 0, 255, -DM_STICK_MAX, 1);

    dm_gamesir_hid_clear(&s);
    check_axis(&s, 50, -100, 0, 255, -DM_STICK_MAX, 1);
    check_axis(&s, 53, 999, 0, 255, -DM_STICK_MAX, 1);
    check_axis(&s, 50, 999, 0, 255, DM_STICK_MAX, 1);
    check_axis(&s, 53, -100, 0, 255, DM_STICK_MAX, 1);

    before = s;
    CHECK(dm_gamesir_hid_update(&s, 0x01, 48, 77, 9, 9) == 0,
          "invalid-axis range must report no change");
    CHECK(s.buttons == before.buttons, "invalid-axis range changed buttons");
    for (i = 0; i < 4; ++i)
        CHECK(s.axes[i] == before.axes[i],
              "invalid-axis range changed axis %u", i);
}

static void test_hat_null_release_then_stick(void) {
    /* The real X4A reports 15 on release, outside its advertised 0..7 range.
     * HID null reports must release every direction, preserving other input. */
    static const int64_t nulls[] = { 15, 8, -1, 9 };
    const uint64_t dpad = DM_BTN_LEFT | DM_BTN_UP | DM_BTN_RIGHT | DM_BTN_DOWN;
    dm_gamesir_hid_state s;
    unsigned n;
    int direction;

    for (n = 0; n < sizeof nulls / sizeof nulls[0]; ++n) {
        for (direction = 0; direction < 8; ++direction) {
            dm_gamesir_hid_clear(&s);
            dm_gamesir_hid_update(&s, 9, 1, 1, 0, 1);
            dm_gamesir_hid_update(&s, 2, 196, 255, 0, 255);
            dm_gamesir_hid_update(&s, 1, 48, 255, 0, 255);
            CHECK(dm_gamesir_hid_update(&s, 1, 57, direction, 0, 7) == 1,
                  "direction %d press not reported", direction);
            CHECK((s.buttons & dpad) != 0, "direction %d missing", direction);
            CHECK(dm_gamesir_hid_update(&s, 1, 57, nulls[n], 0, 7) == 1,
                  "direction %d null %lld release swallowed", direction, (long long)nulls[n]);
            CHECK(s.buttons == (DM_BTN_A | DM_BTN_ZR) && s.axes[0] == DM_STICK_MAX,
                  "null %lld must clear only dpad", (long long)nulls[n]);
            CHECK(dm_gamesir_hid_update(&s, 1, 57, nulls[n], 0, 7) == 0,
                  "duplicate null should not publish a change");
            CHECK(dm_gamesir_hid_update(&s, 1, 48, 0, 0, 255) == 1,
                  "stick after null release did not update");
            CHECK((s.buttons & dpad) == 0 && s.axes[0] == -DM_STICK_MAX,
                  "stick update retained a stuck dpad direction");
        }
    }
}

static void test_triggers_half_threshold_and_independent_release(void) {
    dm_gamesir_hid_state s;

    dm_gamesir_hid_clear(&s);
    CHECK(dm_gamesir_hid_update(&s, 0x02, 196, 127, 0, 255) == 0,
          "Gas below half must not press");
    CHECK(dm_gamesir_hid_update(&s, 0x02, 197, 127, 0, 255) == 0,
          "Brake below half must not press");
    CHECK(dm_gamesir_hid_update(&s, 0x02, 196, 128, 0, 255) == 1,
          "Gas at half must press");
    CHECK(s.buttons == DM_BTN_ZR, "Gas must map to ZR");
    CHECK(dm_gamesir_hid_update(&s, 0x02, 197, 128, 0, 255) == 1,
          "Brake at half must press");
    CHECK(s.buttons == (DM_BTN_ZR | DM_BTN_ZL), "Both triggers must coexist");

    CHECK(dm_gamesir_hid_update(&s, 0x02, 196, 0, 0, 255) == 1,
          "Gas release must be reported");
    CHECK(s.buttons == DM_BTN_ZL, "Gas release disturbed Brake");
    CHECK(dm_gamesir_hid_update(&s, 0x02, 196, 0, 0, 255) == 0,
          "duplicate Gas release must not be reported");
    CHECK(dm_gamesir_hid_update(&s, 0x02, 197, 0, 0, 255) == 1,
          "Brake release must be reported");
    CHECK(s.buttons == 0, "Brake release did not clear ZL");

    CHECK(dm_gamesir_hid_update(&s, 0x02, 196, 999, 0, 255) == 1,
          "out-of-range Gas must clamp to pressed");
    CHECK(dm_gamesir_hid_update(&s, 0x02, 196, -100, 0, 255) == 1,
          "out-of-range Gas release must clamp to released");
    CHECK(s.buttons == 0, "clamped Gas did not return to released");
    CHECK(dm_gamesir_hid_update(&s, 0x02, 197, 255, 5, 5) == 0,
          "invalid trigger range changed state");
    CHECK(s.buttons == 0, "invalid trigger range changed buttons");
}

static void test_clear_and_unknown_reports(void) {
    dm_gamesir_hid_state s;
    uint64_t before;
    int32_t axes_before[4];
    unsigned i;

    dm_gamesir_hid_clear(&s);
    CHECK(dm_gamesir_hid_update(&s, 0x09, 1, 1, 0, 1) == 1,
          "A press setup failed");
    CHECK(dm_gamesir_hid_update(&s, 0x01, 49, 0, 0, 255) == 1,
          "left Y setup failed");
    before = s.buttons;
    for (i = 0; i < 4; ++i) axes_before[i] = s.axes[i];

    CHECK(dm_gamesir_hid_update(&s, 0x07, 1, 1, 0, 1) == 0,
          "unknown page changed state");
    CHECK(dm_gamesir_hid_update(&s, 0x01, 54, 1, 0, 1) == 0,
          "unknown axis usage changed state");
    CHECK(dm_gamesir_hid_update(&s, 0x09, 3, 1, 0, 1) == 0,
          "unmapped button usage changed state");
    CHECK(dm_gamesir_hid_update(&s, 0x09, 1, 0, 1, 1) == 0,
          "invalid button range changed state");
    CHECK(dm_gamesir_hid_update(&s, 0x01, 57, 0, 1, 1) == 0,
          "invalid hat range changed state");
    CHECK(dm_gamesir_hid_update(&s, 0x02, 198, 255, 0, 255) == 0,
          "unknown trigger changed state");
    CHECK(s.buttons == before, "unknown report changed buttons");
    for (i = 0; i < 4; ++i)
        CHECK(s.axes[i] == axes_before[i], "unknown report changed axis %u", i);

    CHECK(dm_gamesir_hid_update(&s, 0x09, 1, 0, 0, 1) == 1,
          "a valid release was swallowed after unknown reports");
    CHECK((s.buttons & DM_BTN_A) == 0, "A remained pressed after release");

    dm_gamesir_hid_clear(&s);
    CHECK(s.buttons == 0 && s.axes[0] == 0 && s.axes[1] == 0
              && s.axes[2] == 0 && s.axes[3] == 0,
          "clear must zero buttons and all axes");
}

int main(void) {
    test_measured_report_mapping();
    test_hat_diagonals_and_neutral();
    test_hat_null_release_then_stick();
    test_axes_y_inversion_centre_endpoints_and_clamp();
    test_triggers_half_threshold_and_independent_release();
    test_clear_and_unknown_reports();

    printf("t_gamesir_hid: %d checks, %d failures\n", checks, failures);
    return failures != 0;
}
