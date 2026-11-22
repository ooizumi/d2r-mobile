/* This GameSir profile can appear in GameController without delivering input.
 * Read only the verified HID device, without seizing it or changing macOS
 * settings. All callbacks use the main run loop and the existing pad merger. */
#import <Foundation/Foundation.h>
#import <IOKit/hid/IOHIDManager.h>
#include "dm_gamepad_hid_mac.h"
#include "dm_gamesir_hid_map.h"
#include "hle.h"

enum { MAX_HID_PADS = 4 };
static struct raw_pad {
    IOHIDDeviceRef device;
    CFArrayRef elements;
    dm_gamesir_hid_state state;
    int ready;
    int logged_input;
} g_raw[MAX_HID_PADS];
static IOHIDManagerRef g_manager;
static int g_focused;
static dm_hid_pad_publish g_publish;
static dm_hid_pad_remove g_remove;
static void (*g_changed)(void);

static struct raw_pad *find_pad(IOHIDDeviceRef device) {
    for (int i = 0; i < MAX_HID_PADS; ++i)
        if (g_raw[i].device == device) return &g_raw[i];
    return NULL;
}
int dm_gamepad_hid_mac_claims_vendor(const char *name) {
    if (!name || strcasecmp(name,"GameSir-X4A for Xbox")) return 0;
    for (int i = 0; i < MAX_HID_PADS; ++i) if (g_raw[i].device && g_raw[i].ready) return 1;
    return 0;
}
static int read_value(struct raw_pad *pad, IOHIDValueRef value) {
    IOHIDElementRef element = IOHIDValueGetElement(value);
    IOHIDElementType type = IOHIDElementGetType(element);
    if (type < kIOHIDElementTypeInput_Misc || type > kIOHIDElementTypeInput_ScanCodes ||
        IOHIDValueGetLength(value) > (CFIndex)sizeof(int64_t) ||
        IOHIDValueGetTimeStamp(value) == 0) return 0;
    uint32_t page = IOHIDElementGetUsagePage(element), usage = IOHIDElementGetUsage(element);
    CFIndex min = IOHIDElementGetLogicalMin(element), max = IOHIDElementGetLogicalMax(element);
    if (min >= max || !((page == 9 && dm_gamesir_hid_button_bit(usage)) ||
        (page == 1 && (dm_gamesir_hid_axis_index(usage) >= 0 || usage == 57)) ||
        (page == 2 && (usage == 196 || usage == 197)))) return 0;
    if (!pad->ready) { pad->ready = 1; if (g_changed) g_changed(); }
    return dm_gamesir_hid_update(&pad->state,page,usage,IOHIDValueGetIntegerValue(value),min,max);
}
static void publish_pad(struct raw_pad *pad) {
    dm_gamesir_hid_state *s = &pad->state;
    if (!pad->logged_input && (s->buttons || abs(s->axes[0]) > DM_STICK_MAX / 4 ||
        abs(s->axes[1]) > DM_STICK_MAX / 4 || abs(s->axes[2]) > DM_STICK_MAX / 4 ||
        abs(s->axes[3]) > DM_STICK_MAX / 4)) {
        pad->logged_input = 1;
        LOG("手柄 HID：已收到 GameSir 实际输入 buttons=0x%llx axes=%d,%d,%d,%d",
            (unsigned long long)s->buttons,s->axes[0],s->axes[1],s->axes[2],s->axes[3]);
    }
    if (g_publish) g_publish(pad,s->buttons,s->axes[0],s->axes[1],s->axes[2],s->axes[3],DM_GLYPH_XBOX);
}
static void sample_pad(struct raw_pad *pad) {
    dm_gamesir_hid_clear(&pad->state);
    for (CFIndex i = 0; pad->elements && i < CFArrayGetCount(pad->elements); ++i) {
        IOHIDElementRef element = (IOHIDElementRef)CFArrayGetValueAtIndex(pad->elements,i);
        IOHIDElementType type = IOHIDElementGetType(element);
        uint32_t page = IOHIDElementGetUsagePage(element);
        if (type < kIOHIDElementTypeInput_Misc || type > kIOHIDElementTypeInput_ScanCodes ||
            (page != 1 && page != 2 && page != 9)) continue;
        IOHIDValueRef value = NULL;
        if (IOHIDDeviceGetValue(pad->device,element,&value) == kIOReturnSuccess && value)
            read_value(pad,value);
    }
    publish_pad(pad);
}
void dm_gamepad_hid_mac_focus(int focused) {
    g_focused = focused != 0;
    for (int i = 0; i < MAX_HID_PADS; ++i) if (g_raw[i].device) {
        if (g_focused) sample_pad(&g_raw[i]);
        else { dm_gamesir_hid_clear(&g_raw[i].state); publish_pad(&g_raw[i]); }
    }
}
static void input_value(void *context, IOReturn result, void *sender, IOHIDValueRef value) {
    (void)context; (void)sender;
    if (result != kIOReturnSuccess || !g_focused) return;
    struct raw_pad *pad = find_pad(IOHIDElementGetDevice(IOHIDValueGetElement(value)));
    if (pad && read_value(pad,value)) publish_pad(pad);
}
static void device_added(void *context, IOReturn result, void *sender, IOHIDDeviceRef device) {
    (void)context; (void)sender;
    if (result != kIOReturnSuccess || find_pad(device)) return;
    struct raw_pad *pad = NULL;
    for (int i = 0; i < MAX_HID_PADS; ++i) if (!g_raw[i].device) { pad = &g_raw[i]; break; }
    if (!pad) { LOG("手柄 HID：GameSir 槽已满"); return; }
    memset(pad,0,sizeof *pad);
    pad->elements = IOHIDDeviceCopyMatchingElements(device,NULL,kIOHIDOptionsTypeNone);
    if (!pad->elements || !CFArrayGetCount(pad->elements)) {
        if (pad->elements) CFRelease(pad->elements);
        memset(pad,0,sizeof *pad);
        LOG("手柄 HID：GameSir 没有可读元素，保留 GameController");
        return;
    }
    pad->device = (IOHIDDeviceRef)CFRetain(device);
    LOG("手柄 HID：连上 GameSir 3537:1104，等待原始输入 / Xbox 键位");
    if (g_focused) sample_pad(pad);
}
static void device_removed(void *context, IOReturn result, void *sender, IOHIDDeviceRef device) {
    (void)context; (void)result; (void)sender;
    struct raw_pad *pad = find_pad(device);
    if (!pad) return;
    if (g_remove) g_remove(pad);
    if (pad->elements) CFRelease(pad->elements);
    CFRelease(pad->device);
    memset(pad,0,sizeof *pad);
    LOG("手柄 HID：GameSir 已断开，清除该设备输入");
    if (g_changed) g_changed();
}
void dm_gamepad_hid_mac_start(dm_hid_pad_publish publish, dm_hid_pad_remove remove,
                              void (*ownership_changed)(void)) {
    if (g_manager) return;
    g_publish = publish; g_remove = remove; g_changed = ownership_changed;
    g_manager = IOHIDManagerCreate(kCFAllocatorDefault,kIOHIDOptionsTypeNone);
    if (!g_manager) return;
    IOHIDManagerSetDeviceMatching(g_manager,(__bridge CFDictionaryRef)@{
        @kIOHIDVendorIDKey:@0x3537,@kIOHIDProductIDKey:@0x1104,
        @kIOHIDDeviceUsagePageKey:@1,@kIOHIDDeviceUsageKey:@5});
    IOHIDManagerRegisterDeviceMatchingCallback(g_manager,device_added,NULL);
    IOHIDManagerRegisterDeviceRemovalCallback(g_manager,device_removed,NULL);
    IOHIDManagerRegisterInputValueCallback(g_manager,input_value,NULL);
    IOHIDManagerScheduleWithRunLoop(g_manager,CFRunLoopGetMain(),kCFRunLoopCommonModes);
    IOReturn result = IOHIDManagerOpen(g_manager,kIOHIDOptionsTypeNone);
    if (result != kIOReturnSuccess) {
        LOG("手柄 HID：无法打开 GameSir 输入 0x%x，保留 GameController",result);
        IOHIDManagerUnscheduleFromRunLoop(g_manager,CFRunLoopGetMain(),kCFRunLoopCommonModes);
        CFRelease(g_manager); g_manager = NULL;
        return;
    }
    CFSetRef devices = IOHIDManagerCopyDevices(g_manager);
    for (id device in (__bridge NSSet *)devices)
        device_added(NULL,kIOReturnSuccess,NULL,(__bridge IOHIDDeviceRef)device);
    if (devices) CFRelease(devices);
}
