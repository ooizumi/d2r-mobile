// macOS HTTP cloud-save coordinator for D2R Mobile.
// Protocol, snapshot validation, startup conflict gate and the single background
// upload worker live here. The native save copy and atomic pre-mount exchange are
// provided by dm_fs_cloud_snapshot / dm_fs_cloud_install.

#import <Cocoa/Cocoa.h>
#import <CommonCrypto/CommonDigest.h>
#import <copyfile.h>

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <sys/clonefile.h>
#include <sys/stat.h>
#include <sys/xattr.h>
#include <unistd.h>

#include "app_cloud_mac.h"

int dm_fs_cloud_snapshot(const char *root, const char *target);
int dm_fs_cloud_install(const char *root, const char *target);

static NSString *const DMMacCloudErrorDomain = @"DMMacCloudError";
static NSString *const DMMacCloudStateKey = @"state";
static NSString *const DMMacCloudEmptySHA256 = @"e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
static NSString *const DMMacCloudGuestNameKey = @"org.d2rmobile.guest-name";
static NSString *const DMMacCloudUserGuestNameKey = @"user.org.d2rmobile.guest-name";

enum {
    DMMacCloudErrorConfig = 1,
    DMMacCloudErrorFilesystem,
    DMMacCloudErrorProtocol,
    DMMacCloudErrorHTTP,
    DMMacCloudErrorAuthentication,
    DMMacCloudErrorConflict,
    DMMacCloudErrorUnsupported,
    DMMacCloudErrorReevaluate
};

static const NSUInteger DMMacCloudMaxFiles = 2048;
static const unsigned long long DMMacCloudMaxFileBytes = 8ULL * 1024ULL * 1024ULL;
static const unsigned long long DMMacCloudMaxTotalBytes = 24ULL * 1024ULL * 1024ULL;
static const NSUInteger DMMacCloudMaxHTTPBytes = 48U * 1024U * 1024U;
static const NSUInteger DMMacCloudMaxConfigBytes = 256U * 1024U;
static const NSUInteger DMMacCloudMaxPathUTF16 = 512;
static const NSUInteger DMMacCloudMaxPathComponents = 8;
static const NSTimeInterval DMMacCloudUploadInterval = 30.0;

static NSError *DMError(NSInteger code, NSString *message) {
    return [NSError errorWithDomain:DMMacCloudErrorDomain code:code
                           userInfo:@{NSLocalizedDescriptionKey: message ?: @"云存档操作失败"}];
}

static NSError *DMConflictError(NSString *message, id state) {
    NSMutableDictionary *info = [@{NSLocalizedDescriptionKey: message ?: @"云存档冲突"} mutableCopy];
    if (state) info[DMMacCloudStateKey] = state;
    return [NSError errorWithDomain:DMMacCloudErrorDomain code:DMMacCloudErrorConflict userInfo:info];
}

static BOOL DMIsSHA256(NSString *value) {
    if (![value isKindOfClass:NSString.class] || value.length != 64) return NO;
    const char *s = value.UTF8String;
    if (!s || strlen(s) != 64) return NO;
    for (NSUInteger i = 0; i < 64; i++) {
        char c = s[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return NO;
    }
    return YES;
}

static NSString *DMNormalizedRemoteHash(NSString *value) {
    return value.length ? value : DMMacCloudEmptySHA256;
}

static BOOL DMIsEmptyHash(NSString *value) {
    return value.length == 0 || [value isEqualToString:DMMacCloudEmptySHA256];
}

static NSString *DMHex(const unsigned char *bytes, NSUInteger length) {
    static const char digits[] = "0123456789abcdef";
    char *out = malloc(length * 2 + 1);
    if (!out) return nil;
    for (NSUInteger i = 0; i < length; i++) {
        out[i * 2] = digits[bytes[i] >> 4];
        out[i * 2 + 1] = digits[bytes[i] & 15];
    }
    out[length * 2] = 0;
    NSString *value = @(out);
    free(out);
    return value;
}

static NSString *DMSHA256Data(NSData *data) {
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    if (CC_SHA256(data.bytes, (CC_LONG)data.length, digest) == NULL) return nil;
    return DMHex(digest, sizeof digest);
}

static int DMUTF16Compare(NSString *left, NSString *right) {
    NSUInteger n = MIN(left.length, right.length);
    for (NSUInteger i = 0; i < n; i++) {
        unichar a = [left characterAtIndex:i], b = [right characterAtIndex:i];
        if (a < b) return -1;
        if (a > b) return 1;
    }
    if (left.length < right.length) return -1;
    if (left.length > right.length) return 1;
    return 0;
}

static BOOL DMHasISOControl(NSString *value) {
    for (NSUInteger i = 0; i < value.length; i++) {
        unichar c = [value characterAtIndex:i];
        if (c < 32 || (c >= 127 && c <= 159)) return YES;
    }
    return NO;
}

static BOOL DMIsExcludedComponent(NSString *name) {
    return name.length > 0 && ([name characterAtIndex:0] == '.' || [name characterAtIndex:0] == '_');
}

static NSString *DMFoldAliasComponent(NSString *component) {
    NSLocale *locale = [NSLocale localeWithLocaleIdentifier:@"en_US_POSIX"];
    NSString *folded = [component stringByFoldingWithOptions:NSCaseInsensitiveSearch locale:locale];
    folded = [folded stringByFoldingWithOptions:NSWidthInsensitiveSearch locale:locale];
    return [folded precomposedStringWithCanonicalMapping];
}

@interface DMMacCloudEntry : NSObject
@property(nonatomic, copy) NSString *path;
@property(nonatomic) unsigned long long size;
@property(nonatomic, copy) NSString *sha256;
@property(nonatomic, copy) NSData *data;
- (instancetype)initWithPath:(NSString *)path size:(unsigned long long)size sha256:(NSString *)sha256;
@end

@implementation DMMacCloudEntry
- (instancetype)initWithPath:(NSString *)path size:(unsigned long long)size sha256:(NSString *)sha256 {
    if ((self = [super init])) { _path = [path copy]; _size = size; _sha256 = [sha256 copy]; }
    return self;
}
@end

@interface DMMacCloudState : NSObject
@property(nonatomic, copy) NSString *revision;
@property(nonatomic, copy) NSString *snapshotSHA256;
@property(nonatomic, copy) NSString *updatedAt;
@property(nonatomic, copy) NSString *device;
@property(nonatomic) NSUInteger fileCount;
@property(nonatomic) unsigned long long totalBytes;
@end

@implementation DMMacCloudState
@end

@interface DMMacCloudBaseline : NSObject
@property(nonatomic, copy) NSString *scope;
@property(nonatomic, copy) NSString *revision;
@property(nonatomic, copy) NSString *snapshotSHA256;
@end

@implementation DMMacCloudBaseline
@end

@interface DMMacCloudConfig : NSObject
@property(nonatomic, copy) NSString *endpoint;
@property(nonatomic, copy) NSString *token;
@end

@implementation DMMacCloudConfig
@end

@interface DMMacCloudLocalSnapshot : NSObject
@property(nonatomic, copy) NSString *root;
@property(nonatomic, copy) NSArray<DMMacCloudEntry *> *files;
@property(nonatomic, copy) NSString *snapshotSHA256;
@property(nonatomic) unsigned long long totalBytes;
@end

@implementation DMMacCloudLocalSnapshot
@end

@interface DMMacCloudRemoteSnapshot : NSObject
@property(nonatomic, strong) DMMacCloudState *state;
@property(nonatomic, copy) NSArray<DMMacCloudEntry *> *files;
@end

@implementation DMMacCloudRemoteSnapshot
@end

@interface DMMacCloudHTTPResult : NSObject
@property(nonatomic) NSInteger status;
@property(nonatomic, copy) NSData *data;
@property(nonatomic, strong) NSError *error;
@end

@implementation DMMacCloudHTTPResult
@end

@interface DMMacCloudTransfer : NSObject <NSURLSessionDataDelegate>
@property(nonatomic) dispatch_semaphore_t done;
@property(nonatomic) NSUInteger limit;
@property(nonatomic, strong) NSMutableData *data;
@property(nonatomic) NSInteger status;
@property(nonatomic) BOOL redirected;
@property(nonatomic) BOOL overflowed;
@property(nonatomic, strong) NSError *error;
@property(nonatomic) BOOL completed;
@end

@implementation DMMacCloudTransfer
- (instancetype)initWithLimit:(NSUInteger)limit {
    if ((self = [super init])) { _done = dispatch_semaphore_create(0); _limit = limit; _data = [NSMutableData data]; _status = -1; }
    return self;
}
- (void)URLSession:(NSURLSession *)session task:(NSURLSessionTask *)task
 willPerformHTTPRedirection:(NSHTTPURLResponse *)response newRequest:(NSURLRequest *)request
 completionHandler:(void (^)(NSURLRequest *))completionHandler {
    self.redirected = YES;
    completionHandler(nil);
}
- (void)URLSession:(NSURLSession *)session dataTask:(NSURLSessionDataTask *)task
 didReceiveResponse:(NSURLResponse *)response completionHandler:(void (^)(NSURLSessionResponseDisposition))completionHandler {
    if ([response isKindOfClass:NSHTTPURLResponse.class]) {
        self.status = ((NSHTTPURLResponse *)response).statusCode;
        long long length = response.expectedContentLength;
        if (length > (long long)self.limit) {
            self.overflowed = YES;
            self.error = DMError(DMMacCloudErrorProtocol, @"云存档 HTTP 响应超过大小上限");
            completionHandler(NSURLSessionResponseCancel);
            return;
        }
    }
    completionHandler(NSURLSessionResponseAllow);
}
- (void)URLSession:(NSURLSession *)session dataTask:(NSURLSessionDataTask *)task didReceiveData:(NSData *)data {
    if (self.data.length + data.length > self.limit) {
        self.overflowed = YES;
        self.error = DMError(DMMacCloudErrorProtocol, @"云存档 HTTP 响应超过大小上限");
        [task cancel];
        return;
    }
    [self.data appendData:data];
}
- (void)URLSession:(NSURLSession *)session task:(NSURLSessionTask *)task didCompleteWithError:(NSError *)error {
    if (error && !self.error) self.error = error;
    self.completed = YES;
    dispatch_semaphore_signal(self.done);
}
@end

static BOOL DMValidateRelativePath(NSString *path, BOOL cloudPath, NSError **error) {
    if (![path isKindOfClass:NSString.class] || !path.length || path.length > DMMacCloudMaxPathUTF16 ||
        [path hasPrefix:@"/"] || [path hasSuffix:@"/"] || [path containsString:@"\\"] || [path containsString:@":"]) {
        if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档路径非法");
        return NO;
    }
    NSArray<NSString *> *parts = [path componentsSeparatedByString:@"/"];
    if (!parts.count || parts.count > DMMacCloudMaxPathComponents) {
        if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档路径层级过多");
        return NO;
    }
    for (NSUInteger i = 0; i < parts.count; i++) {
        NSString *part = parts[i];
        if (!part.length || [part isEqualToString:@"."] || [part isEqualToString:@".."] || DMHasISOControl(part)) {
            if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档路径包含非法组件");
            return NO;
        }
        if (cloudPath && (DMIsExcludedComponent(part) || (i == 0 && [part isEqualToString:@"Settings"]))) {
            if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档包含应保留在本地的路径");
            return NO;
        }
    }
    return YES;
}

static BOOL DMValidatePathSet(NSArray<DMMacCloudEntry *> *files, BOOL cloudPath, NSError **error) {
    NSMutableDictionary<NSString *, NSString *> *nodes = [NSMutableDictionary dictionary];
    NSMutableSet<NSString *> *names = [NSMutableSet set];
    NSMutableSet<NSString *> *directories = [NSMutableSet set];
    for (DMMacCloudEntry *file in files) {
        if (!DMValidateRelativePath(file.path, cloudPath, error)) return NO;
        if ([names containsObject:file.path] || [directories containsObject:file.path]) {
            if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档文件与目录路径冲突");
            return NO;
        }
        NSArray<NSString *> *parts = [file.path componentsSeparatedByString:@"/"];
        NSMutableString *rawPrefix = [NSMutableString string];
        NSMutableString *aliasPrefix = [NSMutableString string];
        for (NSUInteger i = 0; i < parts.count; i++) {
            if (i) { [rawPrefix appendString:@"/"]; [aliasPrefix appendString:@"/"]; }
            [rawPrefix appendString:parts[i]];
            [aliasPrefix appendString:DMFoldAliasComponent(parts[i])];
            NSString *existing = nodes[aliasPrefix];
            if (existing && ![existing isEqualToString:rawPrefix]) {
                if (error) *error = DMError(DMMacCloudErrorUnsupported,
                    @"云存档包含 APFS 大小写或 Unicode 正规化别名冲突");
                return NO;
            }
            nodes[[aliasPrefix copy]] = [rawPrefix copy];
            if (i + 1 < parts.count) {
                if ([names containsObject:rawPrefix]) {
                    if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档文件与目录路径冲突");
                    return NO;
                }
                [directories addObject:[rawPrefix copy]];
            }
        }
        [names addObject:file.path];
    }
    return YES;
}

static NSString *DMSnapshotDigest(NSArray<DMMacCloudEntry *> *files) {
    NSMutableData *canonical = [NSMutableData data];
    for (DMMacCloudEntry *file in files) {
        NSData *path = [file.path dataUsingEncoding:NSUTF8StringEncoding];
        [canonical appendData:path];
        const unsigned char zero = 0;
        NSString *size = [NSString stringWithFormat:@"%llu", file.size];
        [canonical appendBytes:&zero length:1];
        [canonical appendData:[size dataUsingEncoding:NSUTF8StringEncoding]];
        [canonical appendBytes:&zero length:1];
        [canonical appendData:[file.sha256 dataUsingEncoding:NSUTF8StringEncoding]];
        [canonical appendBytes:"\n" length:1];
    }
    return DMSHA256Data(canonical);
}

static NSString *DMSHA256File(NSString *path, unsigned long long expectedSize, NSError **error) {
    int fd = open(path.fileSystemRepresentation, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) {
        if (error) *error = DMError(DMMacCloudErrorFilesystem, @"无法读取云存档文件");
        return nil;
    }
    struct stat before;
    if (fstat(fd, &before) || !S_ISREG(before.st_mode) || (unsigned long long)before.st_size != expectedSize) {
        close(fd);
        if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档文件大小或类型不一致");
        return nil;
    }
    CC_SHA256_CTX ctx;
    CC_SHA256_Init(&ctx);
    unsigned char *buffer = malloc(1024 * 1024);
    if (!buffer) {
        close(fd);
        if (error) *error = DMError(DMMacCloudErrorFilesystem, @"内存不足");
        return nil;
    }
    unsigned long long total = 0;
    for (;;) {
        ssize_t n = read(fd, buffer, 1024 * 1024);
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) {
            free(buffer); close(fd);
            if (error) *error = DMError(DMMacCloudErrorFilesystem, @"读取云存档文件失败");
            return nil;
        }
        if (!n) break;
        total += (unsigned long long)n;
        if (total > expectedSize) {
            free(buffer); close(fd);
            if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档文件在读取时发生变化");
            return nil;
        }
        CC_SHA256_Update(&ctx, buffer, (CC_LONG)n);
    }
    struct stat after;
    int statResult = fstat(fd, &after);
    free(buffer); close(fd);
    if (statResult || total != expectedSize || before.st_size != after.st_size ||
        before.st_mtimespec.tv_sec != after.st_mtimespec.tv_sec ||
        before.st_mtimespec.tv_nsec != after.st_mtimespec.tv_nsec) {
        if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档文件在读取时发生变化");
        return nil;
    }
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    CC_SHA256_Final(digest, &ctx);
    return DMHex(digest, sizeof digest);
}

