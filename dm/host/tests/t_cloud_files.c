// Host regression: snapshots never overwrite, install exchanges whole trees,
// retains recoverable old data, rejects symlinks/outside paths/partial trees.
#include <assert.h>
#include "../dm_cloud_files.h"

static void write_text(const char *p, const char *s) {
    FILE *f = fopen(p, "wb"); assert(f); assert(fwrite(s, 1, strlen(s), f) == strlen(s)); assert(!fclose(f));
}
static void expect_text(const char *p, const char *s) {
    char b[80] = {0}; FILE *f = fopen(p, "rb"); assert(f); fread(b, 1, sizeof b - 1, f); fclose(f); assert(!strcmp(b, s));
}
int main(int argc, char **argv) {
    assert(argc == 2); char root[PATH_MAX];
    assert(snprintf(root, sizeof root, "%s/cloud-native-XXXXXX", argv[1]) < (int)sizeof root); assert(mkdtemp(root));
    char cloud[PATH_MAX], save[PATH_MAX], target[PATH_MAX], file[PATH_MAX], old[PATH_MAX];
    snprintf(cloud, sizeof cloud, "%s/cloud-save", root); assert(!mkdir(cloud, 0700));
    snprintf(save, sizeof save, "%s/save", root);
    snprintf(target, sizeof target, "%s/snapshot", cloud);
    assert(!dm_cloud_snapshot_files(root, target)); // Fresh device.
    assert(dm_cloud_snapshot_files(root, target) == -EEXIST);
    assert(!rmdir(target)); assert(!mkdir(save, 0700));
    snprintf(file, sizeof file, "%s/Hero.d2s", save); write_text(file, "before");
    assert(!dm_cloud_snapshot_files(root, target));
    snprintf(old, sizeof old, "%s/Hero.d2s", target); expect_text(old, "before");
    write_text(file, "local-new"); expect_text(old, "before");
    write_text(old, "remote-new");
    assert(!dm_cloud_install_files(root, target));
    expect_text(file, "remote-new"); expect_text(old, "local-new");
    // Invalid target must not change the authoritative tree.
    assert(dm_cloud_install_files(root, save) < 0); expect_text(file, "remote-new");
    char link[PATH_MAX], bad[PATH_MAX];
    snprintf(link, sizeof link, "%s/link", target); assert(!symlink(file, link));
    assert(dm_cloud_install_files(root, target) < 0); expect_text(file, "remote-new");
    snprintf(bad, sizeof bad, "%s/outside", root); assert(dm_cloud_snapshot_files(root, bad) < 0);
    assert(!unlink(link));
    // Recovery is another atomic exchange, preserving the version replaced.
    assert(!dm_cloud_install_files(root, target));
    expect_text(file, "local-new"); expect_text(old, "remote-new");
    printf("cloud native snapshots/atomic restore/backup/confinement: PASS\n");
    return 0;
}
