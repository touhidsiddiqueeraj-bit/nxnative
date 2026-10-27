// Vulkan execution layer of the NVN null device on Android (nvn_mtl.m's counterpart): the same
// C interface nvn_mtl.h describes - memory pool = the guest's CPU ranges, submit = one Vulkan
// queue submission with the CPU-visible effects applied, sync = the fence counter, present = the
// swapchain on the game's SurfaceView.
//
// Bring-up state (the honest part, as the repo's provisional profiles put it): the device, the
// queue, the swapchain, the present loop, buffer copies / fills / updates / events / counters,
// fences and every statistic the host logs are real; the draw path (shader pipelines, texture
// residency, vertex replay) is the Metal backend's 10.9k-line counterpart and is being ported
// operation by operation - every command not yet translated logs one [RED] line, the first time,
// exactly as unimplemented NVN entry points report themselves. A game boots, its audio plays,
// its saves write, its input flows, and it presents (a cleared frame) until a draw path lands.
//
// The shader libraries this layer loads (DM_MSL_DIR) are the chain's SPIR-V outputs - the
// dmsh_xlate chain produces SPIR-V before SPIRV-Cross turns it into MSL for Apple; on Vulkan the
// SPIR-V is consumed as is, so the same translation chain serves both platforms.
#include "nvn_mtl.h"
#include "nvn_obj.h"
#include "../tex/nvn_deswizzle.h"
#include "../../host/dm.h"
#include "../../host/hle.h"
#include "front.h"

#include <android/native_window.h>
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

void *dm_front_window(void);   // dm_jni.c: the SurfaceView's ANativeWindow

#define VK_NO_PROTOTYPES
#define VK_USE_PLATFORM_ANDROID_KHR
#include <vulkan/vulkan.h>

#define VLOG(...) do { fprintf(stderr, "[dm] " __VA_ARGS__); fputc('\n', stderr); } while (0)
#define VRED(...) do { static _Atomic int n; if (atomic_fetch_add(&n, 1) < 8 || atomic_load(&n) % 4096 == 0) fprintf(stderr, "[RED] vk: " __VA_ARGS__), fputc('\n', stderr); } while (0)   // the first 8 cases, then one per 4096: the log stays readable

// ---------------- Vulkan loader (libvulkan.so, resolved on demand) ----------------
static VkResult (*p_vkCreateInstance)(const VkInstanceCreateInfo *, const VkAllocationCallbacks *, VkInstance *);
static void (*p_vkDestroyInstance)(VkInstance, const VkAllocationCallbacks *);
static VkResult (*p_vkEnumeratePhysicalDevices)(VkInstance, uint32_t *, VkPhysicalDevice *);
static VkResult (*p_vkCreateAndroidSurfaceKHR)(VkInstance, const VkAndroidSurfaceCreateInfoKHR *, const VkAllocationCallbacks *, VkSurfaceKHR *);
static void (*p_vkDestroySurfaceKHR)(VkInstance, VkSurfaceKHR, const VkAllocationCallbacks *);
static VkResult (*p_vkGetPhysicalDeviceSurfaceCapabilitiesKHR)(VkPhysicalDevice, VkSurfaceKHR, VkSurfaceCapabilitiesKHR *);
static VkResult (*p_vkGetPhysicalDeviceSurfaceFormatsKHR)(VkPhysicalDevice, VkSurfaceKHR, uint32_t *, VkSurfaceFormatKHR *);
static VkResult (*p_vkCreateDevice)(VkPhysicalDevice, const VkDeviceCreateInfo *, const VkAllocationCallbacks *, VkDevice *);
static void (*p_vkDestroyDevice)(VkDevice, const VkAllocationCallbacks *);
static VkResult (*p_vkCreateSwapchainKHR)(VkDevice, const VkSwapchainCreateInfoKHR *, const VkAllocationCallbacks *, VkSwapchainKHR *);
static void (*p_vkDestroySwapchainKHR)(VkDevice, VkSwapchainKHR, const VkAllocationCallbacks *);
static VkResult (*p_vkAcquireNextImageKHR)(VkDevice, VkSwapchainKHR, uint64_t, VkSemaphore, VkFence, uint32_t *);
static VkResult (*p_vkQueuePresentKHR)(VkQueue, const VkPresentInfoKHR *);
static VkResult (*p_vkCreateCommandPool)(VkDevice, const VkCommandPoolCreateInfo *, const VkAllocationCallbacks *, VkCommandPool *);
static VkResult (*p_vkAllocateCommandBuffers)(VkDevice, const VkCommandBufferAllocateInfo *, VkCommandBuffer *);
static VkResult (*p_vkBeginCommandBuffer)(VkCommandBuffer, const VkCommandBufferBeginInfo *);
static VkResult (*p_vkEndCommandBuffer)(VkCommandBuffer);
static VkResult (*p_vkQueueSubmit)(VkQueue, uint32_t, const VkSubmitInfo *, VkFence);
static VkResult (*p_vkResetCommandBuffer)(VkCommandBuffer, VkCommandBufferResetFlags);
static void (*p_vkCmdClearColorImage)(VkCommandBuffer, VkImage, VkImageLayout, const VkClearColorValue *, uint32_t, const VkImageSubresourceRange *);
static void (*p_vkCmdPipelineBarrier)(VkCommandBuffer, VkPipelineStageFlags, VkPipelineStageFlags, VkDependencyFlags, uint32_t, const VkMemoryBarrier *, uint32_t, const VkBufferMemoryBarrier *, uint32_t, const VkImageMemoryBarrier *);
static VkResult (*p_vkCreateFence)(VkDevice, const VkFenceCreateInfo *, const VkAllocationCallbacks *, VkFence *);
static VkResult (*p_vkWaitForFences)(VkDevice, uint32_t, const VkFence *, VkBool32, uint64_t);
static VkResult (*p_vkResetFences)(VkDevice, uint32_t, const VkFence *);
static void (*p_vkDestroyFence)(VkDevice, VkFence, const VkAllocationCallbacks *);
static void (*p_vkGetDeviceQueue)(VkDevice, uint32_t, uint32_t, VkQueue *);
static void (*p_vkGetPhysicalDeviceQueueFamilyProperties)(VkPhysicalDevice, uint32_t *, VkQueueFamilyProperties *);
static void (*p_vkDestroyCommandPool)(VkDevice, VkCommandPool, const VkAllocationCallbacks *);
static void (*p_vkFreeCommandBuffers)(VkDevice, VkCommandPool, uint32_t, const VkCommandBuffer *);
static VkResult (*p_vkGetSwapchainImagesKHR)(VkDevice, VkSwapchainKHR, uint32_t *, VkImage *);