static NSArray<NSString *> *DMSortedNames(NSString *directory, NSError **error) {
    NSError *local = nil;
    NSArray<NSString *> *names = [[NSFileManager defaultManager] contentsOfDirectoryAtPath:directory error:&local];
    if (!names) {
        if (error) *error = DMError(DMMacCloudErrorFilesystem, @"无法列举云存档目录");
        return nil;
    }
    return [names sortedArrayUsingComparator:^NSComparisonResult(NSString *a, NSString *b) {
        int result = DMUTF16Compare(a, b);
        return result < 0 ? NSOrderedAscending : result > 0 ? NSOrderedDescending : NSOrderedSame;
    }];
}

static BOOL DMHasGuestNameXattr(NSString *path, NSError **error) {
    const char *native = path.fileSystemRepresentation;
    ssize_t a = getxattr(native, DMMacCloudGuestNameKey.UTF8String, NULL, 0, 0, XATTR_NOFOLLOW);
    if (a >= 0) {
        if (error) *error = DMError(DMMacCloudErrorUnsupported, @"云存档包含 guest-name 路径映射");
        return YES;
    }
    if (errno != ENOATTR && errno != ENOTSUP) {
        if (error) *error = DMError(DMMacCloudErrorFilesystem, @"无法检查云存档扩展属性");
        return YES;
    }
    ssize_t b = getxattr(native, DMMacCloudUserGuestNameKey.UTF8String, NULL, 0, 0, XATTR_NOFOLLOW);
    if (b >= 0) {
        if (error) *error = DMError(DMMacCloudErrorUnsupported, @"云存档包含 guest-name 路径映射");
        return YES;
    }
    if (errno != ENOATTR && errno != ENOTSUP) {
        if (error) *error = DMError(DMMacCloudErrorFilesystem, @"无法检查云存档扩展属性");
        return YES;
    }
    return NO;
}

static BOOL DMPreflightTree(NSString *directory, NSString *relative, NSMutableDictionary<NSString *, NSString *> *aliases,
                            NSError **error) {
    NSArray<NSString *> *names = DMSortedNames(directory, error);
    if (!names) return NO;
    for (NSString *name in names) {
        NSString *childRelative = relative.length ? [relative stringByAppendingPathComponent:name] : name;
        NSString *child = [directory stringByAppendingPathComponent:name];
        const char *native = child.fileSystemRepresentation;
        if (!native || DMHasGuestNameXattr(child, error)) return NO;
        if ([name hasPrefix:@".dm-case-"]) {
            if (error) *error = DMError(DMMacCloudErrorUnsupported, @"云存档包含 .dm-case-* 路径别名");
            return NO;
        }
        struct stat st;
        if (lstat(native, &st)) {
            if (error) *error = DMError(DMMacCloudErrorFilesystem, @"无法检查云存档目录项");
            return NO;
        }
        if (S_ISLNK(st.st_mode) || (!S_ISREG(st.st_mode) && !S_ISDIR(st.st_mode))) {
            if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档包含符号链接或特殊文件");
            return NO;
        }
        NSArray<NSString *> *parts = [childRelative componentsSeparatedByString:@"/"];
        NSMutableString *raw = [NSMutableString string];
        NSMutableString *alias = [NSMutableString string];
        for (NSUInteger i = 0; i < parts.count; i++) {
            if (i) { [raw appendString:@"/"]; [alias appendString:@"/"]; }
            [raw appendString:parts[i]];
            [alias appendString:DMFoldAliasComponent(parts[i])];
            NSString *previous = aliases[alias];
            if (previous && ![previous isEqualToString:raw]) {
                if (error) *error = DMError(DMMacCloudErrorUnsupported,
                    @"本地存档包含 APFS 大小写或 Unicode 正规化别名冲突");
                return NO;
            }
            aliases[[alias copy]] = [raw copy];
        }
        if (S_ISDIR(st.st_mode) && !DMPreflightTree(child, childRelative, aliases, error)) return NO;
    }
    return YES;
}

