// 生命周期并发回归：开关、真实保存后再挂起、快速后台→前台、保存超时后恢复。
// Lifecycle concurrency regressions: disabled autosave, save-before-suspend, a fast
// background-to-foreground race, and recovery after a save timeout.
// Run outside the repository: cc -std=gnu17 -pthread lifecycle_dispatch_test.c -o <external-output>
#include "../lifecycle_dispatch.h"
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <time.h>
#ifdef NDEBUG
#error "Build this CPU regression with assertions enabled"
#endif

static pthread_mutex_t mock_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t mock_changed = PTHREAD_COND_INITIALIZER;
static int save_calls, save_enabled, save_entered, block_save, release_save, save_commits;
static int discard_active, graphics_paused, audio_paused, final_focus;
static int seen_order[64], seen_count;

static void record(int value) { assert(seen_count < 64); seen_order[seen_count++] = value; }
static int index_of(int value) {
    for (int i = 0; i < seen_count; ++i) if (seen_order[i] == value) return i;
    return -1;
}
static void reset_mock(void) {
    pthread_mutex_lock(&mock_mutex);
    save_calls = save_enabled = save_entered = block_save = release_save = 0;
    save_commits = 1;
    discard_active = graphics_paused = audio_paused = seen_count = 0;
    final_focus = -1;
    pthread_mutex_unlock(&mock_mutex);
}
static void focus(int state) {
    pthread_mutex_lock(&mock_mutex);
    final_focus = state;
    record(10 + state);
    pthread_mutex_unlock(&mock_mutex);
}
static void audio(int paused) {
    pthread_mutex_lock(&mock_mutex);
    audio_paused = paused;
    record(20 + paused);
    pthread_mutex_unlock(&mock_mutex);
}
static void graphics(int paused) {
    pthread_mutex_lock(&mock_mutex);
    graphics_paused = paused;
    record(30 + paused);
    pthread_mutex_unlock(&mock_mutex);
}
static void background_save(int enabled) {
    pthread_mutex_lock(&mock_mutex);
    ++save_calls;
    save_enabled = enabled;
    record(40);
    if (!enabled) {
        record(41);
        pthread_cond_broadcast(&mock_changed);
        pthread_mutex_unlock(&mock_mutex);
        return;
    }
    discard_active = save_entered = 1;
    pthread_cond_broadcast(&mock_changed);
    while (block_save && !release_save) pthread_cond_wait(&mock_changed, &mock_mutex);
    discard_active = 0;
    record(save_commits ? 42 : 43);
    pthread_cond_broadcast(&mock_changed);
    pthread_mutex_unlock(&mock_mutex);
}

static struct timespec timeout(void) {
    struct timespec t;
    clock_gettime(CLOCK_REALTIME, &t);
    t.tv_sec += 3;
    return t;
}
static void wait_mock(int *flag) {
    struct timespec until = timeout();
    pthread_mutex_lock(&mock_mutex);
    while (!*flag) assert(!pthread_cond_timedwait(&mock_changed, &mock_mutex, &until));
    pthread_mutex_unlock(&mock_mutex);
}
static void wait_settled(dm_lifecycle_dispatch *d) {
    struct timespec until = timeout();
    pthread_mutex_lock(&d->mutex);
    while (d->completed != d->requested || d->save_completed != d->save_requested)
        assert(!pthread_cond_timedwait(&d->changed, &d->mutex, &until));
    pthread_mutex_unlock(&d->mutex);
}
static void post(dm_lifecycle_dispatch *d, int event) {
    if (event == 2 || event == 3) {
        pthread_mutex_lock(&mock_mutex);
        discard_active = 0;   // nativeFocus(2/3) clears the save-only present bypass immediately
        pthread_mutex_unlock(&mock_mutex);
    }
    dm_lifecycle_post(d, event);
}
static void start_test(dm_lifecycle_dispatch *d, dm_lifecycle_ops ops) {
    reset_mock();
    assert(!dm_lifecycle_start(d, ops));
}
static void finish_test(dm_lifecycle_dispatch *d) {
    post(d, 3);
    wait_settled(d);
    dm_lifecycle_stop(d);
    assert(!discard_active);
}