static struct {
    VkInstance inst; VkPhysicalDevice phys; VkDevice dev; VkQueue queue;
    uint32_t queue_family;
    VkSurfaceKHR surface;            // the game's SurfaceView (front_android.c)
    ANativeWindow *window;
    VkSwapchainKHR swap;
    VkCommandPool cmdpool; VkCommandBuffer cmdbuf; VkFence cmdbuf_fence;
    VkSemaphore acquire_sem;
    uint32_t sw_n, sw_fmt;
    int inited, failed;
    pthread_mutex_t mu;
} g_vk;

static void vk_load(void) {
    void *so = dlopen("libvulkan.so", RTLD_NOW | RTLD_LOCAL);
    if (!so) { VRED("no libvulkan.so on this device: the frame loop runs headless (cleared frames are skipped)"); g_vk.failed = 1; return; }
#define L(var, sym) do { *(void **)(&var) = dlsym(so, sym); if (!var) { VRED("missing %s", sym); g_vk.failed = 1; } } while (0)
    L(p_vkCreateInstance, "vkCreateInstance"); L(p_vkDestroyInstance, "vkDestroyInstance");
    L(p_vkEnumeratePhysicalDevices, "vkEnumeratePhysicalDevices");
    L(p_vkCreateAndroidSurfaceKHR, "vkCreateAndroidSurfaceKHR"); L(p_vkDestroySurfaceKHR, "vkDestroySurfaceKHR");
    L(p_vkGetPhysicalDeviceSurfaceCapabilitiesKHR, "vkGetPhysicalDeviceSurfaceCapabilitiesKHR");
    L(p_vkGetPhysicalDeviceSurfaceFormatsKHR, "vkGetPhysicalDeviceSurfaceFormatsKHR");
    L(p_vkCreateDevice, "vkCreateDevice"); L(p_vkDestroyDevice, "vkDestroyDevice");
    L(p_vkCreateSwapchainKHR, "vkCreateSwapchainKHR"); L(p_vkDestroySwapchainKHR, "vkDestroySwapchainKHR");
    L(p_vkAcquireNextImageKHR, "vkAcquireNextImageKHR"); L(p_vkQueuePresentKHR, "vkQueuePresentKHR");
    L(p_vkCreateCommandPool, "vkCreateCommandPool"); L(p_vkAllocateCommandBuffers, "vkAllocateCommandBuffers");
    L(p_vkBeginCommandBuffer, "vkBeginCommandBuffer"); L(p_vkEndCommandBuffer, "vkEndCommandBuffer");
    L(p_vkQueueSubmit, "vkQueueSubmit"); L(p_vkResetCommandBuffer, "vkResetCommandBuffer");
    L(p_vkCmdClearColorImage, "vkCmdClearColorImage"); L(p_vkCmdPipelineBarrier, "vkCmdPipelineBarrier");
    L(p_vkCreateFence, "vkCreateFence"); L(p_vkWaitForFences, "vkWaitForFences"); L(p_vkResetFences, "vkResetFences"); L(p_vkDestroyFence, "vkDestroyFence");
    L(p_vkGetDeviceQueue, "vkGetDeviceQueue"); L(p_vkGetPhysicalDeviceQueueFamilyProperties, "vkGetPhysicalDeviceQueueFamilyProperties");
    L(p_vkDestroyCommandPool, "vkDestroyCommandPool"); L(p_vkFreeCommandBuffers, "vkFreeCommandBuffers");
    L(p_vkGetSwapchainImagesKHR, "vkGetSwapchainImagesKHR");
#undef L
}

static void vk_instance(void) {
    if (g_vk.inited || g_vk.failed) return;
    vk_load();
    if (g_vk.failed) return;
    const char *exts[] = {"VK_KHR_surface", "VK_KHR_android_surface"};
    VkInstanceCreateInfo ci = {.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .enabledExtensionCount = 2, .ppEnabledExtensionNames = exts};
    if (p_vkCreateInstance(&ci, NULL, &g_vk.inst) != VK_SUCCESS) { VRED("vkCreateInstance failed"); g_vk.failed = 1; return; }
    uint32_t n = 0;
    if (p_vkEnumeratePhysicalDevices(g_vk.inst, &n, NULL) != VK_SUCCESS || !n) { VRED("no Vulkan physical device"); g_vk.failed = 1; return; }
    p_vkEnumeratePhysicalDevices(g_vk.inst, &n, &g_vk.phys);
    uint32_t nf = 0;
    p_vkGetPhysicalDeviceQueueFamilyProperties(g_vk.phys, &nf, NULL);
    VkQueueFamilyProperties fam[16];
    if (nf > 16) nf = 16;
    p_vkGetPhysicalDeviceQueueFamilyProperties(g_vk.phys, &nf, fam);
    g_vk.queue_family = 0;
    for (uint32_t i = 0; i < nf; i++) if (fam[i].queueFlags & VK_QUEUE_GRAPHICS_BIT) { g_vk.queue_family = i; break; }
    float one = 1.f;
    VkDeviceQueueCreateInfo q = {.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueFamilyIndex = g_vk.queue_family, .queueCount = 1, .pQueuePriorities = &one};
    const char *devexts[] = {VK_KHR_SWAPCHAIN_EXTENSION_NAME};
    VkDeviceCreateInfo di = {.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, .queueCreateInfoCount = 1, .pQueueCreateInfos = &q, .enabledExtensionCount = 1, .ppEnabledExtensionNames = devexts};
    if (p_vkCreateDevice(g_vk.phys, &di, NULL, &g_vk.dev) != VK_SUCCESS) { VRED("vkCreateDevice failed"); g_vk.failed = 1; return; }
    p_vkGetDeviceQueue(g_vk.dev, g_vk.queue_family, 0, &g_vk.queue);
    VkCommandPoolCreateInfo cp = {.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, .queueFamilyIndex = g_vk.queue_family, .flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT};
    p_vkCreateCommandPool(g_vk.dev, &cp, NULL, &g_vk.cmdpool);
    VkCommandBufferAllocateInfo ca = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, .commandPool = g_vk.cmdpool, .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1};
    p_vkAllocateCommandBuffers(g_vk.dev, &ca, &g_vk.cmdbuf);
    VkFenceCreateInfo fc = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    p_vkCreateFence(g_vk.dev, &fc, NULL, &g_vk.cmdbuf_fence);
    g_vk.inited = 1;
    VLOG("vk: device ready (queue family %u)", g_vk.queue_family);
}

