#ifndef DM_APP_MAC_CLOUD_H
#define DM_APP_MAC_CLOUD_H

#ifdef __cplusplus
extern "C" {
#endif

// Startup cloud reconciliation. Called on the macOS main thread before the save
// is mounted. Returns 1 to continue (online or with an explicit offline choice),
// or 0 when the user asks to quit. The token in config_path is never logged.
int dm_mac_cloud_boot(const char *root, const char *config_path);

// Queue one upload of the native committed snapshot. This never blocks and is
// ignored after a conflict, network/auth failure that needs a startup decision,
// or an offline launch.
void dm_mac_cloud_background(void);

// Queue the current committed snapshot and wait up to seconds for that worker
// item. Safe to call from the main thread; a timeout returns 0 without joining
// or cancelling an in-flight request.
int dm_mac_cloud_flush_wait(double seconds);

#ifdef DM_MAC_CLOUD_TESTING
typedef int (*dm_mac_cloud_test_alert_fn)(int kind, const char *message, void *context);
// kind: 0 conflict (0 cloud, 1 local, 2 offline, 3 quit), 1 network
// (0 retry, 1 offline, 2 quit), 2 unsupported local tree (0 offline, 1 quit).
void dm_mac_cloud_test_set_alert(dm_mac_cloud_test_alert_fn callback, void *context);
void dm_mac_cloud_test_reset(void);
int dm_mac_cloud_test_decide(const char *local_hash, const char *remote_revision,
                             const char *remote_hash, int baseline_present,
                             const char *baseline_revision, const char *baseline_hash);
int dm_mac_cloud_test_compare_utf16(const char *left, const char *right);
#endif

#ifdef __cplusplus
}
#endif

#endif