static BOOL DMScanDirectory(NSString *root, NSString *relative, NSMutableArray<DMMacCloudEntry *> *files,
                            unsigned long long *total, NSError **error) {
    NSString *directory = relative.length ? [root stringByAppendingPathComponent:relative] : root;
    NSArray<NSString *> *names = DMSortedNames(directory, error);
    if (!names) return NO;
    for (NSString *name in names) {
        NSString *childRelative = relative.length ? [relative stringByAppendingPathComponent:name] : name;
        NSString *child = [root stringByAppendingPathComponent:childRelative];
        struct stat st;
        if (lstat(child.fileSystemRepresentation, &st)) {
            if (error) *error = DMError(DMMacCloudErrorFilesystem, @"无法检查本地快照文件");
            return NO;
        }
        if (S_ISLNK(st.st_mode) || (!S_ISREG(st.st_mode) && !S_ISDIR(st.st_mode))) {
            if (error) *error = DMError(DMMacCloudErrorProtocol, @"本地快照包含符号链接或特殊文件");
            return NO;
        }
        if (DMIsExcludedComponent(name) || (!relative.length && [name isEqualToString:@"Settings"])) continue;
        if (!DMValidateRelativePath(childRelative, NO, error)) return NO;
        if (S_ISDIR(st.st_mode)) {
            if (!DMScanDirectory(root, childRelative, files, total, error)) return NO;
            continue;
        }
        unsigned long long size = (unsigned long long)st.st_size;
        if (st.st_size < 0 || size > DMMacCloudMaxFileBytes || files.count >= DMMacCloudMaxFiles) {
            if (error) *error = DMError(DMMacCloudErrorProtocol, @"本地快照超过文件数或单文件大小限制");
            return NO;
        }
        *total += size;
        if (*total > DMMacCloudMaxTotalBytes) {
            if (error) *error = DMError(DMMacCloudErrorProtocol, @"本地快照超过 24 MiB 上限");
            return NO;
        }
        NSString *sha = DMSHA256File(child, size, error);
        if (!sha) return NO;
        [files addObject:[[DMMacCloudEntry alloc] initWithPath:childRelative size:size sha256:sha]];
    }
    return YES;
}

static DMMacCloudLocalSnapshot *DMScanLocalSnapshot(NSString *root, NSError **error) {
    struct stat st;
    if (!root.length || lstat(root.fileSystemRepresentation, &st) || !S_ISDIR(st.st_mode)) {
        if (error) *error = DMError(DMMacCloudErrorFilesystem, @"本地快照不是普通目录");
        return nil;
    }
    NSMutableDictionary<NSString *, NSString *> *aliases = [NSMutableDictionary dictionary];
    if (!DMPreflightTree(root, @"", aliases, error)) return nil;
    NSMutableArray<DMMacCloudEntry *> *files = [NSMutableArray array];
    unsigned long long total = 0;
    if (!DMScanDirectory(root, @"", files, &total, error)) return nil;
    [files sortUsingComparator:^NSComparisonResult(DMMacCloudEntry *a, DMMacCloudEntry *b) {
        int result = DMUTF16Compare(a.path, b.path);
        return result < 0 ? NSOrderedAscending : result > 0 ? NSOrderedDescending : NSOrderedSame;
    }];
    if (!DMValidatePathSet(files, NO, error)) return nil;
    DMMacCloudLocalSnapshot *snapshot = [DMMacCloudLocalSnapshot new];
    snapshot.root = root;
    snapshot.files = files;
    snapshot.snapshotSHA256 = DMSnapshotDigest(files);
    snapshot.totalBytes = total;
    if (!snapshot.snapshotSHA256) {
        if (error) *error = DMError(DMMacCloudErrorFilesystem, @"无法计算云存档摘要");
        return nil;
    }
    return snapshot;
}