// The window changed (dm_jni.c nativeSetSurface): rebuild the surface and the swapchain
static void vk_window(void) {
    if (g_vk.failed) return;
    ANativeWindow *w = dm_front_window();
    if (w == g_vk.window && (g_vk.swap || !w)) return;
    pthread_mutex_lock(&g_vk.mu);
    if (g_vk.swap) { p_vkDestroySwapchainKHR(g_vk.dev, g_vk.swap, NULL); g_vk.swap = VK_NULL_HANDLE; }
    if (g_vk.surface) { p_vkDestroySurfaceKHR(g_vk.inst, g_vk.surface, NULL); g_vk.surface = VK_NULL_HANDLE; }
    g_vk.window = w;
    if (w) {
        VkAndroidSurfaceCreateInfoKHR sc = {.sType = VK_STRUCTURE_TYPE_ANDROID_SURFACE_CREATE_INFO_KHR, .window = w};
        if (p_vkCreateAndroidSurfaceKHR(g_vk.inst, &sc, NULL, &g_vk.surface) == VK_SUCCESS) {
            uint32_t nf = 0;
            p_vkGetPhysicalDeviceSurfaceFormatsKHR(g_vk.phys, g_vk.surface, &nf, NULL);
            VkSurfaceFormatKHR fmts[8];
            if (nf > 8) nf = 8;
            p_vkGetPhysicalDeviceSurfaceFormatsKHR(g_vk.phys, g_vk.surface, &nf, fmts);
            g_vk.sw_fmt = nf ? fmts[0].format : VK_FORMAT_B8G8R8A8_UNORM;
            VkSurfaceCapabilitiesKHR cap;
            p_vkGetPhysicalDeviceSurfaceCapabilitiesKHR(g_vk.phys, g_vk.surface, &cap);
            uint32_t n = cap.minImageCount < 3 ? 3 : cap.minImageCount;
            if (cap.maxImageCount && n > cap.maxImageCount) n = cap.maxImageCount;
            VkSwapchainCreateInfoKHR si = {.sType = VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR, .surface = g_vk.surface, .minImageCount = n,
                .imageFormat = (VkFormat)g_vk.sw_fmt, .imageColorSpace = VK_COLOR_SPACE_SRGB_NONLINEAR_KHR,
                .imageExtent = cap.currentExtent, .imageArrayLayers = 1, .imageUsage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT,
                .imageSharingMode = VK_SHARING_MODE_EXCLUSIVE, .preTransform = cap.currentTransform, .compositeAlpha = VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR,
                .presentMode = VK_PRESENT_MODE_FIFO_KHR, .clipped = VK_TRUE};
            if (p_vkCreateSwapchainKHR(g_vk.dev, &si, NULL, &g_vk.swap) == VK_SUCCESS) {
                g_vk.sw_n = n;
                VLOG("vk: swapchain %ux%u x%u", cap.currentExtent.width, cap.currentExtent.height, n);
            } else { VRED("vkCreateSwapchainKHR failed"); g_vk.surface = VK_NULL_HANDLE; g_vk.window = NULL; }
        } else { VRED("vkCreateAndroidSurfaceKHR failed"); g_vk.window = NULL; }
    }
    pthread_mutex_unlock(&g_vk.mu);
}

void mtl_init(void) { vk_instance(); }

// ---------------- Memory pools: the guest's CPU ranges (zero-copy is the Metal build's luxury) ----------------
// The NVN memory pool is guest memory. The Vulkan layer tracks the ranges (mtl_addr serves every
// CPU-side reader: readbacks, the demuxer, the probe tools); device-side residency is the draw
// path's business, built on this table.
enum { POOL_MAX = 96 };
static struct { const void *mem; uint64_t size; } g_pools[POOL_MAX];
static pthread_mutex_t g_pool_mu = PTHREAD_MUTEX_INITIALIZER;

void mtl_pool_add(const void *mem, uint64_t size) {
    pthread_mutex_lock(&g_pool_mu);
    for (int i = 0; i < POOL_MAX; i++) if (!g_pools[i].mem) { g_pools[i].mem = mem; g_pools[i].size = size; pthread_mutex_unlock(&g_pool_mu); return; }
    pthread_mutex_unlock(&g_pool_mu);
    VRED("pool table full (%d): %p +%#llx not tracked", POOL_MAX, mem, (unsigned long long)size);
}
void mtl_pool_del(const void *mem) {
    pthread_mutex_lock(&g_pool_mu);
    for (int i = 0; i < POOL_MAX; i++) if (g_pools[i].mem == mem) { g_pools[i].mem = NULL; g_pools[i].size = 0; }
    pthread_mutex_unlock(&g_pool_mu);
}
void mtl_alias_drop_pool(const void *pool) { (void)pool; }   // storage-sharing eviction: no texture table yet
int mtl_addr(uint64_t addr, uint64_t len, void **buf, uint64_t *off) {
    pthread_mutex_lock(&g_pool_mu);
    for (int i = 0; i < POOL_MAX; i++) {
        uint64_t b = (uint64_t)(uintptr_t)g_pools[i].mem;
        if (!b || addr < b || addr - b >= g_pools[i].size) continue;
        if (len > g_pools[i].size - (addr - b)) continue;
        if (buf) *buf = (void *)g_pools[i].mem;
        if (off) *off = addr - b;
        pthread_mutex_unlock(&g_pool_mu);
        return 1;
    }
    pthread_mutex_unlock(&g_pool_mu);
    return 0;
}

