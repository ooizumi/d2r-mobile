// Whole-directory cloud snapshots. Caller holds the save transaction lock.
// The destination is confined to files/cloud-save; old saves remain there after
// an atomic exchange. No network or conflict policy belongs in this layer.
#ifndef DM_CLOUD_FILES_H
#define DM_CLOUD_FILES_H
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/clonefile.h>
#include <unistd.h>

static int dm_cloud_sync_tree(const char *path) {
    struct stat st;
    if (lstat(path, &st)) return -1;
    if (!S_ISREG(st.st_mode) && !S_ISDIR(st.st_mode)) { errno = EINVAL; return -1; }
    if (S_ISDIR(st.st_mode)) {
        DIR *dir = opendir(path); if (!dir) return -1;
        char *child = malloc(PATH_MAX); int rc = child ? 0 : -1;
        if (!child) errno = ENOMEM;
        struct dirent *entry;
        while (!rc && (entry = readdir(dir))) {
            if (!strcmp(entry->d_name, ".") || !strcmp(entry->d_name, "..")) continue;
            if (snprintf(child, PATH_MAX, "%s/%s", path, entry->d_name) >= PATH_MAX) { errno = ENAMETOOLONG; rc = -1; break; }
            rc = dm_cloud_sync_tree(child);
        }
        int saved = errno; free(child); closedir(dir); errno = saved;
        if (rc) return -1;
    }
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) return -1;
    int rc = fsync(fd), saved = errno; close(fd); errno = saved;
    return rc;
}

static int dm_cloud_paths(const char *root, const char *target, char save[PATH_MAX], char parent[PATH_MAX]) {
    char canonical[PATH_MAX], storage[PATH_MAX], allowed[PATH_MAX];
    if (!root || !target || root[0] != '/' || target[0] != '/' || strlen(target) >= PATH_MAX) { errno = EINVAL; return -1; }
    if (!realpath(root, canonical)) return -1;
    if (snprintf(save, PATH_MAX, "%s/save", canonical) >= PATH_MAX ||
        snprintf(storage, PATH_MAX, "%s/cloud-save", canonical) >= PATH_MAX) { errno = ENAMETOOLONG; return -1; }
    struct stat st;
    if (lstat(storage, &st) || !S_ISDIR(st.st_mode)) { errno = EINVAL; return -1; }
    if (!realpath(storage, allowed) || strcmp(allowed, storage)) { errno = EINVAL; return -1; }
    char path[PATH_MAX]; strcpy(path, target);
    char *slash = strrchr(path, '/');
    if (!slash || !slash[1] || !strcmp(slash + 1, ".") || !strcmp(slash + 1, "..")) { errno = EINVAL; return -1; }
    *slash = 0;
    if (!realpath(path, parent)) return -1;
    size_t n = strlen(allowed);
    if (strncmp(parent, allowed, n) || (parent[n] && parent[n] != '/')) { errno = EPERM; return -1; }
    return 0;
}

static int dm_cloud_snapshot_files(const char *root, const char *target) {
    char save[PATH_MAX], parent[PATH_MAX];
    if (dm_cloud_paths(root, target, save, parent)) return -errno;
    struct stat st;
    if (!lstat(target, &st)) return -EEXIST;
    if (errno != ENOENT) return -errno;
    if (lstat(save, &st)) {
        if (errno != ENOENT) return -errno;
        return mkdir(target, 0700) ? -errno : 0;
    }
    if (!S_ISDIR(st.st_mode)) return -EINVAL;
    // Also rejects special files/symlinks before clonefile on Apple platforms.
    if (dm_cloud_sync_tree(save) || clonefile(save, target, 0)) return -errno;
    return 0;
}

static int dm_cloud_install_files(const char *root, const char *target) {
    char save[PATH_MAX], parent[PATH_MAX];
    if (dm_cloud_paths(root, target, save, parent)) return -errno;
    struct stat st;
    if (lstat(target, &st)) return -errno;
    if (!S_ISDIR(st.st_mode)) return -EINVAL;
    if (dm_cloud_sync_tree(target)) return -errno;
    if (!lstat(save, &st)) {
        if (!S_ISDIR(st.st_mode)) return -EINVAL;
        if (dm_cloud_sync_tree(save) || renamex_np(target, save, RENAME_SWAP)) return -errno;
    } else {
        if (errno != ENOENT) return -errno;
        if (rename(target, save)) return -errno;
    }
    // Both directory entries must survive a power loss. Do not remove target,
    // even on an fsync error: it may already contain the previous local save.
    char rootpath[PATH_MAX]; strcpy(rootpath, save); *strrchr(rootpath, '/') = 0;
    int fd = open(rootpath, O_RDONLY | O_CLOEXEC); if (fd < 0) return -errno;
    int rc = fsync(fd), saved = errno; close(fd); if (rc) return -saved;
    fd = open(parent, O_RDONLY | O_CLOEXEC); if (fd < 0) return -errno;
    rc = fsync(fd); saved = errno; close(fd); return rc ? -saved : 0;
}
#endif