static BOOL DMNormalizeEndpoint(NSString *raw, NSString **out, NSError **error) {
    NSString *endpoint = [raw stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
    while ([endpoint hasSuffix:@"/"]) endpoint = [endpoint substringToIndex:endpoint.length - 1];
    if (!endpoint.length || DMHasISOControl(endpoint) || [endpoint rangeOfCharacterFromSet:NSCharacterSet.whitespaceAndNewlineCharacterSet].location != NSNotFound) {
        if (error) *error = DMError(DMMacCloudErrorConfig, @"云存档地址为空或包含非法字符");
        return NO;
    }
    NSURLComponents *components = [NSURLComponents componentsWithString:endpoint];
    BOOL https = [components.scheme.lowercaseString isEqualToString:@"https"];
#ifdef DM_MAC_CLOUD_TEST_ALLOW_HTTP
    BOOL loopback = [components.scheme.lowercaseString isEqualToString:@"http"] &&
        [components.host isEqualToString:@"127.0.0.1"];
    BOOL secure = https || loopback;
#else
    BOOL secure = https;
#endif
    if (!secure || !components.host.length || components.user.length || components.password.length ||
        components.query.length || components.fragment.length) {
        if (error) *error = DMError(DMMacCloudErrorConfig, @"云存档地址必须是 HTTPS 基础地址");
        return NO;
    }
    if (out) *out = endpoint;
    return YES;
}

static BOOL DMNormalizeToken(NSString *raw, NSString **out, NSError **error) {
    NSString *token = [raw stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
    if (!token.length || token.length > 4096 || DMHasISOControl(token) ||
        [token rangeOfCharacterFromSet:NSCharacterSet.whitespaceAndNewlineCharacterSet].location != NSNotFound) {
        if (error) *error = DMError(DMMacCloudErrorConfig, @"云存档访问令牌为空或格式无效");
        return NO;
    }
    if (out) *out = token;
    return YES;
}

static DMMacCloudConfig *DMLoadConfig(NSString *path, NSError **error) {
    struct stat st;
    if (!path.length || lstat(path.fileSystemRepresentation, &st) || !S_ISREG(st.st_mode) || S_ISLNK(st.st_mode)) {
        if (error) *error = DMError(DMMacCloudErrorConfig, @"云存档配置不存在或不是普通文件");
        return nil;
    }
    if (st.st_size < 0 || (unsigned long long)st.st_size > DMMacCloudMaxConfigBytes) {
        if (error) *error = DMError(DMMacCloudErrorConfig, @"云存档配置超过大小上限");
        return nil;
    }
    NSData *data = [NSData dataWithContentsOfFile:path options:NSDataReadingMappedIfSafe error:nil];
    if (!data || data.length > DMMacCloudMaxConfigBytes) {
        if (error) *error = DMError(DMMacCloudErrorConfig, @"无法读取云存档配置");
        return nil;
    }
    id root = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
    if (![root isKindOfClass:NSDictionary.class]) {
        if (error) *error = DMError(DMMacCloudErrorConfig, @"云存档配置不是 JSON 对象");
        return nil;
    }
    id endpointValue = ((NSDictionary *)root)[@"endpoint"];
    id tokenValue = ((NSDictionary *)root)[@"token"];
    if (![endpointValue isKindOfClass:NSString.class] || ![tokenValue isKindOfClass:NSString.class]) {
        if (error) *error = DMError(DMMacCloudErrorConfig, @"云存档配置缺少 endpoint 或 token");
        return nil;
    }
    NSString *endpoint = nil, *token = nil;
    if (!DMNormalizeEndpoint(endpointValue, &endpoint, error) || !DMNormalizeToken(tokenValue, &token, error)) return nil;
    DMMacCloudConfig *config = [DMMacCloudConfig new];
    config.endpoint = endpoint;
    config.token = token;
    return config;
}

static BOOL DMConfigEqual(DMMacCloudConfig *a, DMMacCloudConfig *b) {
    return a && b && [a.endpoint isEqualToString:b.endpoint] && [a.token isEqualToString:b.token];
}

static NSString *DMConfigScope(DMMacCloudConfig *config) {
    NSMutableData *data = [[config.endpoint dataUsingEncoding:NSUTF8StringEncoding] mutableCopy];
    const unsigned char zero = 0;
    [data appendBytes:&zero length:1];
    [data appendData:[config.token dataUsingEncoding:NSUTF8StringEncoding]];
    NSString *scope = DMSHA256Data(data);
    memset(data.mutableBytes, 0, data.length);
    return scope;
}

static NSString *DMCloudRoot(NSString *root, NSError **error) {
    struct stat st;
    if (!root.length || lstat(root.fileSystemRepresentation, &st) || !S_ISDIR(st.st_mode) || S_ISLNK(st.st_mode)) {
        if (error) *error = DMError(DMMacCloudErrorFilesystem, @"云存档 runtime 目录无效");
        return nil;
    }
    NSString *cloudRoot = [root stringByAppendingPathComponent:@"cloud-save"];
    if (lstat(cloudRoot.fileSystemRepresentation, &st)) {
        if (errno != ENOENT || mkdir(cloudRoot.fileSystemRepresentation, 0700)) {
            if (error) *error = DMError(DMMacCloudErrorFilesystem, @"无法创建云存档私有目录");
            return nil;
        }
    } else if (!S_ISDIR(st.st_mode) || S_ISLNK(st.st_mode)) {
        if (error) *error = DMError(DMMacCloudErrorFilesystem, @"云存档私有路径不是普通目录");
        return nil;
    }
    chmod(cloudRoot.fileSystemRepresentation, 0700);
    return cloudRoot;
}

static NSString *DMUniqueChild(NSString *parent, NSString *prefix, NSError **error) {
    NSString *stamp = [NSString stringWithFormat:@"%.0f-%d", [NSDate timeIntervalSinceReferenceDate] * 1000000.0, getpid()];
    for (NSUInteger i = 0; i < 1000; i++) {
        NSString *candidate = [parent stringByAppendingPathComponent:
            [NSString stringWithFormat:@"%@%@-%lu", prefix, stamp, (unsigned long)i]];
        if (lstat(candidate.fileSystemRepresentation, &(struct stat){0}) && errno == ENOENT) return candidate;
    }
    if (error) *error = DMError(DMMacCloudErrorFilesystem, @"无法分配云存档临时路径");
    return nil;
}

static BOOL DMRemoveTree(NSString *path, NSError **error) {
    struct stat st;
    if (lstat(path.fileSystemRepresentation, &st)) return errno == ENOENT;
    if (S_ISLNK(st.st_mode)) {
        if (unlink(path.fileSystemRepresentation)) {
            if (error) *error = DMError(DMMacCloudErrorFilesystem, @"无法清理云存档临时文件");
            return NO;
        }
        return YES;
    }
    if (!S_ISDIR(st.st_mode)) {
        if (unlink(path.fileSystemRepresentation)) {
            if (error) *error = DMError(DMMacCloudErrorFilesystem, @"无法清理云存档临时文件");
            return NO;
        }
        return YES;
    }
    NSArray<NSString *> *names = DMSortedNames(path, error);
    if (!names) return NO;
    for (NSString *name in names) if (!DMRemoveTree([path stringByAppendingPathComponent:name], error)) return NO;
    if (rmdir(path.fileSystemRepresentation)) {
        if (error) *error = DMError(DMMacCloudErrorFilesystem, @"无法清理云存档临时目录");
        return NO;
    }
    return YES;
}

static BOOL DMWriteAll(int fd, const void *bytes, NSUInteger length) {
    const unsigned char *p = bytes;
    NSUInteger done = 0;
    while (done < length) {
        ssize_t n = write(fd, p + done, length - done);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return NO;
        done += (NSUInteger)n;
    }
    return YES;
}

static BOOL DMFsyncDirectory(NSString *path) {
    int fd = open(path.fileSystemRepresentation, O_RDONLY | O_CLOEXEC | O_DIRECTORY);
    if (fd < 0) return NO;
    BOOL ok = fsync(fd) == 0;
    close(fd);
    return ok;
}

static BOOL DMAtomicWritePrivate(NSString *path, NSData *data, NSError **error) {
    NSString *parent = path.stringByDeletingLastPathComponent;
    NSString *temporary = [parent stringByAppendingPathComponent:
        [NSString stringWithFormat:@".%@.tmp-%d-%llu", path.lastPathComponent, getpid(), (unsigned long long)NSDate.date.timeIntervalSince1970]];
    int fd = open(temporary.fileSystemRepresentation, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0) {
        if (error) *error = DMError(DMMacCloudErrorFilesystem, @"无法创建云存档基准临时文件");
        return NO;
    }
    BOOL ok = DMWriteAll(fd, data.bytes, data.length) && fsync(fd) == 0;
    close(fd);
    if (!ok || rename(temporary.fileSystemRepresentation, path.fileSystemRepresentation) || chmod(path.fileSystemRepresentation, 0600)) {
        unlink(temporary.fileSystemRepresentation);
        if (error) *error = DMError(DMMacCloudErrorFilesystem, @"无法原子保存云存档基准");
        return NO;
    }
    if (!DMFsyncDirectory(parent)) {
        if (error) *error = DMError(DMMacCloudErrorFilesystem, @"无法持久化云存档基准目录");
        return NO;
    }
    return YES;
}

static NSString *DMBaselinePath(NSString *cloudRoot, NSString *scope) {
    return [cloudRoot stringByAppendingPathComponent:[NSString stringWithFormat:@"baseline-%@.json", scope]];
}

static DMMacCloudBaseline *DMLoadBaseline(NSString *path, NSString *scope) {
    NSData *data = [NSData dataWithContentsOfFile:path options:NSDataReadingMappedIfSafe error:nil];
    if (!data || data.length > DMMacCloudMaxConfigBytes) return nil;
    id root = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
    if (![root isKindOfClass:NSDictionary.class]) return nil;
    NSDictionary *json = root;
    if (![json[@"scope"] isKindOfClass:NSString.class] || ![json[@"scope"] isEqualToString:scope] ||
        ![json[@"revision"] isKindOfClass:NSString.class] || ![json[@"snapshot_sha256"] isKindOfClass:NSString.class]) return nil;
    NSString *revision = json[@"revision"], *hash = json[@"snapshot_sha256"];
    if ((revision.length == 0 && hash.length != 0) || (revision.length && !DMIsSHA256(hash))) return nil;
    DMMacCloudBaseline *baseline = [DMMacCloudBaseline new];
    baseline.scope = scope; baseline.revision = revision; baseline.snapshotSHA256 = hash;
    return baseline;
}

static BOOL DMSaveBaseline(NSString *path, NSString *scope, DMMacCloudState *state, NSError **error) {
    NSDictionary *json = @{@"schema_version": @1, @"scope": scope,
        @"revision": state.revision ?: @"", @"snapshot_sha256": state.snapshotSHA256 ?: @""};
    NSData *data = [NSJSONSerialization dataWithJSONObject:json options:0 error:nil];
    return data && DMAtomicWritePrivate(path, data, error);
}

static BOOL DMStrictInteger(id value, unsigned long long *out) {
    if (![value isKindOfClass:NSNumber.class] || CFGetTypeID((__bridge CFTypeRef)value) == CFBooleanGetTypeID()) return NO;
    const char *type = [(NSNumber *)value objCType];
    if (!type || strchr("cCsSiIlLqQ", type[0]) == NULL) return NO;
    long long signedValue = [(NSNumber *)value longLongValue];
    if (signedValue < 0) return NO;
    if (out) *out = (unsigned long long)signedValue;
    return YES;
}

static DMMacCloudState *DMParseState(NSDictionary *json, NSError **error) {
    id revisionValue = json[@"revision"];
    id hashValue = json[@"snapshot_sha256"];
    id updatedValue = json[@"updated_at"];
    id deviceValue = json[@"device"];
    id countValue = json[@"file_count"];
    id totalValue = json[@"total_bytes"];
    unsigned long long count = 0, total = 0;
    if (![revisionValue isKindOfClass:NSString.class] || ![hashValue isKindOfClass:NSString.class] ||
        ![updatedValue isKindOfClass:NSString.class] || ![deviceValue isKindOfClass:NSString.class] ||
        !DMStrictInteger(countValue, &count) || !DMStrictInteger(totalValue, &total) ||
        count > DMMacCloudMaxFiles || total > DMMacCloudMaxTotalBytes ||
        (count == 0 && total != 0)) {
        if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档 state 字段无效");
        return nil;
    }
    NSString *revision = revisionValue, *hash = hashValue;
    if ((revision.length == 0 && hash.length != 0) || (revision.length && !DMIsSHA256(hash))) {
        if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档 state 的 revision 与摘要不一致");
        return nil;
    }
    DMMacCloudState *state = [DMMacCloudState new];
    state.revision = revision; state.snapshotSHA256 = hash; state.updatedAt = updatedValue;
    state.device = deviceValue; state.fileCount = (NSUInteger)count; state.totalBytes = total;
    return state;
}

static NSDictionary *DMParseJSONObject(NSData *data, NSError **error) {
    if (!data.length) {
        if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档服务返回空响应");
        return nil;
    }
    id root = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
    if (![root isKindOfClass:NSDictionary.class]) {
        if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档服务返回了无效 JSON");
        return nil;
    }
    return root;
}

static NSString *DMPercentEncodeRevision(NSString *value) {
    NSData *data = [value dataUsingEncoding:NSUTF8StringEncoding];
    NSMutableString *out = [NSMutableString string];
    const unsigned char *p = data.bytes;
    for (NSUInteger i = 0; i < data.length; i++) {
        unsigned char c = p[i];
        if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
            c == '-' || c == '.' || c == '_' || c == '~') [out appendFormat:@"%c", c];
        else [out appendFormat:@"%%%02X", c];
    }
    return out;
}

static DMMacCloudHTTPResult *DMHTTPRequest(DMMacCloudConfig *config, NSString *method,
                                           NSString *suffix, NSData *body, NSUInteger limit) {
    DMMacCloudHTTPResult *result = [DMMacCloudHTTPResult new];
    NSString *urlText = [config.endpoint stringByAppendingString:suffix];
    NSURL *url = [NSURL URLWithString:urlText];
    if (!url) {
        result.error = DMError(DMMacCloudErrorConfig, @"云存档 URL 无效");
        return result;
    }
    NSMutableURLRequest *request = [NSMutableURLRequest requestWithURL:url
        cachePolicy:NSURLRequestReloadIgnoringLocalCacheData timeoutInterval:45.0];
    request.HTTPMethod = method;
    [request setValue:[@"Bearer " stringByAppendingString:config.token] forHTTPHeaderField:@"Authorization"];
    [request setValue:@"application/json" forHTTPHeaderField:@"Accept"];
    [request setValue:@"D2RMobile-macOS" forHTTPHeaderField:@"User-Agent"];
    [request setValue:@"no-store" forHTTPHeaderField:@"Cache-Control"];
    [request setValue:@"identity" forHTTPHeaderField:@"Accept-Encoding"];
    if (body) {
        request.HTTPBody = body;
        [request setValue:@"application/json; charset=utf-8" forHTTPHeaderField:@"Content-Type"];
    }
    NSURLSessionConfiguration *configuration = NSURLSessionConfiguration.ephemeralSessionConfiguration;
    configuration.timeoutIntervalForRequest = 15.0;
    configuration.timeoutIntervalForResource = 45.0;
    configuration.HTTPShouldSetCookies = NO;
    configuration.HTTPCookieStorage = nil;
    configuration.URLCache = nil;
    NSOperationQueue *delegateQueue = [NSOperationQueue new];
    delegateQueue.name = @"D2RMobile.MacCloud.HTTP";
    delegateQueue.maxConcurrentOperationCount = 1;
    DMMacCloudTransfer *transfer = [[DMMacCloudTransfer alloc] initWithLimit:limit ?: DMMacCloudMaxHTTPBytes];
    NSURLSession *session = [NSURLSession sessionWithConfiguration:configuration delegate:transfer delegateQueue:delegateQueue];
    NSURLSessionDataTask *task = [session dataTaskWithRequest:request];
    [task resume];
    dispatch_time_t deadline = dispatch_time(DISPATCH_TIME_NOW, (int64_t)(50.0 * NSEC_PER_SEC));
    long waited = dispatch_semaphore_wait(transfer.done, deadline);
    if (waited != 0) {
        [task cancel];
        result.error = DMError(DMMacCloudErrorHTTP, @"云存档网络请求超时");
    } else if (transfer.redirected || (transfer.status >= 300 && transfer.status < 400)) {
        result.error = DMError(DMMacCloudErrorHTTP, @"云存档服务返回重定向，已拒绝携带认证继续");
    } else if (transfer.error) {
        result.status = transfer.status;
        result.error = transfer.error;
    } else {
        result.status = transfer.status;
        result.data = [transfer.data copy];
    }
    [session invalidateAndCancel];
    return result;
}

static DMMacCloudState *DMHTTPState(DMMacCloudConfig *config, NSError **error) {
    DMMacCloudHTTPResult *response = DMHTTPRequest(config, @"GET", @"/state", nil, DMMacCloudMaxConfigBytes);
    if (response.error) {
        if (error) *error = response.error;
        return nil;
    }
    if (response.status == 401) {
        if (error) *error = DMError(DMMacCloudErrorAuthentication, @"云存档访问令牌无效或已失效");
        return nil;
    }
    if (response.status != 200) {
        if (error) *error = DMError(DMMacCloudErrorHTTP, [NSString stringWithFormat:@"云存档服务返回 HTTP %ld", (long)response.status]);
        return nil;
    }
    NSDictionary *json = DMParseJSONObject(response.data, error);
    return json ? DMParseState(json, error) : nil;
}

static BOOL DMValidateRemoteSnapshot(NSDictionary *json, DMMacCloudState *expected,
                                     DMMacCloudRemoteSnapshot **result, NSError **error) {
    DMMacCloudState *state = DMParseState(json, error);
    if (!state) return NO;
    if (![state.revision isEqualToString:expected.revision] ||
        ![state.snapshotSHA256 isEqualToString:expected.snapshotSHA256] ||
        state.fileCount != expected.fileCount || state.totalBytes != expected.totalBytes) {
        if (error) *error = DMConflictError(@"云存档 snapshot 响应与已确认 state 不一致", state);
        return NO;
    }
    id rowsValue = json[@"files"];
    if (![rowsValue isKindOfClass:NSArray.class] || [(NSArray *)rowsValue count] != state.fileCount) {
        if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档 files 数量与 state 不一致");
        return NO;
    }
    NSMutableArray<DMMacCloudEntry *> *files = [NSMutableArray array];
    unsigned long long total = 0;
    NSString *previous = nil;
    for (id rowValue in (NSArray *)rowsValue) {
        if (![rowValue isKindOfClass:NSDictionary.class]) {
            if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档文件项无效");
            return NO;
        }
        NSDictionary *row = rowValue;
        id pathValue = row[@"path"];
        id sizeValue = row[@"size"];
        id shaValue = row[@"sha256"];
        id dataValue = row[@"data"];
        unsigned long long size = 0;
        if (![pathValue isKindOfClass:NSString.class] || ![shaValue isKindOfClass:NSString.class] ||
            ![dataValue isKindOfClass:NSString.class] || !DMStrictInteger(sizeValue, &size) ||
            size > DMMacCloudMaxFileBytes) {
            if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档文件项字段无效");
            return NO;
        }
        NSString *path = pathValue, *sha = shaValue, *encoded = dataValue;
        if (!DMValidateRelativePath(path, YES, error) || !DMIsSHA256(sha)) return NO;
        if (previous && DMUTF16Compare(previous, path) >= 0) {
            if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档 files 未按 UTF-16 路径顺序排列");
            return NO;
        }
        previous = path;
        NSData *decoded = [[NSData alloc] initWithBase64EncodedString:encoded options:0];
        if (!decoded || ![decoded base64EncodedStringWithOptions:0] || ![[decoded base64EncodedStringWithOptions:0] isEqualToString:encoded] ||
            decoded.length != size || ![DMSHA256Data(decoded) isEqualToString:sha]) {
            if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档文件大小、Base64 或 SHA-256 校验失败");
            return NO;
        }
        total += size;
        if (total > DMMacCloudMaxTotalBytes || files.count >= DMMacCloudMaxFiles) {
            if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档总大小或文件数超过上限");
            return NO;
        }
        DMMacCloudEntry *entry = [[DMMacCloudEntry alloc] initWithPath:path size:size sha256:sha];
        entry.data = decoded;
        [files addObject:entry];
    }
    if (!DMValidatePathSet(files, YES, error)) return NO;
    if (total != state.totalBytes || ![DMSnapshotDigest(files) isEqualToString:state.snapshotSHA256]) {
        if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档 manifest 摘要或总大小不一致");
        return NO;
    }
    DMMacCloudRemoteSnapshot *snapshot = [DMMacCloudRemoteSnapshot new];
    snapshot.state = state;
    snapshot.files = files;
    if (result) *result = snapshot;
    return YES;
}

static DMMacCloudRemoteSnapshot *DMHTTPSnapshot(DMMacCloudConfig *config, DMMacCloudState *expected, NSError **error) {
    NSString *suffix = [@"/snapshot?revision=" stringByAppendingString:DMPercentEncodeRevision(expected.revision)];
    DMMacCloudHTTPResult *response = DMHTTPRequest(config, @"GET", suffix, nil, DMMacCloudMaxHTTPBytes);
    if (response.error) {
        if (error) *error = response.error;
        return nil;
    }
    if (response.status == 401) {
        if (error) *error = DMError(DMMacCloudErrorAuthentication, @"云存档访问令牌无效或已失效");
        return nil;
    }
    if (response.status != 200) {
        if (error) *error = DMError(DMMacCloudErrorHTTP, [NSString stringWithFormat:@"云存档服务返回 HTTP %ld", (long)response.status]);
        return nil;
    }
    NSDictionary *json = DMParseJSONObject(response.data, error);
    DMMacCloudRemoteSnapshot *snapshot = nil;
    return json && DMValidateRemoteSnapshot(json, expected, &snapshot, error) ? snapshot : nil;
}

static BOOL DMAppendJSONString(NSMutableData *out, NSString *value) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:value options:NSJSONWritingFragmentsAllowed error:nil];
    if (!data) return NO;
    [out appendData:data];
    return YES;
}