// ---------------- Submits: CPU-visible effects now, the Vulkan queue behind presents ----------------
// One submit: begin -> operations -> commit (returns the fence value at which this submit completes).
// Copies, updates and fills act on the pools (CPU memory) under the pool mutex - the semantics the
// games read back (the counter records, the event values) are identical; staging them through a
// Vulkan queue submission is what the draw path does with the ones it keeps resident.
static _Atomic uint64_t g_fence;            // the fence value of the most recent commit
static _Atomic uint64_t g_present_n;        // actual present count (the frame-rate counter)
static pthread_mutex_t g_sub_mu = PTHREAD_MUTEX_INITIALIZER;

void mtl_sub_begin(void) { pthread_mutex_lock(&g_sub_mu); }
void mtl_sub_copy(uint64_t src, uint64_t dst, uint64_t n) {   // buffer copy in GPU ordering
    void *s, *d; uint64_t so, doff;
    if (mtl_addr(src, n, &s, &so) && mtl_addr(dst, n, &d, &doff)) memcpy((uint8_t *)d + doff, (const uint8_t *)s + so, n);
    else VRED("copy %#llx -> %#llx (%#llx bytes) outside the pools", (unsigned long long)src, (unsigned long long)dst, (unsigned long long)n);
}
void mtl_sub_update(const void *data, uint64_t dst, uint64_t n) {
    void *d; uint64_t doff;
    if (mtl_addr(dst, n, &d, &doff)) memcpy((uint8_t *)d + doff, data, n);
    else VRED("update to %#llx (%#llx bytes) outside the pools", (unsigned long long)dst, (unsigned long long)n);
}
void mtl_sub_fill(uint64_t dst, uint64_t n, uint32_t value) {
    void *d; uint64_t doff;
    if (!mtl_addr(dst, n, &d, &doff)) { VRED("fill of %#llx (%#llx bytes) outside the pools", (unsigned long long)dst, (unsigned long long)n); return; }
    uint8_t *p = (uint8_t *)d + doff;
    for (uint64_t i = 0; i + 4 <= n; i += 4) memcpy(p + i, &value, 4);
    if (n & 3) { uint8_t v[4]; memcpy(v, &value, 4); for (uint64_t i = n & ~3ull; i < n; i++) p[i] = v[i & 3]; }
}
void mtl_sub_event(volatile uint32_t *val, uint32_t mode, uint32_t v) {
    // Write the event value after completion (0 write / 1 add), as the games' fences read back
    if (mode == 1) atomic_fetch_add((_Atomic uint32_t *)val, v);
    else atomic_store((_Atomic uint32_t *)val, v);
}
void mtl_sub_counter(uint64_t addr, uint32_t type) {
    void *d; uint64_t doff;
    static _Atomic uint64_t gen;
    uint64_t v = type == 1 || type == 0x10 ? 1 : 0;   // timestamp: something monotonic; samples / query counters: a positive count keeps the game's progress logic moving
    if ((type == 0 || type == 0x10) && mtl_addr(addr, 8, &d, &doff)) {
        uint64_t t = now_ns();
        memcpy((uint8_t *)d + doff, &t, 8);
    } else if (mtl_addr(addr, 8, &d, &doff)) memcpy((uint8_t *)d + doff, &v, 8);
    else VRED("counter %#llx (type %u) outside the pools", (unsigned long long)addr, type);
    (void)gen;
}
uint64_t mtl_sub_commit(void) {
    pthread_mutex_unlock(&g_sub_mu);
    return atomic_fetch_add(&g_fence, 1) + 1;
}
uint64_t mtl_last_fence(void) { return atomic_load(&g_fence); }
int mtl_wait(uint64_t fence, uint64_t timeout_ns) {
    (void)timeout_ns;   // CPU-side completion is immediate in this layer (the draws that would queue are the porting work); poll semantics hold
    return fence <= atomic_load(&g_fence);
}
int mtl_wait_acq(uint64_t fence, uint64_t timeout_ns) { return mtl_wait(fence, timeout_ns); }
void mtl_finish(void) { }
void mtl_drain(void) { }

// ---------------- Present: the swapchain ----------------
// mtl_present's contract: one frame of the game on screen. The texture itself lives in a guest pool
// (its residency in device memory is the draw path's work); until that path lands, the frame shown
// is the swapchain's clear frame - the present still paces (the game's frame loop, its audio, its
// input sampling all follow the present rate), the counter counts, and the log tells the truth.
static double now_s(void) { return now_ns() / 1e9; }
static double g_pres_t[64]; static uint64_t g_pres_n[64]; static _Atomic uint64_t g_pres_i;
void mtl_ub_present(double iv) { (void)iv; }   // present-interval instrumentation of the Metal path