static void test_disabled_still_suspends(void) {
    dm_lifecycle_dispatch dispatcher = DM_LIFECYCLE_DISPATCH_INIT;
    dm_lifecycle_ops ops = {focus, audio, graphics, background_save};
    start_test(&dispatcher, ops);
    dm_lifecycle_set_background_autosave(&dispatcher, 0);
    post(&dispatcher, 1);
    wait_settled(&dispatcher);
    assert(save_calls == 1 && save_enabled == 0 && !discard_active);
    assert(graphics_paused && audio_paused && final_focus == 3);
    assert(index_of(41) >= 0 && index_of(31) > index_of(41));
    finish_test(&dispatcher);
}

static void test_save_before_suspend(void) {
    dm_lifecycle_dispatch dispatcher = DM_LIFECYCLE_DISPATCH_INIT;
    dm_lifecycle_ops ops = {focus, audio, graphics, background_save};
    start_test(&dispatcher, ops);
    post(&dispatcher, 1);
    wait_settled(&dispatcher);
    assert(save_calls == 1 && save_enabled == 1 && !discard_active);
    assert(graphics_paused && final_focus == 3);
    assert(index_of(42) >= 0 && index_of(31) > index_of(42));
    finish_test(&dispatcher);
}

static void test_fast_foreground_keeps_save_but_skips_stale_suspend(void) {
    dm_lifecycle_dispatch dispatcher = DM_LIFECYCLE_DISPATCH_INIT;
    dm_lifecycle_ops ops = {focus, audio, graphics, background_save};
    start_test(&dispatcher, ops);
    pthread_mutex_lock(&mock_mutex);
    block_save = 1;
    pthread_mutex_unlock(&mock_mutex);
    post(&dispatcher, 1);
    wait_mock(&save_entered);
    post(&dispatcher, 2);
    post(&dispatcher, 3);
    pthread_mutex_lock(&mock_mutex);
    assert(!discard_active);
    release_save = 1;
    pthread_cond_broadcast(&mock_changed);
    pthread_mutex_unlock(&mock_mutex);
    wait_settled(&dispatcher);
    assert(save_calls == 1 && save_enabled == 1 && !discard_active);
    assert(!graphics_paused && final_focus == 1 && index_of(31) < 0);
    finish_test(&dispatcher);
}

static void test_save_timeout_then_resume(void) {
    dm_lifecycle_dispatch dispatcher = DM_LIFECYCLE_DISPATCH_INIT;
    dm_lifecycle_ops ops = {focus, audio, graphics, background_save};
    start_test(&dispatcher, ops);
    pthread_mutex_lock(&mock_mutex);
    save_commits = 0;
    pthread_mutex_unlock(&mock_mutex);
    post(&dispatcher, 1);
    wait_settled(&dispatcher);
    assert(save_calls == 1 && save_enabled == 1 && !discard_active);
    assert(index_of(42) < 0 && index_of(43) >= 0);   // zero commits is a timeout, not success
    assert(graphics_paused && index_of(31) > index_of(43));   // timeout still reaches safe suspend
    post(&dispatcher, 3);
    wait_settled(&dispatcher);
    assert(!graphics_paused && !audio_paused && final_focus == 1);
    int resumed = index_of(30);
    assert(resumed >= 0 && resumed + 2 < seen_count && seen_order[resumed + 1] == 20 && seen_order[resumed + 2] == 11);
    dm_lifecycle_stop(&dispatcher);
}

int main(void) {
    test_disabled_still_suspends();
    test_save_before_suspend();
    test_fast_foreground_keeps_save_but_skips_stale_suspend();
    test_save_timeout_then_resume();
    puts("lifecycle_dispatch_test: ok");
    return 0;
}