static NSData *DMUploadBody(DMMacCloudLocalSnapshot *snapshot, NSString *baseRevision, NSError **error) {
    NSMutableData *out = [NSMutableData dataWithCapacity:36U * 1024U * 1024U];
    const char *head = "{\"base_revision\":";
    [out appendBytes:head length:strlen(head)];
    if (!DMAppendJSONString(out, baseRevision)) return nil;
    const char *middle = ",\"device\":\"macOS\",\"snapshot_sha256\":";
    [out appendBytes:middle length:strlen(middle)];
    if (!DMAppendJSONString(out, snapshot.snapshotSHA256)) return nil;
    const char *files = ",\"files\":[";
    [out appendBytes:files length:strlen(files)];
    NSUInteger index = 0;
    for (DMMacCloudEntry *file in snapshot.files) {
        if (index++) [out appendBytes:"," length:1];
        [out appendBytes:"{\"path\":" length:strlen("{\"path\":")];
        if (!DMAppendJSONString(out, file.path)) {
            if (error) *error = DMError(DMMacCloudErrorProtocol, @"无法编码云存档路径");
            return nil;
        }
        NSString *metadata = [NSString stringWithFormat:@",\"size\":%llu,\"sha256\":", file.size];
        [out appendData:[metadata dataUsingEncoding:NSUTF8StringEncoding]];
        if (!DMAppendJSONString(out, file.sha256)) return nil;
        [out appendBytes:"," length:1];
        [out appendBytes:"\"data\":\"" length:strlen("\"data\":\"")];
        NSString *path = [snapshot.root stringByAppendingPathComponent:file.path];
        NSData *data = [NSData dataWithContentsOfFile:path options:NSDataReadingMappedIfSafe error:nil];
        if (!data || data.length != file.size || ![DMSHA256Data(data) isEqualToString:file.sha256]) {
            if (error) *error = DMError(DMMacCloudErrorProtocol, @"本地快照在读取时发生变化");
            return nil;
        }
        [out appendData:[data base64EncodedDataWithOptions:0]];
        [out appendBytes:"\"}" length:strlen("\"}")];
        if (out.length > DMMacCloudMaxHTTPBytes) {
            if (error) *error = DMError(DMMacCloudErrorProtocol, @"上传请求超过 48 MiB 上限");
            return nil;
        }
    }
    [out appendBytes:"]}" length:strlen("]}")];
    return out.length <= DMMacCloudMaxHTTPBytes ? out : nil;
}

static DMMacCloudState *DMHTTPPut(DMMacCloudConfig *config, DMMacCloudLocalSnapshot *snapshot,
                                  NSString *baseRevision, NSError **error) {
    NSData *body = DMUploadBody(snapshot, baseRevision, error);
    if (!body) return nil;
    DMMacCloudHTTPResult *response = DMHTTPRequest(config, @"PUT", @"/snapshot", body, DMMacCloudMaxConfigBytes);
    if (response.error) {
        if (error) *error = response.error;
        return nil;
    }
    NSDictionary *json = DMParseJSONObject(response.data, error);
    if (response.status == 401) {
        if (error) *error = DMError(DMMacCloudErrorAuthentication, @"云存档访问令牌无效或已失效");
        return nil;
    }
    if (response.status == 409) {
        id stateValue = json[@"state"];
        DMMacCloudState *state = [stateValue isKindOfClass:NSDictionary.class] ? DMParseState(stateValue, error) : nil;
        if (state && error) *error = DMConflictError(@"上传前云端已变化，请重新选择覆盖方向", state);
        return nil;
    }
    if (response.status != 200) {
        if (error) *error = DMError(response.status == 413 ? DMMacCloudErrorProtocol : DMMacCloudErrorHTTP,
            [NSString stringWithFormat:@"云存档服务返回 HTTP %ld", (long)response.status]);
        return nil;
    }
    DMMacCloudState *state = json ? DMParseState(json, error) : nil;
    if (state && ![DMNormalizedRemoteHash(state.snapshotSHA256) isEqualToString:DMNormalizedRemoteHash(snapshot.snapshotSHA256)]) {
        if (error) *error = DMError(DMMacCloudErrorProtocol, @"云存档上传后返回的摘要不一致");
        return nil;
    }
    return state;
}