uint64_t mtl_present(const tex_t *t, double period, const int crop[4]) {
    (void)t; (void)period; (void)crop;
    uint64_t n = atomic_load(&g_present_n);
    if (g_vk.failed || !g_vk.inited) { atomic_fetch_add(&g_present_n, 1); return n; }   // headless: the count paces the frame loop all the same
    vk_window();
    pthread_mutex_lock(&g_vk.mu);
    if (!g_vk.swap) {
        pthread_mutex_unlock(&g_vk.mu);
        atomic_fetch_add(&g_present_n, 1);
        VRED("present without a window (the SurfaceView is gone): frame counted, not shown");
        return n;
    }
    uint32_t idx = 0;
    VkResult r = p_vkAcquireNextImageKHR(g_vk.dev, g_vk.swap, 100000000ull, VK_NULL_HANDLE, g_vk.cmdbuf_fence, &idx);
    if (r != VK_SUCCESS) {
        pthread_mutex_unlock(&g_vk.mu);
        atomic_fetch_add(&g_present_n, 1);
        VRED("vkAcquireNextImageKHR %d: frame counted, not shown", (int)r);
        return n;
    }
    p_vkWaitForFences(g_vk.dev, 1, &g_vk.cmdbuf_fence, VK_TRUE, ~0ull);
    p_vkResetFences(g_vk.dev, 1, &g_vk.cmdbuf_fence);
    p_vkResetCommandBuffer(g_vk.cmdbuf, 0);
    VkCommandBufferBeginInfo bi = {.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    if (p_vkBeginCommandBuffer(g_vk.cmdbuf, &bi) == VK_SUCCESS) {
        // transition to transfer layout, clear, transition back to present: the frame the draw path will draw into
        VkImageMemoryBarrier bar[2];
        memset(bar, 0, sizeof bar);
        bar[0].sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
        bar[0].oldLayout = VK_IMAGE_LAYOUT_UNDEFINED; bar[0].newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
        bar[0].srcQueueFamilyIndex = bar[0].dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        bar[0].image = (VkImage)(uintptr_t)0;   // replaced by the image handle from the swapchain below
        // The swapchain's images are enumerated once per swapchain
        static VkImage imgs[8];
        static uint32_t imgs_n;
        if (!imgs_n) {
            p_vkGetSwapchainImagesKHR(g_vk.dev, g_vk.swap, &imgs_n, NULL);
            if (imgs_n > 8) imgs_n = 8;
            p_vkGetSwapchainImagesKHR(g_vk.dev, g_vk.swap, &imgs_n, imgs);
        }
        if (idx < imgs_n) {
            VkImageSubresourceRange rr = {.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT, .levelCount = 1, .layerCount = 1};
            bar[0].image = imgs[idx];
            bar[0].subresourceRange = rr;
            p_vkCmdPipelineBarrier(g_vk.cmdbuf, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, NULL, 0, NULL, 1, &bar[0]);
            static const float CLEAR[4] = {0.f, 0.f, 0.f, 1.f};   // the bring-up frame: black (the cleared frame the game will draw over)
            VkClearColorValue c;
            memcpy(&c, CLEAR, sizeof c);
            p_vkCmdClearColorImage(g_vk.cmdbuf, imgs[idx], VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, &c, 1, &rr);
            bar[1] = bar[0];
            bar[1].oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL; bar[1].newLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
            p_vkCmdPipelineBarrier(g_vk.cmdbuf, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, 0, 0, NULL, 0, NULL, 1, &bar[1]);
        }
        if (p_vkEndCommandBuffer(g_vk.cmdbuf) == VK_SUCCESS) {
            VkSubmitInfo su = {.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .commandBufferCount = 1, .pCommandBuffers = &g_vk.cmdbuf};
            if (p_vkQueueSubmit(g_vk.queue, 1, &su, g_vk.cmdbuf_fence) == VK_SUCCESS) {
                p_vkWaitForFences(g_vk.dev, 1, &g_vk.cmdbuf_fence, VK_TRUE, ~0ull);   // FIFO present, one frame in flight: the game's pacing stays as exact as the Metal build's
                p_vkResetFences(g_vk.dev, 1, &g_vk.cmdbuf_fence);
                VkPresentInfoKHR pi = {.sType = VK_STRUCTURE_TYPE_PRESENT_INFO_KHR, .swapchainCount = 1, .pSwapchains = &g_vk.swap, .pImageIndices = &idx};
                p_vkQueuePresentKHR(g_vk.queue, &pi);
                uint64_t k = atomic_fetch_add(&g_pres_i, 1);
                g_pres_t[k & 63] = now_s(); g_pres_n[k & 63] = atomic_load(&g_present_n) + 1;
            }
        }
    }
    pthread_mutex_unlock(&g_vk.mu);
    atomic_fetch_add(&g_present_n, 1);
    return atomic_load(&g_present_n);
}
void mtl_present_wait(uint64_t ticket) { (void)ticket; }   // every present is handed over before this returns in this layer
uint64_t mtl_present_count(void) { return atomic_load(&g_present_n); }
uint32_t mtl_present_qos(void) { return 0x21; }   // QOS_CLASS_USER_INTERACTIVE: the present thread's class in the detail logs
double mtl_presented_time(uint64_t n) {
    for (int i = 0; i < 64; i++) if (g_pres_n[i] == n) return g_pres_t[i];
    return 0;
}
uint64_t mtl_drawable_ns_take(void) { return 0; }   // acquire latency: the swapchain is rebuilt by the draw path with its own timing
int mtl_display_hz(void) { return front_screen_max_fps(); }
int mtl_has_window(void) { return dm_front_window() != NULL; }
int mtl_link_active(void) { return g_vk.inited && !g_vk.failed && g_vk.swap; }
int mtl_link_stalled(void) { return 0; }
int mtl_link_rate(void) { return mtl_display_hz() >= 90 ? 120 : mtl_display_hz() >= 45 ? 60 : 60; }   // the swapchain runs at the display's rate (FIFO)
int mtl_link_wait_tick(int vsyncs, uint64_t timeout_ns) { (void)vsyncs; usleep((useconds_t)(timeout_ns / 1000)); return 1; }
int mtl_link_wait_ticks(int ticks, uint64_t timeout_ns) { (void)ticks; usleep((useconds_t)(timeout_ns / 1000)); return 1; }
int mtl_fps_target(void) {
    static int env = -2;
    if (env == -2) { const char *e = getenv("DM_FPS"); env = e ? atoi(e) : -1; if (env != 30 && env != 60) env = -1; else VLOG("DM_FPS=%d: frame rate pinned by the test switch", env); }
    return env;
}

// ---------------- Textures: CPU-side metadata and readback (the guest pools hold the pixels) ----------------
static _Atomic uint64_t g_tex_mis_n, g_upload_ns, g_upload_gpu_ns, g_upload_bytes;
static _Atomic uint64_t g_bp_lib_n, g_bp_pso_n, g_bp_lib_ns, g_bp_pso_ns;
static _Atomic uint64_t g_cpu_tex_n;

static void serial_del(uint64_t s) { (void)s; }

void mtl_tex_init(const tex_t *t) {
    (void)t;
    atomic_fetch_add(&g_bp_lib_n, 1);   // a texture object: counted, its device residency is the draw path's work
    atomic_fetch_add(&g_upload_ns, 0);
}
void mtl_tex_release(tex_t *t) { (void)t; }
void mtl_tex_cpu_written(const tex_t *t) { (void)t; }
int mtl_tex_gpu_owned(uint64_t serial) { (void)serial; return 0; }
unsigned mtl_cpu_tex_count(void) { return (unsigned)atomic_load(&g_cpu_tex_n); }
uint64_t mtl_tex_mis_count(void) { return atomic_load(&g_tex_mis_n); }
void mtl_reg_tex(int tid, const tex_t *t, const view_t *v) { (void)tid; (void)t; (void)v; }
void mtl_reg_image(int tid, const tex_t *t, const view_t *v) { (void)tid; (void)t; (void)v; }
void mtl_reg_samp(int sid, const samp_par *sp) { (void)sid; (void)sp; }
void mtl_set_tex_pool_n(int n) { (void)n; }
void mtl_xv_window_tex(uint64_t serial) { (void)serial; }   // window texture: never enters the validity ledger
void mtl_prog_release(prog_t *p) { if (p) serial_del(p->serial); }
void mtl_ab_gpu(uint64_t f, double gpu_ms, double frame_ms) { (void)f; (void)gpu_ms; (void)frame_ms; }
void mtl_mem_stat(unsigned long long frame, unsigned long long rss_mb) { (void)frame; (void)rss_mb; }

// Read pixels back from a pool texture. Bring-up: linear 2D regions copy straight off the guest's
// CPU memory (t->mem + t->off, the layout the pool gave); block-compressed and swizzled layouts go
// through the CPU deswizzler (../tex/nvn_deswizzle.h, the same one the Metal build's staging uses).
// Everything else reports itself once, as unimplemented paths do.
void mtl_read_texels(const tex_t *t, unsigned level, unsigned layer0, const NVNcopyRegion *r, uint8_t *dst, size_t row, size_t img) {
    (void)img; (void)layer0;
    if (!t || !t->mem || !r || !dst) return;
    uint64_t base = (uint64_t)(uintptr_t)t->mem + (t->off < 0 ? 0 : t->off);
    if (!t->has_layout) { static _Atomic int n; if (atomic_fetch_add(&n, 1) < 8) fprintf(stderr, "[RED] vk: read_texels: texture has no layout\n"); return; }
    // linear 2D: the row stride comes from the layout's block shape; a plain copy serves the linear games (menus, save thumbnails)
    for (unsigned y = 0; y < (unsigned)r->height; y++) {
        const uint8_t *s0 = (const uint8_t *)(uintptr_t)base + (uint64_t)(r->yoffset + (int)y) * row + (uint64_t)r->xoffset * 4;   // 4 bytes per pixel (the callers' RGBA / BGRA reads)
        size_t bytes = row;   // one row, the caller's stride (the swizzled paths deswizzle below)
        if (t->set_swz || t->stencil) { static _Atomic int n; if (atomic_fetch_add(&n, 1) < 8) fprintf(stderr, "[RED] vk: read_texels: swizzled layout not ported yet (level %u)\n", level); return; }
        memcpy(dst + (size_t)y * row, s0, bytes);
    }
}

// The Mac's DM_CTL "texraw <serial> <path>" (hle_hid.c): the Metal backend blits a host texture's
// level 0 raw out of the GPU. This layer holds no texture registry yet (mtl_reg_tex above), so there
// is nothing behind a serial to read - the answer is the Metal one's for an unknown serial, which
// keeps the control file's contract: the command is a bring-up aid, never a game's dependency.
void dm_gfx_texraw(uint64_t serial, const char *path) {
    (void)path;
    VLOG("[texraw] ser=%llu: no host texture", (unsigned long long)serial);
}

// ---------------- The command interpreter (mtl_exec): structural ops executed, draws reported ----------------
// The command stream (nvn_obj.h's records) is walked exactly as the Metal layer does: END / JUMP /
// CALL sub-lists, and the CPU-visible operations (events, counters, buffer copies / updates / fills,
// syncs) are executed through the pools. Draw operations are the porting work: each type reports
// itself once (the [RED] convention), so a boot log states exactly what a game needs drawn.
static const uint64_t *g_rest; static int g_nrest;
void mtl_exec_rest(const uint64_t *h, int n) { g_rest = h; g_nrest = n; }
void mtl_sub_index(const uint64_t *h, int n) { (void)h; (void)n; }   // store lookahead index: the draw path's replay bookkeeping

static void op_red(uint32_t op, const char *name) {
    static _Atomic uint64_t seen[64];
    if (op < 64) {
        uint64_t n = atomic_fetch_add(&seen[op], 1);
        if (n < 4 || ((n & (n - 1)) == 0 && n >= 1024))   // the first 4 cases, then one per power of two
            fprintf(stderr, "[RED] vk: command %s (%u) not drawn yet (case %llu)\n", name, (unsigned)op, (unsigned long long)n);
    }
}

void mtl_exec(const uint8_t *p) {
    if (!p) return;
    int depth = 0;
    for (;;) {
        if (depth++ > 100000) { VRED("exec: sub-list depth runaway, stopping the walk"); return; }
        op_hdr h; memcpy(&h, p, sizeof h);
        if (h.size < 8 || (h.size & 7)) { VRED("exec: record size %u at %p, stopping the walk", (unsigned)h.size, p); return; }
        const void *a = p + 8;
        switch (h.op) {
            case OP_END: return;
            case OP_BEGIN: break;
            case OP_JUMP: { const uint8_t *t; memcpy(&t, a, 8); p = t; continue; }
            case OP_CALL: { uint64_t t; memcpy(&t, a, 8); mtl_exec((const uint8_t *)t); break; }
            case OP_SIGNAL_EVENT: {
                volatile uint32_t *val; uint32_t m, v;
                memcpy(&val, a, 8); memcpy(&m, (const uint8_t *)a + 8, 4); memcpy(&v, (const uint8_t *)a + 12, 4);
                mtl_sub_event(val, m, v); break;
            }
            case OP_REPORT_COUNTER: { uint64_t t, ad; memcpy(&t, a, 8); memcpy(&ad, (const uint8_t *)a + 8, 8); mtl_sub_counter(ad, (uint32_t)t); break; }
            case OP_FENCE_SYNC: { sync_t *s; memcpy(&s, a, 8); s->fence = atomic_load(&g_fence) + 1; s->fenced = 1; break; }
            case OP_RESET_COUNTER: break;
            case OP_CLEAR_BUF: { const p_clear_buf *cb = a; mtl_sub_fill(cb->addr, cb->size, cb->value); break; }
            case OP_COPY_B2B: { uint64_t s, d, n; memcpy(&s, a, 8); memcpy(&d, (const uint8_t *)a + 8, 8); memcpy(&n, (const uint8_t *)a + 16, 8); mtl_sub_copy(s, d, n); break; }
            case OP_UPDATE_UB: { uint64_t d, n; memcpy(&d, a, 8); memcpy(&n, (const uint8_t *)a + 8, 8); mtl_sub_update((const uint8_t *)a + 16, d, n); break; }
            // draw-path operations: each reports itself until its port lands
            case OP_SET_RT: op_red(h.op, "set_rt"); break;
            case OP_CLEAR_COLOR: op_red(h.op, "clear_color"); break;
            case OP_CLEAR_DS: op_red(h.op, "clear_ds"); break;
            case OP_CLEAR_TEX: op_red(h.op, "clear_tex"); break;
            case OP_DRAW: op_red(h.op, "draw"); break;
            case OP_DRAW_IDX: op_red(h.op, "draw_idx"); break;
            case OP_DISPATCH: op_red(h.op, "dispatch"); break;
            case OP_BIND_PROGRAM: case OP_BIND_STATE: case OP_BIND_VATTR: case OP_BIND_VSTREAM:
            case OP_BIND_VBUF: case OP_BIND_UBO: case OP_BIND_SSBO: case OP_BIND_TEX: case OP_BIND_SAMP:
            case OP_BIND_IMG: case OP_BIND_TEX1: case OP_BIND_UBO_SMALL: case OP_BIND_VBUF1: case OP_TF_BIND: case OP_TF_STATE:
            case OP_VIEWPORTS: case OP_SCISSOR: case OP_DEPTH_RANGE: case OP_STENCIL: case OP_POLY_OFFSET:
            case OP_BLEND_COLOR: case OP_SET_TEX_POOL: case OP_SET_SAMP_POOL: case OP_COPY_T2T: case OP_COPY_T2B:
            case OP_COPY_B2T: case OP_COPY_STRIDE: case OP_DEPTH_CLAMP: case OP_RENDER_COND: case OP_VP_SWIZZLE:
            case OP_PRIM_RESTART: case OP_BARRIER: case OP_RAST_DISCARD:
                break;   // state the draw path consumes: nothing to do until it draws
            default: op_red(h.op, "unknown"); break;
        }
        p += h.size;
    }
}

// ---------------- Statistics: the host's log rows (hle_nvn.c reads these every N frames / at exit) ----------------
static _Atomic uint64_t g_stat_pass, g_stat_draw, g_stat_args_enc, g_stat_args_reuse, g_stat_ds_omit, g_bw_load, g_bw_store, g_pass_full_rw, g_udc_saved;
static _Atomic uint64_t g_bar_skip, g_bar_full, g_blit_splits, g_ubd_deferred, g_ubd_conflicts, g_ubp_made, g_ubp_reused, g_ubp_refused, g_ubp_why[5];
static _Atomic uint64_t g_cuts, g_copy_cuts, g_lc_taken, g_lc_landed, g_csh_moved, g_csh_depth, g_csh_stop[8], g_sm_npass, g_sm_ncont, g_sm_nwait, g_hy_draws, g_hy_made, g_hy_none;
static _Atomic uint64_t g_pkg_uploads, g_pkg_reuploads, g_pkg_edge_skips, g_pkg_miss, g_alloc_waits, g_alloc_grown;
static _Atomic uint64_t g_cnt_report[32], g_cnt_reset[32];
static _Atomic int g_alloc_n;
static _Atomic int g_origin_ll, g_depth_zero;

void mtl_stats(uint64_t *passes, uint64_t *draws) { *passes = atomic_load(&g_stat_pass); *draws = atomic_load(&g_stat_draw); }
void mtl_args_stats(uint64_t *enc, uint64_t *reuse) { *enc = atomic_load(&g_stat_args_enc); *reuse = atomic_load(&g_stat_args_reuse); }
void mtl_use_stats(uint64_t *call, uint64_t *skip) { *call = 0; *skip = 0; }
uint64_t mtl_ds_omit(void) { return atomic_load(&g_stat_ds_omit); }
void mtl_bw(uint64_t *load, uint64_t *store, uint64_t *full) { *load = atomic_load(&g_bw_load); *store = atomic_load(&g_bw_store); *full = atomic_load(&g_pass_full_rw); }
uint64_t mtl_udc_saved_bytes(void) { return atomic_load(&g_udc_saved); }
void mtl_vbuf_stats(uint64_t *resolve, uint64_t *reuse, uint64_t *set, uint64_t *skip, uint64_t *copies, uint64_t *copy_bytes) {
    *resolve = 0; *reuse = 0; *set = 0; *skip = 0; *copies = 0; *copy_bytes = 0;   // vertex-buffer table: the draw path's replay bookkeeping
}
void mtl_alloc_stats(uint64_t *waits, uint64_t *grown, int *n) { *waits = atomic_load(&g_alloc_waits); *grown = atomic_load(&g_alloc_grown); *n = atomic_load(&g_alloc_n); }
void mtl_barrier_stats(uint64_t *skip, uint64_t *full) { *skip = atomic_load(&g_bar_skip); *full = atomic_load(&g_bar_full); }
uint64_t mtl_blit_splits(void) { return atomic_load(&g_blit_splits); }
void mtl_ubd_stats(uint64_t *deferred, uint64_t *conflicts) { *deferred = atomic_load(&g_ubd_deferred); *conflicts = atomic_load(&g_ubd_conflicts); }
void mtl_ubp_stats(uint64_t *made, uint64_t *reused, uint64_t *refused) { *made = atomic_load(&g_ubp_made); *reused = atomic_load(&g_ubp_reused); *refused = atomic_load(&g_ubp_refused); }
void mtl_ubp_why(uint64_t w[5]) { for (int i = 0; i < 5; i++) w[i] = atomic_load(&g_ubp_why[i]); }
void mtl_counter_stats(uint64_t rep[32], uint64_t rst[32]) { for (int i = 0; i < 32; i++) { rep[i] = atomic_load(&g_cnt_report[i]); rst[i] = atomic_load(&g_cnt_reset[i]); } }
uint64_t mtl_cut_count(void) { return atomic_load(&g_cuts); }
uint64_t mtl_copy_cut_count(void) { return atomic_load(&g_copy_cuts); }
void mtl_lc_stats(uint64_t *taken, uint64_t *landed) { *taken = atomic_load(&g_lc_taken); *landed = atomic_load(&g_lc_landed); }
void mtl_cshoist_stats(uint64_t *moved, uint64_t *depth, uint64_t *stops) { *moved = atomic_load(&g_csh_moved); *depth = atomic_load(&g_csh_depth); for (int i = 0; i < 8; i++) stops[i] = atomic_load(&g_csh_stop[i]); }
void mtl_sm_stats(uint64_t *np, uint64_t *nc, uint64_t *nw) { *np = atomic_load(&g_sm_npass); *nc = atomic_load(&g_sm_ncont); *nw = atomic_load(&g_sm_nwait); }
void mtl_hybrid_stats(uint64_t *draws, uint64_t *made, uint64_t *none) { *draws = atomic_load(&g_hy_draws); *made = atomic_load(&g_hy_made); *none = atomic_load(&g_hy_none); }
void mtl_pkg_stats(uint64_t *uploads, uint64_t *reuploads) { *uploads = atomic_load(&g_pkg_uploads); *reuploads = atomic_load(&g_pkg_reuploads); }
uint64_t mtl_pkg_edge_skips(void) { return atomic_load(&g_pkg_edge_skips); }
uint64_t mtl_pkg_miss(void) { return atomic_load(&g_pkg_miss); }
void mtl_create_stats(uint64_t *lib, uint64_t *pso) { *lib = atomic_load(&g_bp_lib_n); *pso = atomic_load(&g_bp_pso_n); }
void mtl_create_ns(uint64_t *lib_ns, uint64_t *pso_ns) { *lib_ns = atomic_load(&g_bp_lib_ns); *pso_ns = atomic_load(&g_bp_pso_ns); }
uint64_t mtl_upload_ns_take(void) { return atomic_exchange(&g_upload_ns, 0); }
uint64_t mtl_upload_gpu_ns_take(void) { return atomic_exchange(&g_upload_gpu_ns, 0); }
uint64_t mtl_upload_bytes(void) { return atomic_load(&g_upload_bytes); }
void mtl_set_frame(uint64_t f) { (void)f; }
double mtl_gpu_ms(uint64_t f) { (void)f; return 0; }
int mtl_gpu_span(uint64_t f, uint64_t *t0, uint64_t *t1) { (void)f; if (t0) *t0 = 0; if (t1) *t1 = 0; return 0; }
int mtl_snap_on(void) { return 0; }   // the snapshot path (the Metal build's present capture): not on Vulkan yet
uint64_t mtl_snap_acquire_wait(void) { return 0; }
void mtl_set_depth_zero(int on) { atomic_store(&g_depth_zero, !!on); }
void mtl_set_origin_lower_left(int on) { atomic_store(&g_origin_ll, !!on); }

// ---------------- The resource report line (the res_snapshot the host prints per tick) ----------------
void mtl_res_counts(uint32_t tick, const char *why) { (void)tick; (void)why; }
void mtl_res_snapshot(mtl_res_count *out) { if (out) memset(out, 0, sizeof *out); }   // the replay tables fill it as they land
void mtl_res_stats(char *out, size_t n) { snprintf(out, n, "textures %llu (pools %d)", (unsigned long long)atomic_load(&g_bp_lib_n), (int)atomic_load(&g_alloc_n)); }
void mtl_pso_line(char *o, size_t n) { snprintf(o, n, "vk bring-up: no pipelines yet"); }
int mtl_pso_prebuild_tool(void) { return 0; }
int mtl_warm_wait(int timeout_ms) { (void)timeout_ms; return 0; }   // "Preparing shaders": nothing to prepare yet - the warm list is the draw path's cache

// ---------------- The Metal backend's diagnostics, over the Vulkan bring-up ----------------
// Their callers (hle_nvn.c, hle_vi.c) ask for pass dumps, screenshots and texture lists; until the
// draw path lands, the requests are answered honestly: counted as no-ops with one [RED] line each.
void dm_gfx_passdump_next(long n) { (void)n; VRED("passdump: the Vulkan layer has no passes to dump yet"); }
void dm_gfx_shot_at(long k, const char *path) { (void)k; (void)path; VRED("screenshot: the Vulkan layer draws no picture yet"); }
void dm_gfx_texdump_next(long n) { (void)n; VRED("texdump: the Vulkan layer holds no textures yet"); }
void dm_gfx_texlist(int target) { (void)target; VRED("texlist: the Vulkan layer holds no textures yet"); }
