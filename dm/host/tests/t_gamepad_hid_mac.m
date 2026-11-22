/* Production-path test, with a connected 3537:1104 device used only to obtain
 * its real HID elements. Synthesized values stay inside this process; neither
 * system input nor a running game is injected. No save files are opened. */
#import <Foundation/Foundation.h>
#include <assert.h>
#include <mach/mach_time.h>
#include "../dm_input.h"
static uint64_t last_buttons;
static int32_t last_axes[4];
void dm_padmode_request(int mode) { (void)mode; }
void dm_padmode_request_glyph(int glyph) { (void)glyph; }
int dm_front_navigation(void) { return 1; }
int dm_freetp_zr_free(int zl) { (void)zl; return 0; }
void dm_tp_request(void) {}
void dm_pad_set(uint64_t b,int32_t lx,int32_t ly,int32_t rx,int32_t ry) {
    last_buttons=b; last_axes[0]=lx; last_axes[1]=ly; last_axes[2]=rx; last_axes[3]=ry;
}
#include "../dm_gamepad_hid_mac.m"
#include "../../ios/Sources/dm_gamepad.m"

static void event(struct raw_pad *pad,uint32_t page,uint32_t usage,int value) {
    for (CFIndex i=0;i<CFArrayGetCount(pad->elements);++i) {
        IOHIDElementRef e=(IOHIDElementRef)CFArrayGetValueAtIndex(pad->elements,i);
        if (IOHIDElementGetUsagePage(e)!=page || IOHIDElementGetUsage(e)!=usage) continue;
        IOHIDValueRef v=IOHIDValueCreateWithIntegerValue(kCFAllocatorDefault,e,mach_absolute_time(),value);
        assert(v); input_value(NULL,kIOReturnSuccess,NULL,v); CFRelease(v); return;
    }
    assert(!"expected measured GameSir HID element");
}
int main(void) {
    @autoreleasepool {
        dm_gamepad_start(NULL);
        [[NSRunLoop mainRunLoop] runUntilDate:[NSDate dateWithTimeIntervalSinceNow:2]];
        struct raw_pad *p=NULL;
        for(int i=0;i<MAX_HID_PADS;++i) if(g_raw[i].device) {p=&g_raw[i];break;}
        if(!p) { puts("SKIP: GameSir 3537:1104 not connected"); return 77; }
        assert(!dm_gamepad_hid_mac_claims_vendor("Xbox Wireless Controller"));
        dm_gamepad_resume();
        /* Start from neutral independently of the physical controller position. */
        dm_gamesir_hid_clear(&p->state); publish_pad(p);
        event(p,9,1,1); assert(last_buttons==DM_BTN_A);
        assert(dm_gamepad_hid_mac_claims_vendor("GameSir-X4A for Xbox"));
        event(p,9,1,0); assert(last_buttons==0);
        event(p,1,48,255); assert(last_axes[0]==DM_STICK_MAX);
        event(p,1,49,0); assert(last_axes[1]==DM_STICK_MAX);
        event(p,9,1,1);
        for (int direction=0;direction<8;++direction) {
            event(p,1,57,direction);
            assert(last_buttons & (DM_BTN_LEFT|DM_BTN_UP|DM_BTN_RIGHT|DM_BTN_DOWN));
            event(p,1,57,15); /* Measured X4A release report. */
            assert(last_buttons==DM_BTN_A && last_axes[1]==DM_STICK_MAX);
            event(p,1,48,direction % 2 ? 0 : 255);
            assert(last_buttons==DM_BTN_A);
            assert(last_axes[0]==(direction % 2 ? -DM_STICK_MAX : DM_STICK_MAX));
        }
        event(p,9,1,0); assert(last_buttons==0);
        dm_gamepad_reset();
        assert(last_buttons==0 && last_axes[0]==0 && last_axes[1]==0);
        event(p,9,1,1); assert(last_buttons==0 && p->state.buttons==0);
        dm_gamepad_resume();
        dm_gamesir_hid_clear(&p->state); publish_pad(p);
        dm_gamepad_inject_n(2,DM_BTN_B,0,0,0,0);
        event(p,9,1,1); assert(last_buttons==(DM_BTN_A|DM_BTN_B));
        IOHIDDeviceRef device=(IOHIDDeviceRef)CFRetain(p->device);
        device_removed(NULL,kIOReturnSuccess,NULL,device);
        assert(last_buttons==DM_BTN_B);
        device_added(NULL,kIOReturnSuccess,NULL,device); CFRelease(device);
        dm_gamepad_inject_n(2,0,0,0,0,0);
        dm_gamepad_reset();
        assert(last_buttons==0 && last_axes[0]==0 && last_axes[1]==0 && last_axes[2]==0 && last_axes[3]==0);
        puts("t_gamepad_hid_mac: production input, hat null release, subsequent stick, focus, disconnect and merge passed");
    }
    return 0;
}