static BOOL DMEnsureDirectory(NSString *path, NSError **error) {
    struct stat st;
    if (!lstat(path.fileSystemRepresentation, &st)) {
        if (S_ISDIR(st.st_mode) && !S_ISLNK(st.st_mode)) return YES;
        if (error) *error = DMError(DMMacCloudErrorUnsupported, @"云存档恢复路径与普通目录冲突");
        return NO;
    }
    if (errno != ENOENT || mkdir(path.fileSystemRepresentation, 0700)) {
        if (error) *error = DMError(DMMacCloudErrorFilesystem, @"无法创建云存档恢复目录");
        return NO;
    }
    return YES;
}

static BOOL DMWriteRemoteTree(NSString *stage, NSArray<DMMacCloudEntry *> *files, NSError **error) {
    struct stat st;
    if (!lstat(stage.fileSystemRepresentation, &st)) {
        if (error) *error = DMError(DMMacCloudErrorFilesystem, @"云存档恢复目录已存在");
        return NO;
    }
    if (errno != ENOENT || mkdir(stage.fileSystemRepresentation, 0700)) {
        if (error) *error = DMError(DMMacCloudErrorFilesystem, @"无法创建云存档恢复目录");
        return NO;
    }
    for (DMMacCloudEntry *file in files) {
        NSArray<NSString *> *parts = [file.path componentsSeparatedByString:@"/"];
        NSString *directory = stage;
        for (NSUInteger i = 0; i + 1 < parts.count; i++) {
            directory = [directory stringByAppendingPathComponent:parts[i]];
            if (!DMEnsureDirectory(directory, error)) return NO;
        }
        NSString *destination = [stage stringByAppendingPathComponent:file.path];
        int fd = open(destination.fileSystemRepresentation, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
        if (fd < 0) {
            if (error) *error = DMError(DMMacCloudErrorUnsupported,
                @"云存档恢复路径发生 APFS 大小写、正规化或文件目录冲突");
            return NO;
        }
        BOOL ok = DMWriteAll(fd, file.data.bytes, file.data.length) && fsync(fd) == 0;
        close(fd);
        if (!ok) {
            if (error) *error = DMError(DMMacCloudErrorFilesystem, @"无法写入云存档恢复文件");
            return NO;
        }
    }
    return YES;
}

static BOOL DMCopyExcluded(NSString *source, NSString *stage, NSString *relative, BOOL topLevel, NSError **error) {
    struct stat sourceStat;
    if (lstat(source.fileSystemRepresentation, &sourceStat)) {
        if (errno == ENOENT) return YES;
        if (error) *error = DMError(DMMacCloudErrorFilesystem, @"无法检查本地存档排除项");
        return NO;
    }
    if (!S_ISDIR(sourceStat.st_mode) || S_ISLNK(sourceStat.st_mode)) {
        if (error) *error = DMError(DMMacCloudErrorUnsupported, @"本地 save 不是可安全读取的普通目录");
        return NO;
    }
    NSString *sourceDirectory = relative.length ? [source stringByAppendingPathComponent:relative] : source;
    NSArray<NSString *> *names = DMSortedNames(sourceDirectory, error);
    if (!names) return NO;
    for (NSString *name in names) {
        NSString *childRelative = relative.length ? [relative stringByAppendingPathComponent:name] : name;
        NSString *child = [source stringByAppendingPathComponent:childRelative];
        NSString *destination = [stage stringByAppendingPathComponent:childRelative];
        struct stat st;
        if (lstat(child.fileSystemRepresentation, &st)) {
            if (error) *error = DMError(DMMacCloudErrorFilesystem, @"无法检查需保留的本地条目");
            return NO;
        }
        if (S_ISLNK(st.st_mode) || (!S_ISREG(st.st_mode) && !S_ISDIR(st.st_mode))) {
            if (error) *error = DMError(DMMacCloudErrorUnsupported, @"需保留的本地条目包含符号链接或特殊文件");
            return NO;
        }
        BOOL excluded = DMIsExcludedComponent(name) || (topLevel && [name isEqualToString:@"Settings"]);
        if (excluded) {
            NSString *parent = destination.stringByDeletingLastPathComponent;
            NSString *parentRelative = parent.length > stage.length ? [parent substringFromIndex:stage.length + 1] : @"";
            NSString *current = stage;
            for (NSString *component in [parentRelative componentsSeparatedByString:@"/"]) {
                if (!component.length) continue;
                current = [current stringByAppendingPathComponent:component];
                if (!DMEnsureDirectory(current, error)) return NO;
            }
            struct stat existing;
            if (!lstat(destination.fileSystemRepresentation, &existing) || errno != ENOENT) {
                if (error) *error = DMError(DMMacCloudErrorUnsupported, @"云端文件与需保留的本地条目发生路径冲突");
                return NO;
            }
            if (clonefile(child.fileSystemRepresentation, destination.fileSystemRepresentation, CLONE_NOFOLLOW)) {
                if (error) *error = DMError(DMMacCloudErrorUnsupported, @"无法无损复制需保留的本地条目");
                return NO;
            }
            continue;
        }
        if (S_ISDIR(st.st_mode) && !DMCopyExcluded(source, stage, childRelative, NO, error)) return NO;
    }
    return YES;
}

static BOOL DMVerifyRemoteTree(NSString *stage, NSArray<DMMacCloudEntry *> *files, NSError **error) {
    for (DMMacCloudEntry *file in files) {
        NSString *path = [stage stringByAppendingPathComponent:file.path];
        struct stat st;
        if (lstat(path.fileSystemRepresentation, &st) || !S_ISREG(st.st_mode) || S_ISLNK(st.st_mode) ||
            (unsigned long long)st.st_size != file.size ||
            ![DMSHA256File(path, file.size, error) isEqualToString:file.sha256]) {
            if (error && !*error) *error = DMError(DMMacCloudErrorFilesystem, @"云存档恢复文件校验失败");
            return NO;
        }
    }
    return YES;
}

static BOOL DMInstallRemote(DMMacCloudConfig *config, NSString *root, NSString *cloudRoot, NSString *baselinePath,
                            NSString *scope, DMMacCloudState *expected, NSString *shownLocalHash,
                            DMMacCloudState **installedState, NSError **error) {
    DMMacCloudRemoteSnapshot *remote = DMHTTPSnapshot(config, expected, error);
    if (!remote) return NO;
    NSString *stage = DMUniqueChild(cloudRoot, @"restore-", error);
    if (!stage) return NO;
    DMMacCloudState *latest = nil;
    NSString *freshPath = nil;
    DMMacCloudLocalSnapshot *fresh = nil;
    BOOL enteredInstall = NO;
    BOOL ok = NO;
    if (!DMWriteRemoteTree(stage, remote.files, error)) goto done;
    if (!DMCopyExcluded([root stringByAppendingPathComponent:@"save"], stage, @"", YES, error)) goto done;
    if (!DMVerifyRemoteTree(stage, remote.files, error)) goto done;
    latest = DMHTTPState(config, error);
    if (!latest) goto done;
    if (![latest.revision isEqualToString:expected.revision] ||
        ![(latest.snapshotSHA256 ?: @"") isEqualToString:(expected.snapshotSHA256 ?: @"")]) {
        if (error) *error = DMConflictError(@"恢复期间云端再次变化，请重新选择覆盖方向", latest);
        goto done;
    }
    freshPath = DMUniqueChild(cloudRoot, @"verify-local-", error);
    if (!freshPath) goto done;
    int snapshotResult = dm_fs_cloud_snapshot(root.fileSystemRepresentation, freshPath.fileSystemRepresentation);
    fresh = snapshotResult == 0 ? DMScanLocalSnapshot(freshPath, error) : nil;
    if (!fresh) {
        if (error && !*error) *error = DMError(DMMacCloudErrorFilesystem, @"无法再次校验本地存档");
        DMRemoveTree(freshPath, NULL);
        goto done;
    }
    BOOL localUnchanged = [fresh.snapshotSHA256 isEqualToString:shownLocalHash];
    DMRemoveTree(freshPath, NULL);
    if (!localUnchanged) {
        if (error) *error = DMError(DMMacCloudErrorReevaluate, @"选择后本地存档发生变化，请重新确认");
        goto done;
    }
    enteredInstall = YES;
    int installResult = dm_fs_cloud_install(root.fileSystemRepresentation, stage.fileSystemRepresentation);
    if (installResult) {
        if (error) *error = DMError(DMMacCloudErrorFilesystem,
            [NSString stringWithFormat:@"本地云存档安装失败（errno %d）", installResult < 0 ? -installResult : installResult]);
        goto done;
    }
    if (!DMSaveBaseline(baselinePath, scope, expected, error)) goto done;
    if (installedState) *installedState = expected;
    ok = YES;
done:
    if (freshPath) DMRemoveTree(freshPath, NULL);
    if (!enteredInstall) DMRemoveTree(stage, NULL);
    return ok;
}

typedef NS_ENUM(NSInteger, DMMacCloudDecision) {
    DMMacCloudDecisionAck,
    DMMacCloudDecisionUpload,
    DMMacCloudDecisionDownload,
    DMMacCloudDecisionConflict
};

static DMMacCloudDecision DMDecide(NSString *localHash, DMMacCloudState *remote,
                                   DMMacCloudBaseline *baseline) {
    NSString *remoteHash = DMNormalizedRemoteHash(remote.snapshotSHA256);
    if (baseline && baseline.revision.length && remote.revision.length == 0) return DMMacCloudDecisionConflict;
    if ([DMNormalizedRemoteHash(localHash) isEqualToString:remoteHash]) return DMMacCloudDecisionAck;
    if (!baseline) {
        if (DMIsEmptyHash(localHash)) return DMMacCloudDecisionDownload;
        if (DMIsEmptyHash(remoteHash)) return DMMacCloudDecisionUpload;
        return DMMacCloudDecisionConflict;
    }
    BOOL localChanged = ![DMNormalizedRemoteHash(localHash) isEqualToString:DMNormalizedRemoteHash(baseline.snapshotSHA256)];
    BOOL remoteChanged = ![remoteHash isEqualToString:DMNormalizedRemoteHash(baseline.snapshotSHA256)];
    if (!localChanged) return DMMacCloudDecisionDownload;
    if (!remoteChanged) return DMMacCloudDecisionUpload;
    return DMMacCloudDecisionConflict;
}

#ifdef DM_MAC_CLOUD_TESTING
typedef int (*DMMacCloudTestAlertFunction)(int kind, const char *message, void *context);
static DMMacCloudTestAlertFunction g_testAlert;
static void *g_testAlertContext;
#endif

static NSString *DMCountsMessage(DMMacCloudLocalSnapshot *local, DMMacCloudState *remote) {
    double localMiB = local.totalBytes / 1048576.0, remoteMiB = remote.totalBytes / 1048576.0;
    return [NSString stringWithFormat:
        @"本地：%lu 个文件，%.1f MiB\n云端：%lu 个文件，%.1f MiB，更新于 %@\n\n两套存档都有独立变化。云端覆盖会保留当前本地存档作为恢复版本；本地覆盖会保留云端历史版本。备份不会被自动删除。",
        (unsigned long)local.files.count, localMiB, (unsigned long)remote.fileCount, remoteMiB,
        remote.updatedAt.length ? remote.updatedAt : @"未知时间"];
}

static NSInteger DMShowConflict(DMMacCloudLocalSnapshot *local, DMMacCloudState *remote, NSString *prefix) {
    fprintf(stderr, "[cloud] Startup conflict: local %lu files, cloud %lu files; awaiting explicit choice.\n", (unsigned long)local.files.count, (unsigned long)remote.fileCount);
    NSString *body = DMCountsMessage(local, remote);
    if (prefix.length) body = [prefix stringByAppendingFormat:@"\n\n%@", body];
#ifdef DM_MAC_CLOUD_TESTING
    if (g_testAlert) return g_testAlert(0, body.UTF8String, g_testAlertContext);
#endif
    NSAlert *alert = [NSAlert new];
    alert.messageText = @"检测到云存档冲突";
    alert.informativeText = body;
    [alert addButtonWithTitle:@"云存档覆盖本地"];
    [alert addButtonWithTitle:@"本地存档覆盖云端"];
    [alert addButtonWithTitle:@"暂时离线"];
    NSModalResponse response = [alert runModal];
    return response == NSAlertFirstButtonReturn ? 0 : response == NSAlertSecondButtonReturn ? 1 : 2;
}

static NSInteger DMShowNetworkError(NSString *message) {
    fprintf(stderr, "[cloud] Startup check failed: %s\n", message.UTF8String ?: "unknown error");
    NSString *body = [NSString stringWithFormat:@"%@\n\n请保留本地存档及恢复备份。可以重试，或明确选择暂时离线启动。", message ?: @"云存档服务不可用"];
#ifdef DM_MAC_CLOUD_TESTING
    if (g_testAlert) return g_testAlert(1, body.UTF8String, g_testAlertContext);
#endif
    NSAlert *alert = [NSAlert new];
    alert.messageText = @"无法检查云存档";
    alert.informativeText = body;
    [alert addButtonWithTitle:@"重试"];
    [alert addButtonWithTitle:@"暂时离线"];
    [alert addButtonWithTitle:@"退出游戏"];
    NSModalResponse response = [alert runModal];
    return response == NSAlertFirstButtonReturn ? 0 : response == NSAlertSecondButtonReturn ? 1 : 2;
}

static NSInteger DMShowUnsupported(NSString *message) {
    fprintf(stderr, "[cloud] Local sync unavailable: %s\n", message.UTF8String ?: "unknown error");
    NSString *body = [NSString stringWithFormat:
        @"%@\n\n为避免静默丢失或覆盖现有角色，本次没有读写云存档。可以离线启动；在线同步前需要先解决路径冲突。",
        message ?: @"当前存档包含不支持的路径映射"];
#ifdef DM_MAC_CLOUD_TESTING
    if (g_testAlert) return g_testAlert(2, body.UTF8String, g_testAlertContext);
#endif
    NSAlert *alert = [NSAlert new];
    alert.messageText = @"当前存档暂不支持在线云同步";
    alert.informativeText = body;
    [alert addButtonWithTitle:@"离线启动"];
    [alert addButtonWithTitle:@"退出游戏"];
    return [alert runModal] == NSAlertFirstButtonReturn ? 0 : 1;
}

static DMMacCloudLocalSnapshot *DMTakeLocalSnapshot(NSString *root, NSString *cloudRoot, NSError **error) {
    NSString *target = DMUniqueChild(cloudRoot, @"snapshot-", error);
    if (!target) return nil;
    int result = dm_fs_cloud_snapshot(root.fileSystemRepresentation, target.fileSystemRepresentation);
    if (result) {
        DMRemoveTree(target, NULL);
        if (error) *error = DMError(DMMacCloudErrorFilesystem,
            [NSString stringWithFormat:@"本地存档快照失败（errno %d）", result < 0 ? -result : result]);
        return nil;
    }
    DMMacCloudLocalSnapshot *snapshot = DMScanLocalSnapshot(target, error);
    if (!snapshot) DMRemoveTree(target, NULL);
    return snapshot;
}

@interface DMMacCloudContext : NSObject
@property(nonatomic, copy) NSString *root;
@property(nonatomic, copy) NSString *configPath;
@property(nonatomic, copy) NSString *cloudRoot;
@property(nonatomic, copy) NSString *scope;
@property(nonatomic, copy) NSString *baselinePath;
@property(nonatomic, strong) DMMacCloudConfig *config;
@property(nonatomic, strong) DMMacCloudBaseline *baseline;
@property(nonatomic, strong) dispatch_source_t timer;
@property(atomic) BOOL online;
@property(atomic) BOOL paused;
@property(atomic) BOOL reset;
- (void)performBackgroundSync;
- (void)startTimer;
@end

static DMMacCloudContext *g_context;
static dispatch_queue_t g_worker;
static const void *DMMacCloudQueueKey = &DMMacCloudQueueKey;

@implementation DMMacCloudContext
- (void)startTimer {
    if (self.timer || !self.online || self.reset) return;
    self.timer = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, g_worker);
    uint64_t interval = (uint64_t)(DMMacCloudUploadInterval * NSEC_PER_SEC);
    dispatch_source_set_timer(self.timer, dispatch_time(DISPATCH_TIME_NOW, interval), interval, NSEC_PER_SEC);
    __weak DMMacCloudContext *weakSelf = self;
    dispatch_source_set_event_handler(self.timer, ^{ [weakSelf performBackgroundSync]; });
    dispatch_resume(self.timer);
}

