#!/usr/bin/env python3
"""The zero-copy flip cache must not hand back a surface recorded for a freed allocation.

The cache is keyed by the NvKmsKapiMemory pointer. A buffer freed and reallocated lands at the same
address, so a match on the pointer alone scans out the stale surface over the reallocated memory; on
the panel that is the every-other-frame black strobe after a refresh or resolution change. Two guards
keep it out: the allocation cookie (never issued twice, so a reallocation at the same address carries
a new one) and, for memory without a cookie, the epoch that every grant and release advances. With
debug.nvrmfb_flip_cookie_match on, an entry whose cookie still matches survives other grants and
releases, which used to empty the cache 160-200 times per heavy browser run.

The real lookup region is lifted out of nvrm-fb.cpp and compiled against a mock cache. The negative
control strips both guards from that same region and proves the lookup then returns the stale
surface, i.e. the test fails on the pre-fix code."""
import pathlib
import re
import subprocess
import tempfile

root = pathlib.Path(__file__).resolve().parents[1]
source = (root / 'kexts/NVRMFB/fb/nvrm-fb.cpp').read_text()

# Lift the exact lookup region: from the epoch sample to the end of the `if (cacheable)` block.
start = source.index('const UInt64 epoch = readFlipEpoch();')
unlock = source.index('IOSimpleLockUnlockEnableInterrupt(fFlipCacheLock, state);', start)
end = source.index('}', unlock) + 1
region = source[start:end]

guard = '(fFlipCache[i].epoch == epoch || (gFlipCookieMatch && cookie)) && fFlipCache[i].cookie == cookie && '
assert guard in region, \
    'the flip-cache lookup no longer requires the current epoch or a matching cookie -- the stale-surface strobe is back'

# The negative control is the same region with both guards removed (a lookup on the pointer alone).
control = region.replace(guard, '')
assert control != region, 'could not build the negative control'

fixture = r'''
#include <cassert>
#include <cstdint>
#include <cstdio>
using UInt64=uint64_t; using NvU64=uint64_t; using SInt64=int64_t;
struct NvKmsKapiMemory; struct NvKmsKapiSurface;
typedef int IOInterruptState;
struct IOSimpleLock;
static IOInterruptState IOSimpleLockLockDisableInterrupt(IOSimpleLock *) { return 0; }
static void IOSimpleLockUnlockEnableInterrupt(IOSimpleLock *, IOInterruptState) {}
static const SInt64 kFlipEpochLimit = 0x7fffffffffffffffLL;
static SInt64 gFlipEpoch = 1;
static UInt64 readFlipEpoch() { return (UInt64)gFlipEpoch; }
static void advanceFlipEpoch() { if (gFlipEpoch != kFlipEpochLimit) gFlipEpoch++; }
static int gFlipCookieMatch = 1;
static SInt64 gFlipCacheHit, gFlipCacheMiss;
static void OSAddAtomic64(SInt64 v, SInt64 *p) { *p += v; }

struct NVFlipEntry { struct NvKmsKapiMemory *mem; struct NvKmsKapiSurface *surf;
                     unsigned w, h, pitch; UInt64 epoch, cookie; struct NvKmsKapiMemory *pin; };
struct FB {
    NVFlipEntry fFlipCache[16] = {};
    IOSimpleLock *fFlipCacheLock = nullptr;
    unsigned fFlipCacheN = 0;
    struct NvKmsKapiSurface *fSurf = nullptr;
    // Run the lifted lookup region exactly and return what it resolved the surface to.
    struct NvKmsKapiSurface *lookup(struct NvKmsKapiMemory *mem, UInt64 cookie,
                                    unsigned pitch, unsigned w, unsigned h) {
        struct NvKmsKapiSurface *surf = mem ? nullptr : fSurf;
        LOOKUP_REGION
        return surf;
    }
};

static struct NvKmsKapiMemory *const M = (struct NvKmsKapiMemory *)0x1000; // reused address
static struct NvKmsKapiSurface *const S_OLD = (struct NvKmsKapiSurface *)0xA000;

static void seed(FB &fb, UInt64 cookie) {
    fb.fFlipCache[0] = { M, S_OLD, 1920, 1080, 7680, readFlipEpoch(), cookie, M };
    fb.fFlipCacheN = 1;
}
int main() {
    // Same epoch, same allocation: a genuine cache hit still works.
    { FB fb; seed(fb, 7u); assert(fb.lookup(M, 7u, 7680, 1920, 1080) == S_OLD); }
    // Another surface's grant or release advanced the epoch; this allocation (cookie 7) is still live: still a hit.
    { FB fb; seed(fb, 7u); advanceFlipEpoch(); assert(fb.lookup(M, 7u, 7680, 1920, 1080) == S_OLD); }
    // Freed and reallocated at the same address: the new allocation has a new cookie, so the stale surface is NOT returned.
    { FB fb; seed(fb, 7u); advanceFlipEpoch();
      struct NvKmsKapiSurface *got = fb.lookup(M, 8u, 7680, 1920, 1080);
      assert(got == nullptr && "stale surface scanned out over a reallocation -- the strobe bug"); }
    // Memory without a cookie: the epoch still guards it.
    { FB fb; seed(fb, 0u); advanceFlipEpoch();
      assert(fb.lookup(M, 0u, 7680, 1920, 1080) == nullptr && "cookie-less stale surface scanned out across an epoch"); }
    // With cookie matching off, the epoch alone guards every entry (the old behaviour).
    { FB fb; seed(fb, 7u); gFlipCookieMatch = 0; advanceFlipEpoch();
      assert(fb.lookup(M, 7u, 7680, 1920, 1080) == nullptr); gFlipCookieMatch = 1; }
    printf("OK\n");
    return 0;
}
'''

def build_and_run(label, lookup_text):
    src = fixture.replace('LOOKUP_REGION', lookup_text)
    with tempfile.TemporaryDirectory() as d:
        cpp = pathlib.Path(d) / 'tc.cpp'
        binp = pathlib.Path(d) / 'tc'
        cpp.write_text(src)
        subprocess.run(['c++', '-std=c++17', '-O0', str(cpp), '-o', str(binp)], check=True)
        return subprocess.run([str(binp)], capture_output=True, text=True)

# Fixed source: the assertions must pass.
r = build_and_run('fixed', region)
assert r.returncode == 0 and r.stdout.strip() == 'OK', \
    f'fixed lookup failed: rc={r.returncode} out={r.stdout!r} err={r.stderr!r}'
print('fixed source: PASS (hit while the allocation lives; miss after a reallocation; epoch guards cookie-less memory)')

# Negative control: the pre-fix lookup must FAIL the post-advance assertion (returns the stale surface).
r = build_and_run('control', control)
assert r.returncode != 0, 'negative control passed -- the test does not detect the stale-surface bug'
print('negative control (cookie and epoch guards removed): FAILS as required (returned the stale surface)')
print('PASS')