- (void)performBackgroundSync {
    if (self.reset || !self.online || self.paused) return;
    NSError *error = nil;
    DMMacCloudConfig *config = DMLoadConfig(self.configPath, &error);
    if (!config || !DMConfigEqual(config, self.config)) {
        self.paused = YES;
        if (self.timer) { dispatch_source_cancel(self.timer); self.timer = nil; }
        return;
    }
    DMMacCloudBaseline *baseline = DMLoadBaseline(self.baselinePath, self.scope);
    if (!baseline) return;
    DMMacCloudLocalSnapshot *local = DMTakeLocalSnapshot(self.root, self.cloudRoot, &error);
    if (!local) return;
    if ([DMNormalizedRemoteHash(local.snapshotSHA256) isEqualToString:DMNormalizedRemoteHash(baseline.snapshotSHA256)]) {
        DMRemoveTree(local.root, NULL);
        return;
    }
    DMMacCloudState *remote = DMHTTPState(config, &error);
    if (!remote) {
        if (error.code == DMMacCloudErrorAuthentication) self.paused = YES;
        DMRemoveTree(local.root, NULL);
        return;
    }
    if (![remote.revision isEqualToString:baseline.revision] ||
        ![DMNormalizedRemoteHash(remote.snapshotSHA256) isEqualToString:DMNormalizedRemoteHash(baseline.snapshotSHA256)]) {
        self.paused = YES;
        DMRemoveTree(local.root, NULL);
        return;
    }
    if ([DMNormalizedRemoteHash(local.snapshotSHA256) isEqualToString:DMNormalizedRemoteHash(remote.snapshotSHA256)]) {
        NSError *baselineError = nil;
        if (DMSaveBaseline(self.baselinePath, self.scope, remote, &baselineError)) self.baseline = baseline;
        DMRemoveTree(local.root, NULL);
        return;
    }
    DMMacCloudState *uploaded = DMHTTPPut(config, local, baseline.revision, &error);
    DMRemoveTree(local.root, NULL);
    if (!uploaded) {
        if (error.code == DMMacCloudErrorConflict || error.code == DMMacCloudErrorAuthentication) self.paused = YES;
        return;
    }
    NSError *baselineError = nil;
    if (DMSaveBaseline(self.baselinePath, self.scope, uploaded, &baselineError)) {
        DMMacCloudBaseline *next = [DMMacCloudBaseline new];
        next.scope = self.scope; next.revision = uploaded.revision; next.snapshotSHA256 = uploaded.snapshotSHA256;
        self.baseline = next;
    }
}
@end

static dispatch_queue_t DMWorkerQueue(void) {
    static dispatch_once_t once;
    dispatch_once(&once, ^{
        g_worker = dispatch_queue_create("com.d2rmobile.cloud.mac.worker", DISPATCH_QUEUE_SERIAL);
        dispatch_queue_set_specific(g_worker, DMMacCloudQueueKey, (__bridge void *)g_worker, NULL);
    });
    return g_worker;
}

static int DMReconcile(DMMacCloudContext *context) {
    for (;;) {
        NSError *error = nil;
        DMMacCloudLocalSnapshot *local = DMTakeLocalSnapshot(context.root, context.cloudRoot, &error);
        if (!local) {
            NSInteger choice = DMShowUnsupported(error.localizedDescription);
            context.online = NO; context.paused = YES;
            return choice == 0 ? 1 : 0;
        }
        DMMacCloudState *remote = DMHTTPState(context.config, &error);
        if (!remote) {
            DMRemoveTree(local.root, NULL);
            NSInteger choice = DMShowNetworkError(error.localizedDescription);
            if (choice == 0) continue;
            context.online = NO; context.paused = YES;
            return choice == 1 ? 1 : 0;
        }
        DMMacCloudBaseline *baseline = DMLoadBaseline(context.baselinePath, context.scope);
        DMMacCloudDecision decision = DMDecide(local.snapshotSHA256, remote, baseline);
        if (decision == DMMacCloudDecisionConflict) {
            NSInteger choice = DMShowConflict(local, remote, @"云端与本地存档都需要明确选择覆盖方向。时间先后不会作为胜者。");
            if (choice == 2) {
                DMRemoveTree(local.root, NULL);
                context.online = NO; context.paused = YES;
                return 1;
            }
            if (choice == 3) {
                DMRemoveTree(local.root, NULL);
                context.online = NO; context.paused = YES;
                return 0;
            }
            NSError *freshError = nil;
            DMMacCloudLocalSnapshot *fresh = DMTakeLocalSnapshot(context.root, context.cloudRoot, &freshError);
            DMMacCloudState *latest = fresh ? DMHTTPState(context.config, &freshError) : nil;
            if (!fresh || !latest) {
                DMRemoveTree(local.root, NULL); DMRemoveTree(fresh.root, NULL);
                NSInteger networkChoice = DMShowNetworkError(freshError.localizedDescription);
                if (networkChoice == 0) continue;
                context.online = NO; context.paused = YES;
                return networkChoice == 1 ? 1 : 0;
            }
            BOOL unchanged = [fresh.snapshotSHA256 isEqualToString:local.snapshotSHA256] &&
                [latest.revision isEqualToString:remote.revision] &&
                [DMNormalizedRemoteHash(latest.snapshotSHA256) isEqualToString:DMNormalizedRemoteHash(remote.snapshotSHA256)];
            DMRemoveTree(fresh.root, NULL);
            if (!unchanged) {
                DMRemoveTree(local.root, NULL);
                continue;
            }
            if (choice == 0) {
                if (remote.revision.length == 0) {
                    DMRemoveTree(local.root, NULL);
                    continue;
                }
                DMMacCloudState *installed = nil;
                if (!DMInstallRemote(context.config, context.root, context.cloudRoot, context.baselinePath,
                                     context.scope, remote, local.snapshotSHA256, &installed, &error)) {
                    DMRemoveTree(local.root, NULL);
                    if (error.code == DMMacCloudErrorConflict || error.code == DMMacCloudErrorReevaluate) continue;
                    NSInteger networkChoice = DMShowNetworkError(error.localizedDescription);
                    if (networkChoice == 0) continue;
                    context.online = NO; context.paused = YES;
                    return networkChoice == 1 ? 1 : 0;
                }
                DMRemoveTree(local.root, NULL);
                context.online = YES; context.paused = NO;
                return 1;
            }
            DMMacCloudState *uploaded = DMHTTPPut(context.config, local, remote.revision, &error);
            DMRemoveTree(local.root, NULL);
            if (!uploaded) {
                if (error.code == DMMacCloudErrorConflict) continue;
                NSInteger networkChoice = DMShowNetworkError(error.localizedDescription);
                if (networkChoice == 0) continue;
                context.online = NO; context.paused = YES;
                return networkChoice == 1 ? 1 : 0;
            }
            if (!DMSaveBaseline(context.baselinePath, context.scope, uploaded, &error)) {
                NSInteger networkChoice = DMShowNetworkError(error.localizedDescription);
                if (networkChoice == 0) continue;
                context.online = NO; context.paused = YES;
                return networkChoice == 1 ? 1 : 0;
            }
            context.online = YES; context.paused = NO;
            return 1;
        }
        if (decision == DMMacCloudDecisionAck) {
            if (!DMSaveBaseline(context.baselinePath, context.scope, remote, &error)) {
                DMRemoveTree(local.root, NULL);
                NSInteger choice = DMShowNetworkError(error.localizedDescription);
                if (choice == 0) continue;
                context.online = NO; context.paused = YES;
                return choice == 1 ? 1 : 0;
            }
            DMRemoveTree(local.root, NULL);
            context.online = YES; context.paused = NO;
            return 1;
        }
        if (decision == DMMacCloudDecisionUpload) {
            DMMacCloudState *uploaded = DMHTTPPut(context.config, local, remote.revision, &error);
            DMRemoveTree(local.root, NULL);
            if (!uploaded) {
                if (error.code == DMMacCloudErrorConflict) continue;
                NSInteger choice = DMShowNetworkError(error.localizedDescription);
                if (choice == 0) continue;
                context.online = NO; context.paused = YES;
                return choice == 1 ? 1 : 0;
            }
            if (!DMSaveBaseline(context.baselinePath, context.scope, uploaded, &error)) {
                NSInteger choice = DMShowNetworkError(error.localizedDescription);
                if (choice == 0) continue;
                context.online = NO; context.paused = YES;
                return choice == 1 ? 1 : 0;
            }
            context.online = YES; context.paused = NO;
            return 1;
        }
        if (remote.revision.length == 0) continue;
        DMMacCloudState *installed = nil;
        if (!DMInstallRemote(context.config, context.root, context.cloudRoot, context.baselinePath,
                             context.scope, remote, local.snapshotSHA256, &installed, &error)) {
            DMRemoveTree(local.root, NULL);
            if (error.code == DMMacCloudErrorConflict || error.code == DMMacCloudErrorReevaluate) continue;
            NSInteger choice = DMShowNetworkError(error.localizedDescription);
            if (choice == 0) continue;
            context.online = NO; context.paused = YES;
            return choice == 1 ? 1 : 0;
        }
        DMRemoveTree(local.root, NULL);
        context.online = YES; context.paused = NO;
        return 1;
    }
}

int dm_mac_cloud_boot(const char *root, const char *config_path) {
    @autoreleasepool {
        DMWorkerQueue();
        if (g_context) {
            DMMacCloudContext *old = g_context;
            old.reset = YES; old.online = NO; old.paused = YES;
            if (old.timer) { dispatch_source_cancel(old.timer); old.timer = nil; }
            g_context = nil;
        }
        DMMacCloudContext *context = [DMMacCloudContext new];
        context.online = NO; context.paused = YES; context.reset = NO;
        if (!root || !config_path) {
            NSInteger choice = DMShowUnsupported(@"云存档启动参数或配置路径缺失");
            return choice == 0 ? 1 : 0;
        }
        NSError *error = nil;
        context.root = @(root);
        context.configPath = @(config_path);
        context.cloudRoot = DMCloudRoot(context.root, &error);
        if (!context.cloudRoot) {
            NSInteger choice = DMShowUnsupported(error.localizedDescription);
            return choice == 0 ? 1 : 0;
        }
        context.config = DMLoadConfig(context.configPath, &error);
        if (!context.config) {
            NSInteger choice = DMShowUnsupported(error.localizedDescription);
            return choice == 0 ? 1 : 0;
        }
        context.scope = DMConfigScope(context.config);
        if (!context.scope) {
            NSInteger choice = DMShowUnsupported(@"无法计算云存档配置作用域");
            return choice == 0 ? 1 : 0;
        }
        context.baselinePath = DMBaselinePath(context.cloudRoot, context.scope);
        context.baseline = DMLoadBaseline(context.baselinePath, context.scope);
        int result = DMReconcile(context);
        fprintf(stderr, "[cloud] Startup decision: %s.\n", result != 1 ? "quit" : context.online ? "online" : "offline");
        if (result == 1 && context.online) {
            g_context = context;
            [context startTimer];
        }
        return result;
    }
}

void dm_mac_cloud_background(void) {
    DMMacCloudContext *context = g_context;
    if (!context || !context.online || context.paused || context.reset) return;
    dispatch_async(DMWorkerQueue(), ^{ [context performBackgroundSync]; });
}

int dm_mac_cloud_flush_wait(double seconds) {
    DMMacCloudContext *context = g_context;
    if (!context || !context.online || context.paused || context.reset) return 1;
    if (dispatch_get_specific(DMMacCloudQueueKey)) return 0;
    double bounded = seconds;
    if (!(bounded >= 0)) bounded = 0;
    if (bounded > 60.0) bounded = 60.0;
    dispatch_group_t group = dispatch_group_create();
    dispatch_group_enter(group);
    dispatch_async(DMWorkerQueue(), ^{
        @autoreleasepool { [context performBackgroundSync]; }
        dispatch_group_leave(group);
    });
    dispatch_time_t deadline = dispatch_time(DISPATCH_TIME_NOW, (int64_t)(bounded * NSEC_PER_SEC));
    return dispatch_group_wait(group, deadline) == 0 ? 1 : 0;
}

#ifdef DM_MAC_CLOUD_TESTING
void dm_mac_cloud_test_set_alert(dm_mac_cloud_test_alert_fn callback, void *context) {
    g_testAlert = callback;
    g_testAlertContext = context;
}

void dm_mac_cloud_test_reset(void) {
    DMMacCloudContext *context = g_context;
    if (context) {
        context.reset = YES; context.online = NO; context.paused = YES;
        if (context.timer) { dispatch_source_cancel(context.timer); context.timer = nil; }
    }
    g_context = nil;
    if (g_worker && !dispatch_get_specific(DMMacCloudQueueKey)) dispatch_sync(g_worker, ^{});
}

int dm_mac_cloud_test_decide(const char *local_hash, const char *remote_revision,
                             const char *remote_hash, int baseline_present,
                             const char *baseline_revision, const char *baseline_hash) {
    DMMacCloudState *remote = [DMMacCloudState new];
    remote.revision = remote_revision ? @(remote_revision) : @"";
    remote.snapshotSHA256 = remote_hash ? @(remote_hash) : @"";
    DMMacCloudBaseline *baseline = nil;
    if (baseline_present) {
        baseline = [DMMacCloudBaseline new];
        baseline.revision = baseline_revision ? @(baseline_revision) : @"";
        baseline.snapshotSHA256 = baseline_hash ? @(baseline_hash) : @"";
    }
    return (int)DMDecide(local_hash ? @(local_hash) : @"", remote, baseline);
}

int dm_mac_cloud_test_compare_utf16(const char *left, const char *right) {
    return DMUTF16Compare(left ? @(left) : @"", right ? @(right) : @"");
}
#endif
